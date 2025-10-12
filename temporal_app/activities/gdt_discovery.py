"""GDT invoice discovery activities - Real implementation."""

import asyncio
import httpx
from datetime import datetime
from typing import Any
from temporalio import activity

from temporal_app.models import GdtInvoice, GdtSession

# ============================================================================
# GDT Invoice API Endpoints
# ============================================================================
GDT_BASE_URL = "https://hoadondientu.gdt.gov.vn:30000"

# Flow-specific endpoints
FLOW_ENDPOINTS = {
    "ban_ra_dien_tu": f"{GDT_BASE_URL}/query/invoices/sold",
    "ban_ra_may_tinh_tien": f"{GDT_BASE_URL}/sco-query/invoices/sold",
    "mua_vao_dien_tu": f"{GDT_BASE_URL}/query/invoices/purchase",
    "mua_vao_may_tinh_tien": f"{GDT_BASE_URL}/sco-query/invoices/purchase",
}

# ============================================================================
# Configuration
# ============================================================================
MAX_RETRIES = 5
RETRY_DELAY_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 30.0


class GDTDiscoveryError(Exception):
    """Raised when invoice discovery fails."""
    pass


@activity.defn
async def discover_invoices(
    session: GdtSession,
    date_range_start: str,
    date_range_end: str,
    flows: list[str],
) -> list[GdtInvoice]:
    """
    Discover all invoices from GDT portal for specified flows.

    Flow:
    1. For each flow, make API request to flow-specific endpoint
    2. Process responses in parallel
    3. Parse invoice metadata from responses
    4. Return combined list of all invoices

    Args:
        session: GdtSession with bearer token and cookies
        date_range_start: Start date (YYYY-MM-DD)
        date_range_end: End date (YYYY-MM-DD)
        flows: List of flow codes (e.g., ["ban_ra_dien_tu", "mua_vao_dien_tu"])

    Returns:
        List of GdtInvoice objects discovered from all flows

    Raises:
        GDTDiscoveryError: If discovery fails for all flows
    """
    activity.logger.info(
        f"🔍 Discovering invoices for {session.company_id} "
        f"from {date_range_start} to {date_range_end} "
        f"for {len(flows)} flows"
    )

    # Build session headers with bearer token
    headers = _build_request_headers(session)

    # Process all flows in parallel
    activity.logger.info(f"🚀 Processing {len(flows)} flows in PARALLEL")

    async def fetch_flow_invoices(flow_code: str) -> tuple[str, list[GdtInvoice]]:
        """Fetch invoices for a single flow."""
        if flow_code not in FLOW_ENDPOINTS:
            activity.logger.warning(f"⚠️ Unknown flow: {flow_code}")
            return flow_code, []

        endpoint_url = FLOW_ENDPOINTS[flow_code]
        activity.logger.info(f"🔄 Fetching {flow_code} invoices")

        try:
            response_data = await _make_api_request(
                endpoint_url,
                flow_code,
                date_range_start,
                date_range_end,
                headers,
                session.cookies,
            )

            if response_data and response_data.get("datas"):
                invoices = _parse_invoices(response_data["datas"], flow_code)
                activity.logger.info(f"✅ {flow_code}: Found {len(invoices)} invoices")
                return flow_code, invoices
            else:
                activity.logger.warning(f"⚠️ {flow_code}: No invoices found")
                return flow_code, []

        except GDTDiscoveryError:
            # Re-raise GDTDiscoveryError (auth failures, rate limits, etc) - let Temporal handle retry
            activity.logger.error(f"❌ {flow_code} failed with GDTDiscoveryError - failing activity")
            raise
        except Exception as e:
            # Other unexpected errors - also fail the activity
            activity.logger.error(f"❌ {flow_code} failed with unexpected error: {str(e)}")
            raise GDTDiscoveryError(f"{flow_code} failed: {str(e)}")

    # Execute all flow requests in parallel
    results = await asyncio.gather(
        *[fetch_flow_invoices(flow) for flow in flows],
        return_exceptions=True,
    )

    # Combine results
    all_invoices = []
    for result in results:
        if isinstance(result, tuple):
            _, invoices = result
            all_invoices.extend(invoices)
        else:
            activity.logger.error(f"Flow request failed: {str(result)}")

    activity.logger.info(f"✅ Discovery complete: {len(all_invoices)} total invoices")

    # Send heartbeat with progress
    activity.heartbeat(f"Discovered {len(all_invoices)} invoices from {len(flows)} flows")

    return all_invoices


async def _make_api_request(
    endpoint_url: str,
    flow_name: str,
    date_start: str,
    date_end: str,
    headers: dict[str, str],
    cookies: dict[str, str],
) -> dict[str, Any] | None:
    """Make API request to GDT with pagination (follows direct_gdt_integration.py pattern)."""

    # Convert YYYY-MM-DD to DD/MM/YYYY format, then add time component
    # Input: "2025-09-01" -> Parse -> Format as "01/09/2025T00:00:00"
    start_dt = datetime.strptime(date_start, "%Y-%m-%d")
    end_dt = datetime.strptime(date_end, "%Y-%m-%d")

    start_date_str = start_dt.strftime("%d/%m/%Y")
    end_date_str = end_dt.strftime("%d/%m/%Y")

    # Base search parameters (following direct_gdt_integration.py format)
    base_search_params = f"tdlap=ge={start_date_str}T00:00:00;tdlap=le={end_date_str}T23:59:59"

    # Define ttxly values to crawl - ALL processing statuses [5, 6, 8] for purchase invoices
    ttxly_values = [5, 6, 8] if "purchase" in endpoint_url else [None]

    all_combined_data = {"datas": [], "total": 0}

    # Add Action header (URL-encoded Vietnamese text)
    action_map = {
        "ban_ra_dien_tu": "T%C3%ACm%20ki%E1%BA%BFm%20(h%C3%B3a%20%C4%91%C6%A1n%20%C4%91i%E1%BB%87n%20t%E1%BB%AD%20b%C3%A1n%20ra)",
        "ban_ra_may_tinh_tien": "T%C3%ACm%20ki%E1%BA%BFm%20(h%C3%B3a%20%C4%91%C6%A1n%20m%C3%A1y%20t%C3%ADnh%20ti%E1%BB%81n%20b%C3%A1n%20ra)",
        "mua_vao_dien_tu": "T%C3%ACm%20ki%E1%BA%BFm%20(h%C3%B3a%20%C4%91%C6%A1n%20%C4%91i%E1%BB%87n%20t%E1%BB%AD%20mua%20v%C3%A0o)",
        "mua_vao_may_tinh_tien": "T%C3%ACm%20ki%E1%BA%BFm%20(h%C3%B3a%20%C4%91%C6%A1n%20m%C3%A1y%20t%C3%ADnh%20ti%E1%BB%81n%20mua%20v%C3%A0o)",
    }
    if flow_name in action_map:
        headers = {**headers, "Action": action_map[flow_name]}

    # Process all ttxly values
    for ttxly in ttxly_values:
        # Build search params with ttxly filter if applicable
        if ttxly is not None:
            activity.logger.info(f"🔄 Fetching {flow_name} with ttxly={ttxly}")
            search_params = f"{base_search_params};ttxly=={ttxly}"
        else:
            activity.logger.info(f"🔄 Fetching {flow_name} (no ttxly filter)")
            search_params = base_search_params

        # Build base URL with query parameters (size before search!)
        page_size = 50
        full_url = f"{endpoint_url}?sort=tdlap:desc,khmshdon:asc,shdon:desc&size={page_size}&search={search_params}"

        # Pagination loop with state tokens
        page = 0
        state_token = None

        try:
            async with httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT_SECONDS,
                verify=False,
            ) as client:
                while True:
                    # Build paginated URL - first page doesn't need state parameter
                    if state_token:
                        paginated_url = f"{full_url}&state={state_token}"
                    else:
                        paginated_url = full_url

                    activity.logger.info(f"📄 Fetching {flow_name} page {page + 1}" + (f" (ttxly={ttxly})" if ttxly else ""))

                    # Make GET request
                    response = await client.get(
                        paginated_url,
                        headers=headers,
                        cookies=cookies,
                    )

                    # Handle rate limiting - let Temporal retry with exponential backoff
                    if response.status_code == 429:
                        activity.logger.warning(f"Rate limited (429) on {flow_name} - Temporal will retry")
                        # Let Temporal handle the retry with exponential backoff
                        # No manual backoff needed - GDT will clear the rate limit
                        raise GDTDiscoveryError(f"Rate limit exceeded (429) for {flow_name}")

                    # Auth error
                    if response.status_code in (401, 403):
                        activity.logger.error(f"Auth failed for {flow_name}: {response.status_code}")
                        raise GDTDiscoveryError(f"Authentication failed: {response.status_code}")

                    # Success
                    if response.status_code == 200:
                        page_data = response.json()

                        # Check if we have data
                        if not page_data or not page_data.get("datas"):
                            activity.logger.info(f"✅ {flow_name}: No more data on page {page + 1}")
                            break

                        # Add this page's data to combined results
                        current_page_count = len(page_data["datas"])
                        all_combined_data["datas"].extend(page_data["datas"])
                        all_combined_data["total"] = all_combined_data.get("total", 0) + current_page_count

                        activity.logger.info(f"✅ {flow_name}: Got {current_page_count} invoices on page {page + 1}")

                        # Extract state token for next page
                        state_token = page_data.get("state")

                        # Check pagination termination conditions
                        if not state_token or current_page_count < page_size:
                            activity.logger.info(f"✅ {flow_name}: Reached last page")
                            break

                        page += 1

                        # Safety limit to prevent infinite loops
                        if page >= 100:
                            activity.logger.warning(f"⚠️ Reached maximum page limit (100) for {flow_name}")
                            break

                        # Small delay between pages
                        await asyncio.sleep(0.1)

                    else:
                        # Other errors
                        activity.logger.error(
                            f"Request failed for {flow_name} ({response.status_code}): {response.text[:200]}"
                        )
                        raise GDTDiscoveryError(f"Request failed: HTTP {response.status_code}")

        except httpx.RequestError as e:
            activity.logger.error(f"Network error on {flow_name}: {str(e)}")
            raise GDTDiscoveryError(f"Network error: {str(e)}")

    # Return combined results or None if no data found
    if all_combined_data["datas"]:
        activity.logger.info(f"✅ Combined total: {len(all_combined_data['datas'])} invoices for {flow_name}")
        return all_combined_data
    else:
        activity.logger.warning(f"⚠️ No data found for {flow_name}")
        return None


def _parse_invoices(invoice_data: list[dict[str, Any]], flow_type: str) -> list[GdtInvoice]:
    """Parse invoice data from GDT API response."""
    invoices = []

    for item in invoice_data:
        try:
            # Parse date (GDT returns ISO format)
            date_str = item.get("tdlap", "")
            try:
                if date_str:
                    invoice_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                else:
                    invoice_date = datetime.now()
            except Exception:
                invoice_date = datetime.now()

            invoice = GdtInvoice(
                invoice_id=str(item.get("id", "")),
                invoice_number=str(item.get("shdon", "")),
                invoice_date=invoice_date.strftime("%Y-%m-%d"),
                invoice_type=flow_type,
                amount=float(item.get("tgtttbso", 0) or 0),
                tax_amount=float(item.get("tgtthue", 0) or 0),
                supplier_name=str(item.get("nbten", "")),
                supplier_tax_code=str(item.get("nbmst", "")),
                metadata={
                    "khhdon": str(item.get("khhdon", "")),  # Invoice code
                    "khmshdon": str(item.get("khmshdon", 1)),  # Invoice code type (must be string)
                    "buyer_name": str(item.get("nmten", "")),
                    "buyer_tax_code": str(item.get("nmmst", "")),
                    "status": str(item.get("tthai", "")),
                    "flow_type": flow_type,
                },
            )
            invoices.append(invoice)

        except Exception as e:
            activity.logger.warning(f"Failed to parse invoice: {str(e)}")
            continue

    return invoices


def _build_request_headers(session: GdtSession) -> dict[str, str]:
    """Build HTTP headers for GDT API requests."""
    # access_token already includes "Bearer " prefix from auth activity
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "vi",
        "Authorization": session.access_token,  # Already has "Bearer " prefix
        "Content-Type": "application/json",
        "Origin": "https://hoadondientu.gdt.gov.vn",
        "Referer": "https://hoadondientu.gdt.gov.vn/",
        "Host": "hoadondientu.gdt.gov.vn:30000",
        "End-Point": "/tra-cuu/tra-cuu-hoa-don",  # GDT custom header
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }
