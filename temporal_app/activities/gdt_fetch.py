"""GDT invoice fetching activities - Real implementation."""

import httpx
from datetime import datetime, timedelta
from temporalio import activity
from temporal_app.activities.hooks import emit_on_complete

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
@emit_on_complete(
    event_name="fetch.completed",
    payload_from_result=lambda r, invoice, session: {
        "company_id": getattr(invoice, "metadata", {}).get("company_id"),
        "invoice_id": getattr(invoice, "invoice_id", ""),
        "invoice_number": getattr(invoice, "invoice_number", ""),
        "invoice_detail": r.get("invoice_detail"),
        "line_items": r.get("line_items", []),
        "metadata": getattr(invoice, "metadata", {}),
    },
    compact_from_result=lambda r, invoice, session: InvoiceFetchResult(
        invoice_id=getattr(invoice, "invoice_id", ""),
        success=True,
        data={
            "invoice_id": getattr(invoice, "invoice_id", ""),
            "invoice_number": getattr(invoice, "invoice_number", ""),
            "metadata": getattr(invoice, "metadata", {}),
            "line_items_count": len(r.get("line_items", [])),
        },
    ),
)
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
    # Handle case where arguments are passed as a list (serialization issue)
    if isinstance(invoice, list):
        activity.logger.error(f"Received list instead of GdtInvoice: {invoice}")
        if len(invoice) != 2:
            return InvoiceFetchResult(
                invoice_id="unknown",
                success=False,
                error=f"Invalid argument format: expected [invoice, session], got {len(invoice)} items",
            )
        actual_invoice = invoice[0]
        actual_session = invoice[1]
        activity.logger.info(f"Extracted invoice and session from list for invoice {actual_invoice.get('invoice_id', 'unknown')}")
        invoice = actual_invoice
        session = actual_session
    
    # Handle case where invoice is a dictionary (serialization issue)
    if isinstance(invoice, dict):
        activity.logger.info(f"Converting dictionary to GdtInvoice for invoice {invoice.get('invoice_id', 'unknown')}")
        # Convert dictionary to GdtInvoice-like object
        class DictInvoice:
            def __init__(self, data):
                self.invoice_id = data.get('invoice_id', '')
                self.invoice_number = data.get('invoice_number', '')
                self.invoice_date = data.get('invoice_date', '')
                self.invoice_type = data.get('invoice_type', '')
                self.amount = data.get('amount', 0)
                self.tax_amount = data.get('tax_amount', 0)
                self.supplier_name = data.get('supplier_name', '')
                self.supplier_tax_code = data.get('supplier_tax_code', '')
                self.metadata = data.get('metadata', {})
        
        invoice = DictInvoice(invoice)
    
    # Handle case where session is a dictionary (serialization issue)
    if isinstance(session, dict):
        activity.logger.info(f"Converting dictionary to GdtSession")
        # Convert dictionary to GdtSession-like object
        class DictSession:
            def __init__(self, data):
                self.company_id = data.get('company_id', '')
                self.session_id = data.get('session_id', '')
                self.access_token = data.get('access_token', '')
                self.cookies = data.get('cookies', {})
                self.expires_at = data.get('expires_at', '')
        
        session = DictSession(session)

    activity.logger.info(f"📄 Fetching invoice details: {invoice.invoice_id}")

    # Extract invoice parameters from metadata
    nbmst = invoice.supplier_tax_code or invoice.metadata.get("nbmst", "")
    khhdon = invoice.metadata.get("khhdon", "")
    shdon = invoice.invoice_number
    khmshdon = invoice.metadata.get("khmshdon", "1")

    activity.logger.info(f"📋 Invoice parameters: nbmst={nbmst}, khhdon={khhdon}, shdon={shdon}, khmshdon={khmshdon}")

    if not all([nbmst, khhdon, shdon]):
        activity.logger.error(
            f"Missing required parameters: nbmst={nbmst}, khhdon={khhdon}, shdon={shdon}"
        )
        return InvoiceFetchResult(
            invoice_id=invoice.invoice_id,
            success=False,
            error="Missing required invoice parameters",
        )

    # Determine detail URL using endpoint_kind passed from discovery (preferred)
    endpoint_kind = invoice.metadata.get("endpoint_kind")
    flow_type = invoice.metadata.get("flow_type", "")
    khmshdon = invoice.metadata.get("khmshdon", "1")

    if endpoint_kind in ("sco-query", "query"):
        detail_url = GDT_DETAIL_SCO_URL if endpoint_kind == "sco-query" else GDT_DETAIL_URL
        activity.logger.info(f"🔀 Using endpoint from discovery: {endpoint_kind} (flow_type={flow_type}, khmshdon={khmshdon})")
    else:
        # Fallback to heuristic for backwards compatibility
        use_sco_endpoint = (
            "may_tinh_tien" in flow_type or 
            khmshdon in ["2", "3", "4"]
        )
        detail_url = GDT_DETAIL_SCO_URL if use_sco_endpoint else GDT_DETAIL_URL
        activity.logger.info(f"🔁 Fallback endpoint selection: {'sco-query' if use_sco_endpoint else 'query'} (flow_type={flow_type}, khmshdon={khmshdon})")

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

    # Build full URL with parameters for logging
    from urllib.parse import urlencode
    full_url = f"{detail_url}?{urlencode(params)}"
    activity.logger.info(f"🔗 Request URL: {full_url}")

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
                try:
                    # Check if response has content
                    if not response.content:
                        activity.logger.error(f"Empty response content for invoice {invoice.invoice_id}, Request URL: {full_url}, Response status: {response.status_code}")
                        raise Exception(f"Empty response content from detail API for invoice {invoice.invoice_id}")
                    
                    # Try to parse JSON
                    invoice_detail = response.json()
                    
                    if invoice_detail:
                        # Extract line items from hdhhdvu field (count only for compactness)
                        line_items = invoice_detail.get("hdhhdvu", [])
                        activity.logger.info(
                            f"✅ Fetched invoice {invoice.invoice_id} with {len(line_items)} line items"
                        )

                        # Return full data to decorator for webhook; decorator returns compact to workflow
                        return {"invoice_detail": invoice_detail, "line_items": line_items}
                    else:
                        activity.logger.error(f"Empty JSON response for invoice {invoice.invoice_id}, Response: {response.text[:500]}, Request URL: {full_url}, Response status: {response.status_code}, Raw Response: {response}")
                        raise Exception(f"Empty JSON response from detail API for invoice {invoice.invoice_id}")
                        
                        
                except Exception as json_error:
                    activity.logger.error(f"JSON parsing failed for invoice {invoice.invoice_id}: {str(json_error)}, Request URL: {full_url}, Response status: {response.status_code}")
                    raise Exception(f"JSON parsing failed for invoice {invoice.invoice_id}: {str(json_error)}")

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
