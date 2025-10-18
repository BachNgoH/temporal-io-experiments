"""Activity to send Lark notifications.

This is invoked by workflow interceptors to avoid doing network I/O in workflow code.
"""

from __future__ import annotations

from typing import Any

from temporalio import activity

from app.config import settings
from temporal_app.interceptors.lark.client import LarkWebhookBot


@activity.defn(name="lark.notify")
async def lark_notify(event: dict[str, Any]) -> None:
    """Send a Lark notification.

    Expected event keys:
    - event: str (workflow_started | workflow_completed | workflow_failed)
    - fields: dict[str, Any] (optional)
    """
    bot = LarkWebhookBot(settings.lark_webhook_url)
    if not bot.is_configured():
        return

    event_name = str(event.get("event", "event")).replace("_", " ").title()
    fields = event.get("fields") or {}

    severity = "LOW"
    if "failed" in str(event.get("event", "")).lower():
        severity = "HIGH"
    if "completed" in str(event.get("event", "")).lower():
        severity = "SUCCESS"

    await bot.send_card(title=event_name, fields=fields, severity=severity)


