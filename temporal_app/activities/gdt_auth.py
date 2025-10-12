"""GDT authentication activities."""

import asyncio
import random
from datetime import datetime, timedelta

from temporalio import activity

from temporal_app.models import GdtLoginRequest, GdtSession


@activity.defn
async def login_to_gdt(request: GdtLoginRequest) -> GdtSession:
    """
    Login to GDT portal.

    This is a MOCK implementation. In production, this would:
    1. Make HTTP request to GDT login endpoint
    2. Handle captcha if needed
    3. Extract session tokens and cookies
    4. Store session information
    """
    activity.logger.info(f"Logging in to GDT for company: {request.company_id}")

    # Simulate network delay
    await asyncio.sleep(random.uniform(1.0, 3.0))

    # Mock: Simulate occasional login failures (10% chance)
    if random.random() < 0.1:
        activity.logger.warning(f"Login failed for {request.company_id} - will retry")
        raise Exception("Login failed: Invalid captcha or network error")

    # Mock: Generate fake session data
    session = GdtSession(
        company_id=request.company_id,
        session_id=f"session_{request.company_id}_{datetime.now().timestamp()}",
        access_token=f"token_{random.randint(100000, 999999)}",
        cookies={
            "JSESSIONID": f"jsession_{random.randint(100000, 999999)}",
            "gdt_session": f"gdt_{random.randint(100000, 999999)}",
        },
        expires_at=datetime.now() + timedelta(hours=2),
    )

    activity.logger.info(f"✅ Login successful for {request.company_id}")

    return session
