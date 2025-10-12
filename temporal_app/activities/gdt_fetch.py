"""GDT invoice fetching activities - Real implementation."""

import httpx
from datetime import datetime, timedelta
from temporalio import activity

from temporal_app.models import GdtInvoice, GdtSession, InvoiceFetchResult

# ============================================================================
# GDT Invoice Detail URLs
# ============================================================================
GDT_BASE_URL = "https://hoadondientu.gdt.gov.vn:30000"
GDT_DETAIL_URL = f"{GDT_BASE_URL}/query/invoices/detail"
GDT_DETAIL_SCO_URL = f"{GDT_BASE_URL}/sco-query/invoices/detail"

# ============================================================================
# Configuration
# ============================================================================
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 30.0
RATE_LIMIT_BACKOFF_SECONDS = 30

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
    Fetch detailed invoice JSON from GDT portal.

    Flow:
    1. Check shared rate limit state
    2. Build detail API URL with invoice parameters
    3. Download invoice JSON with full details including line items (hdhhdvu)
    4. Return invoice data with JSON content

    Rate Limiting Strategy:
    - Check state before making request
    - If 429 → set backoff and raise RateLimitError
    - Temporal retries with exponential backoff
    - Other concurrent fetches see backoff state and wait

    Args:
        invoice: GdtInvoice with invoice_id, metadata (khhdon, nbmst, etc.)
        session: GdtSession with bearer token and cookies

    Returns:
        InvoiceFetchResult with JSON content or error
    """
    activity.logger.info(f"📄 Fetching invoice details: {invoice.invoice_id}")

    # Check if we're currently rate limited (shared state)
    if not await check_rate_limit(session.company_id):
        raise RateLimitError(f"Rate limited for company {session.company_id}")

    # Extract invoice parameters from metadata
    nbmst = invoice.supplier_tax_code or invoice.metadata.get("nbmst", "")
    khhdon = invoice.metadata.get("khhdon", "")
    shdon = invoice.invoice_number
    khmshdon = invoice.metadata.get("khmshdon", "1")

    if not all([nbmst, khhdon, shdon]):
        activity.logger.error(
            f"Missing required parameters: nbmst={nbmst}, khhdon={khhdon}, shdon={shdon}"
        )
        return InvoiceFetchResult(
            invoice_id=invoice.invoice_id,
            success=False,
            error="Missing required invoice parameters",
        )

    # Determine detail URL based on invoice type (electronic vs cash register)
    flow_type = invoice.metadata.get("flow_type", "")
    if "may_tinh_tien" in flow_type:
        detail_url = GDT_DETAIL_SCO_URL  # Cash register (SCO)
    else:
        detail_url = GDT_DETAIL_URL  # Electronic

    # Build query parameters
    params = {
        "nbmst": nbmst,
        "khhdon": khhdon,
        "shdon": shdon,
        "khmshdon": khmshdon,
    }

    # Build headers with bearer token
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "vi",
        "Authorization": session.access_token,
        "Origin": "https://hoadondientu.gdt.gov.vn",
        "Referer": "https://hoadondientu.gdt.gov.vn/",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }

    # Fetch invoice details (Temporal handles retries)
    try:
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_SECONDS,
            verify=False,
        ) as client:
            response = await client.get(
                detail_url,
                params=params,
                headers=headers,
                cookies=session.cookies,
            )

            # Handle rate limiting
            if response.status_code == 429:
                activity.logger.warning(f"Rate limited (429) for invoice {invoice.invoice_id}")
                await set_rate_limit_backoff(
                    session.company_id,
                    backoff_seconds=RATE_LIMIT_BACKOFF_SECONDS,
                )
                raise RateLimitError(f"Rate limit exceeded (429) for invoice {invoice.invoice_id}")

            # Success - process response
            if response.status_code == 200:
                invoice_detail = response.json()

                if invoice_detail:
                    # Extract line items from hdhhdvu field
                    line_items = invoice_detail.get("hdhhdvu", [])
                    activity.logger.info(
                        f"✅ Fetched invoice {invoice.invoice_id} with {len(line_items)} line items"
                    )

                    return InvoiceFetchResult(
                        invoice_id=invoice.invoice_id,
                        success=True,
                        data={
                            "invoice_id": invoice.invoice_id,
                            "invoice_number": invoice.invoice_number,
                            "invoice_detail": invoice_detail,  # Full JSON response
                            "line_items": line_items,  # Invoice line items (hdhhdvu)
                            "metadata": invoice.metadata,
                        },
                    )
                else:
                    activity.logger.error("Empty response from detail API")
                    return InvoiceFetchResult(
                        invoice_id=invoice.invoice_id,
                        success=False,
                        error="Empty response from detail API",
                    )

            # Auth error
            if response.status_code in (401, 403):
                activity.logger.error(f"Auth failed for invoice {invoice.invoice_id}")
                return InvoiceFetchResult(
                    invoice_id=invoice.invoice_id,
                    success=False,
                    error=f"Authentication failed: {response.status_code}",
                )

            # Other errors (let Temporal retry)
            activity.logger.error(
                f"Download failed for {invoice.invoice_id} ({response.status_code}): {response.text[:200]}"
            )
            raise RateLimitError(f"Download failed: HTTP {response.status_code}")

    except httpx.RequestError as e:
        activity.logger.error(f"Network error for invoice {invoice.invoice_id}: {str(e)}")
        raise RateLimitError(f"Network error: {str(e)}")
