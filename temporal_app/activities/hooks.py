"""Reusable activity decorators for emitting webhooks without bloating workflow payloads."""

import os
import functools
import json
import time
import hmac
import hashlib
import base64
from typing import Any, Callable
from email.utils import formatdate

import httpx
from temporalio import activity


def emit_on_complete(
    event_name: str,
    payload_from_result: Callable[[Any, Any, Any], dict],
    compact_from_result: Callable[[Any, Any, Any], Any],
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: after activity returns, POST payload to webhook and return compact result.

    - payload_from_result(result, *args) must build the full payload to send externally
    - compact_from_result(result, *args) must build the small object returned to workflow
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            result = await func(*args, **kwargs)
            try:
                payload = payload_from_result(result, *args, **kwargs)
                url = os.getenv("EVENT_WEBHOOK_URL", "http://host.docker.internal:8000/internal/webhooks")
                
                # Get workflow run_id from activity info
                run_id = activity.info().workflow_run_id
                
                envelope = {
                    "event_id": activity.info().activity_id,
                    "event_name": event_name,
                    "run_id": run_id,
                    "workflow_id": activity.info().workflow_id,
                    "payload": payload,
                }

                # Serialize with stable formatting for deterministic signatures
                body_bytes = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

                # Build signed headers per RFC 9421 (HTTP Message Signatures) using HMAC-SHA256
                headers = _build_http_signature_headers(
                    method="POST",
                    url=url,
                    body=body_bytes,
                    key_id=os.getenv("WEBHOOK_SIGNING_KEY_ID", "temporal-worker"),
                    secret=os.getenv("WEBHOOK_SIGNING_SECRET", ""),
                )

                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.post(url, content=body_bytes, headers=headers)
            except Exception as e:
                activity.logger.warning(f"emit_on_complete failed: {e}")

            try:
                return compact_from_result(result, *args, **kwargs)
            except Exception as e:
                activity.logger.warning(f"emit_on_complete compact result failed: {e}")
                return result

        return wrapper

    return decorator


def _build_http_signature_headers(method: str, url: str, body: bytes, key_id: str, secret: str) -> dict[str, str]:
    """Create headers: Content-Digest, Date, Signature-Input, Signature.

    Uses HMAC-SHA256 over canonical components (method, target-uri, content-digest, date).
    If secret is missing, emits only Content-Digest and Date (unsigned mode).
    """
    # Content-Type set explicitly
    headers: dict[str, str] = {"Content-Type": "application/json"}

    # Date header in IMF-fixdate, e.g., Mon, 13 Oct 2025 10:02:15 GMT
    date_hdr = formatdate(timeval=None, localtime=False, usegmt=True)
    headers["Date"] = date_hdr

    # Content-Digest per RFC 9530: sha-256=:<base64>:
    sha256 = hashlib.sha256(body).digest()
    content_digest_value = f"sha-256=:{base64.b64encode(sha256).decode('ascii')}:"
    headers["Content-Digest"] = content_digest_value

    if not secret:
        # Unsigned mode (development); still include digest/date
        return headers

    # Build signature base (simple canonical form for covered components)
    covered_components = ["@method", "@target-uri", "content-digest", "date"]
    created = int(time.time())

    # Canonicalize components into a deterministic string
    signature_base_lines = [
        f"@method: {method.upper()}",
        f"@target-uri: {url}",
        f"content-digest: {content_digest_value}",
        f"date: {date_hdr}",
    ]
    signature_base = "\n".join(signature_base_lines).encode("utf-8")

    mac = hmac.new(secret.encode("utf-8"), signature_base, hashlib.sha256).digest()
    signature_b64 = base64.b64encode(mac).decode("ascii")

    sig_input = (
        'sig1=("' + '" "'.join(covered_components) + f'");alg="hmac-sha256";created={created};keyid="{key_id}"'
    )
    sig_value = f"sig1=:{signature_b64}:"

    headers["Signature-Input"] = sig_input
    headers["Signature"] = sig_value
    # Optional companion header for non-RFC consumers
    headers["X-Signature"] = signature_b64

    return headers


