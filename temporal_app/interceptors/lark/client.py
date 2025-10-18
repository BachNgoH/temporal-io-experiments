"""Lark webhook client used by interceptors and activities."""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx


logger = logging.getLogger(__name__)


class LarkWebhookBot:
    def __init__(self, webhook_url: str | None) -> None:
        self.webhook_url = webhook_url

    def is_configured(self) -> bool:
        return bool(self.webhook_url)

    async def send_text(self, text: str) -> bool:
        if not self.webhook_url:
            return False
        try:
            payload = {
                "msg_type": "text",
                "content": {"text": text},
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(self.webhook_url, json=payload)
            data = resp.json() if resp.content else {}
            ok = data.get("code") == 0
            if ok:
                logger.info("Lark text sent")
            else:
                logger.warning("Lark text failed: %s", data)
            return ok
        except Exception as e:
            logger.warning("Lark text error: %s", e)
            return False

    async def send_card(self, title: str, fields: dict[str, Any], severity: str = "LOW") -> bool:
        if not self.webhook_url:
            return False
        colors = {
            "CRITICAL": "red",
            "HIGH": "orange",
            "MEDIUM": "yellow",
            "LOW": "blue",
            "SUCCESS": "green",
        }
        emojis = {
            "CRITICAL": "🔥",
            "HIGH": "🚨",
            "MEDIUM": "⚠️",
            "LOW": "ℹ️",
            "SUCCESS": "✅",
        }

        # Use Vietnam time (UTC+7). Prefer ZoneInfo when available, fallback to fixed offset.
        try:
            from zoneinfo import ZoneInfo  # Python 3.9+
            viet_tz = ZoneInfo("Asia/Ho_Chi_Minh")
        except Exception:
            viet_tz = timezone(timedelta(hours=7))
        ts = datetime.now(timezone.utc).astimezone(viet_tz).strftime("%Y-%m-%d %H:%M:%S %Z")

        def field_pair(k: str, v: Any) -> dict[str, Any]:
            return {
                "is_short": True,
                "text": {"tag": "lark_md", "content": f"**{k}:** {v}"},
            }

        elements: list[dict[str, Any]] = [
            {"tag": "div", "fields": [field_pair(k, v) for k, v in fields.items()]},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": f"⏰ **Time:** {ts}"}},
        ]

        card_content = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"{emojis.get(severity, 'ℹ️')} {title}"},
                "template": colors.get(severity, "blue"),
            },
            "elements": elements,
        }

        payload = {"msg_type": "interactive", "card": card_content}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(self.webhook_url, json=payload)
            data = resp.json() if resp.content else {}
            ok = data.get("code") == 0
            if ok:
                logger.info("Lark card sent")
            else:
                logger.warning("Lark card failed: %s", data)
            return ok
        except Exception as e:
            logger.warning("Lark card error: %s", e)
            return False


