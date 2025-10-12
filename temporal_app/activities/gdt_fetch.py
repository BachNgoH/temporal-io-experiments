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
REQUEST_TIMEOUT_SECONDS = 30.0


@activity.defn
async def fetch_invoice(
    invoice: GdtInvoice,
    session: GdtSession,
) -> InvoiceFetchResult:
    """
    Fetch detailed invoice JSON from GDT portal.

    Flow:
    1. Build detail API URL with invoice parameters
    2. Download invoice JSON with full details including line items (hdhhdvu)
    3. Return invoice data with JSON content

    Rate Limiting Strategy:
    - Let GDT handle their own rate limiting (429 responses)
    - Temporal automatically retries with exponential backoff
    - No manual rate limiting - trust GDT's rate limit headers

    Args:
        invoice: GdtInvoice with invoice_id, metadata (khhdon, nbmst, etc.)
        session: GdtSession with bearer token and cookies

    Returns:
        InvoiceFetchResult with JSON content or error
    """
    activity.logger.info(f"📄 Fetching invoice details: {invoice.invoice_id}")

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

            # Handle rate limiting - let Temporal retry with exponential backoff
            if response.status_code == 429:
                activity.logger.warning(f"Rate limited (429) for invoice {invoice.invoice_id} - Temporal will retry")
                # Let Temporal handle the retry with exponential backoff
                # No manual backoff needed - GDT will clear the rate limit
                raise Exception(f"Rate limit exceeded (429) for invoice {invoice.invoice_id}")

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
            raise Exception(f"Download failed: HTTP {response.status_code}")

    except httpx.RequestError as e:
        activity.logger.error(f"Network error for invoice {invoice.invoice_id}: {str(e)}")
        raise Exception(f"Network error: {str(e)}")
