"""GDT invoice fetching activities."""

import asyncio
import random
from datetime import datetime, timedelta

from temporalio import activity

from temporal_app.models import GdtInvoice, GdtSession, InvoiceFetchResult

# ============================================================================
# Shared Rate Limit State (In-Memory)
# ============================================================================
# In production: Use Redis or Temporal's own state management
# For now: Simple in-memory dict shared across all activities in same worker

_rate_limit_state: dict[str, datetime] = {}


class RateLimitError(Exception):
    """Raised when rate limit is detected (429)."""

    pass


async def check_rate_limit(company_id: str) -> bool:
    """
    Check if we're currently rate limited for this company.

    Returns:
        bool: True if OK to proceed, False if rate limited

    In production:
    - Use Redis with TTL for distributed rate limit tracking
    - Store backoff_until timestamp
    - All workers check same Redis key
    """
    backoff_until = _rate_limit_state.get(company_id)

    if backoff_until and datetime.now() < backoff_until:
        wait_seconds = (backoff_until - datetime.now()).total_seconds()
        activity.logger.warning(
            f"Rate limited for {company_id}, backing off for {wait_seconds:.1f}s"
        )
        return False

    return True


async def set_rate_limit_backoff(company_id: str, backoff_seconds: int) -> None:
    """
    Set rate limit backoff for this company.

    In production:
    - Store in Redis with TTL
    - Use exponential backoff based on consecutive 429s
    """
    backoff_until = datetime.now() + timedelta(seconds=backoff_seconds)
    _rate_limit_state[company_id] = backoff_until

    activity.logger.warning(f"Setting rate limit backoff for {company_id}: {backoff_seconds}s")


@activity.defn
async def fetch_invoice(
    invoice: GdtInvoice,
    session: GdtSession,
) -> InvoiceFetchResult:
    """
    Fetch detailed invoice data from GDT portal.

    Rate Limiting Strategy:
    1. Check shared rate limit state before fetching
    2. If rate limited → raise RateLimitError (Temporal will retry with backoff)
    3. If 429 response → set backoff state + raise RateLimitError
    4. Temporal's exponential backoff spreads retries naturally

    Why this prevents cascade failures:
    - First invoice to hit 429 sets backoff state
    - Other concurrent fetches check state and back off immediately
    - No thundering herd of retries
    - Exponential backoff gradually increases delay

    This is a MOCK implementation. In production, this would:
    1. Make HTTP request to fetch invoice details
    2. Download invoice PDF/XML
    3. Store invoice data to Cloud Storage
    4. Return fetch result
    """
    activity.logger.info(f"Fetching invoice: {invoice.invoice_id}")

    # Check if we're currently rate limited (shared state)
    if not await check_rate_limit(session.company_id):
        # Don't even try - we know we're rate limited
        raise RateLimitError(f"Rate limited for company {session.company_id}")

    # Simulate network delay
    await asyncio.sleep(random.uniform(0.3, 1.0))

    # Mock: Simulate rate limiting (10% chance of 429)
    # In production: Detect actual 429 response from GDT
    if random.random() < 0.1:
        activity.logger.warning(f"429 Rate Limit for invoice {invoice.invoice_id}")

        # Set backoff state so other concurrent fetches back off
        await set_rate_limit_backoff(session.company_id, backoff_seconds=30)

        # Raise error → Temporal will retry with exponential backoff
        raise RateLimitError("Rate limit exceeded (429)")

    # Mock: Simulate occasional failures (5% chance, non-rate-limit)
    if random.random() < 0.05:
        activity.logger.warning(f"Failed to fetch invoice {invoice.invoice_id}")
        return InvoiceFetchResult(
            invoice_id=invoice.invoice_id,
            success=False,
            error="Network timeout",
        )

    # Mock: Generate detailed invoice data
    invoice_data = {
        "invoice_id": invoice.invoice_id,
        "invoice_number": invoice.invoice_number,
        "invoice_date": invoice.invoice_date,
        "invoice_type": invoice.invoice_type,
        "amount": invoice.amount,
        "tax_amount": invoice.tax_amount,
        "supplier_name": invoice.supplier_name,
        "supplier_tax_code": invoice.supplier_tax_code,
        "line_items": [
            {
                "description": f"Item {i+1}",
                "quantity": random.randint(1, 10),
                "unit_price": round(random.uniform(10.0, 1000.0), 2),
            }
            for i in range(random.randint(1, 5))
        ],
        "metadata": invoice.metadata,
        # In production: Would include storage_path to Cloud Storage
        "storage_path": f"gs://gdt-invoices/{session.company_id}/{invoice.invoice_id}.json",
    }

    activity.logger.info(f"✅ Fetched invoice {invoice.invoice_id}")

    return InvoiceFetchResult(
        invoice_id=invoice.invoice_id,
        success=True,
        data=invoice_data,
    )
