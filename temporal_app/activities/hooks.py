"""Reusable activity decorators for emitting webhooks without bloating workflow payloads."""

import os
import functools
from typing import Any, Callable

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
                envelope = {
                    "event_id": activity.info().activity_id,
                    "event_name": event_name,
                    "payload": payload,
                }
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.post(url, json=envelope)
            except Exception as e:
                activity.logger.warning(f"emit_on_complete failed: {e}")

            try:
                return compact_from_result(result, *args, **kwargs)
            except Exception as e:
                activity.logger.warning(f"emit_on_complete compact result failed: {e}")
                return result

        return wrapper

    return decorator


