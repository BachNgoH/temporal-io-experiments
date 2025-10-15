"""Reusable activity decorator for emitting webhooks with minimal ceremony.

Design goals:
  - Simple DX: by default, payload = activity return, and the activity returns its original result
  - Small payloads when needed: exclude keys at send-time without changing the activity code
  - Consistent envelope: only event_id, event_name, payload (no workflow ids)

If payloads grow too large, prefer excluding keys rather than changing return values.
"""

import os
import functools
import json
import hmac
import hashlib
from typing import Any, Optional
from dataclasses import is_dataclass, asdict

import httpx
from app.config import settings
from temporalio import activity
from pydantic import BaseModel


class EventEnvelope(BaseModel):
    """Minimal webhook envelope.

    Only the essentials: event id, name, and payload.
    """

    event_id: str
    event_name: str
    payload: Any


def emit_on_complete(
    event_name: str,
    exclude_payload_keys: Optional[set[str]] = None,
    exclude_result_keys: Optional[set[str]] = None,
):
    """Decorator: after activity returns, POST payload to webhook and return original result.

    Args:
        event_name: Logical event name (e.g., "discovery.completed").
        exclude_keys: Optional set of keys to exclude from the payload if the activity result
            is a dict or a Pydantic model. The activity's return value is NOT modified.
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            result = await func(*args, **kwargs)

            try:
                # Build payload from result (apply exclusions for dicts and Pydantic models)
                payload_obj: Any
                if isinstance(result, BaseModel):
                    payload_obj = result.model_dump(exclude=exclude_payload_keys or set())
                elif isinstance(result, dict):
                    if exclude_payload_keys:
                        payload_obj = {k: v for k, v in result.items() if k not in exclude_payload_keys}
                    else:
                        payload_obj = result
                elif is_dataclass(result):
                    data_dict = asdict(result)
                    if exclude_payload_keys:
                        payload_obj = {k: v for k, v in data_dict.items() if k not in exclude_payload_keys}
                    else:
                        payload_obj = data_dict
                else:
                    payload_obj = result

                envelope = EventEnvelope(
                    event_id=activity.info().activity_id,
                    event_name=event_name,
                    payload=payload_obj,
                )

                body_str = envelope.model_dump_json()
                body_bytes = body_str.encode("utf-8")
                secret = settings.webhook_signing_secret
                headers = _build_b4b_headers(body=body_bytes, secret=secret)

                async with httpx.AsyncClient(timeout=10.0) as client:
                    print("EMMITING TO ", settings.webhook_url)
                    await client.post(settings.webhook_url, content=body_bytes, headers=headers)

            except Exception as e:
                activity.logger.warning(f"emit_on_complete failed: {e}")

            # Return result (optionally excluding keys for BaseModel/dict)
            if exclude_result_keys:
                try:
                    if isinstance(result, BaseModel):
                        result_dict = result.model_dump()
                        for k in exclude_result_keys:
                            result_dict.pop(k, None)
                        return result.__class__(**result_dict)
                    if isinstance(result, dict):
                        return {k: v for k, v in result.items() if k not in exclude_result_keys}
                except Exception:
                    pass

            return result

        return wrapper

    return decorator


def _build_b4b_headers(body: bytes, secret: str) -> dict[str, str]:
    """Headers for b4b webhook verification.

    - Content-Type: application/json
    - X-Webhook-Signature: sha256=<hex(hmac_sha256(secret, body))>
    """
    mac_hex = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Webhook-Signature": f"sha256={mac_hex}",
    }


