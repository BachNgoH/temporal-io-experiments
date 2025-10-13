"""GDT Excel-based invoice discovery activities - Alternative to API discovery."""

import asyncio
import json
import os
import tempfile
from datetime import datetime, date, timedelta
from typing import Any, Optional
from temporalio import activity
from temporal_app.activities.hooks import emit_on_complete

from temporal_app.models import GdtInvoice, GdtSession

# ============================================================================
# Configuration
# ============================================================================
MAX_RETRIES = 5
RETRY_DELAY_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 60.0  # Longer timeout for Excel downloads


class GDTExcelDiscoveryError(Exception):
    """Raised when Excel-based invoice discovery fails."""
    pass


@activity.defn
@emit_on_complete(
    event_name="discovery.completed",
    payload_from_result=lambda invoices, session, date_range_start, date_range_end, flows: {
        "company_id": getattr(session, "company_id", ""),
        "date_range_start": date_range_start,
        "date_range_end": date_range_end,
        "flows": flows,
        "invoice_count": len(invoices or []),
    },
    compact_from_result=lambda invoices, *args, **kwargs: invoices,
)
async def discover_invoices_excel(
    session: GdtSession,
    date_range_start: str,
    date_range_end: str,
    flows: list[str],
) -> list[GdtInvoice]:
    """
    Discover invoices using Excel export method - more reliable than API discovery.
    
    Flow:
    1. Download Excel files for each flow and processing status
    2. Parse Excel files to extract invoice metadata
    3. Convert to GdtInvoice objects
    4. Return combined list of all invoices
    
    Args:
        session: GdtSession with bearer token and cookies
        date_range_start: Start date (YYYY-MM-DD)
        date_range_end: End date (YYYY-MM-DD)
        flows: List of flow codes (e.g., ["ban_ra_dien_tu", "mua_vao_dien_tu"])
    
    Returns:
        List of GdtInvoice objects discovered from Excel exports
    
    Raises:
        GDTExcelDiscoveryError: If Excel discovery fails
    """
    activity.logger.info(
        f"📊 Excel Discovery: Starting for {session.company_id} "
        f"from {date_range_start} to {date_range_end} "
        f"for {len(flows)} flows"
    )
    
    try:
        # Create temporary directory for Excel files
        with tempfile.TemporaryDirectory() as temp_dir:
            activity.logger.info(f"📁 Using temporary directory: {temp_dir}")
            
            # Download Excel files for all flows
            excel_files = await _download_excel_files_for_flows(
                session, date_range_start, date_range_end, flows, temp_dir
            )
            
            if not excel_files:
                activity.logger.warning("⚠️ No Excel files downloaded")
                return []
            
            # Parse Excel files to extract invoices
            all_invoices = await _parse_excel_files_to_invoices(excel_files, flows)
            
            activity.logger.info(f"✅ Excel Discovery complete: {len(all_invoices)} total invoices")
            
            # Send heartbeat with progress
            activity.heartbeat(f"Excel discovery found {len(all_invoices)} invoices from {len(excel_files)} files")
            
            return all_invoices
            
    except Exception as e:
        activity.logger.error(f"❌ Excel discovery failed: {str(e)}")
        raise GDTExcelDiscoveryError(f"Excel discovery failed: {str(e)}")


async def _download_excel_files_for_flows(
    session: GdtSession,
    date_start: str,
    date_end: str,
    flows: list[str],
    temp_dir: str,
) -> list[str]:
    """Download Excel files for all flows and processing statuses."""
    
    # Convert date strings to date objects
    start_date = datetime.strptime(date_start, "%Y-%m-%d").date()
    end_date = datetime.strptime(date_end, "%Y-%m-%d").date()
    
    # Map flows to invoice types
    flow_to_invoice_type = {
        "ban_ra_dien_tu": "sold",
        "ban_ra_may_tinh_tien": "sold", 
        "mua_vao_dien_tu": "purchase",
        "mua_vao_may_tinh_tien": "purchase",
    }
    
    downloaded_files = []
    
    # Download Excel files per flow mapped to its correct endpoint
    for flow in flows:
        invoice_type = flow_to_invoice_type.get(flow, "purchase")
        endpoint_kind = "sco-query" if "may_tinh_tien" in flow else "query"
        activity.logger.info(
            f"📥 Downloading Excel for flow={flow} (type={invoice_type}, endpoint={endpoint_kind})"
        )

        # For purchase invoices, iterate ttxly; for sold, no ttxly
        # 5: Đã cấp mã hoá đơn, 6: Cục thuế đã nhận không mã, 8: Cục thuế đã nhận hoá đơn có mã khởi tạo từ máy tính tiền
        ttxly_iterable = [5, 6, 8] if invoice_type == "purchase" else [None]

        for ttxly in ttxly_iterable:
            try:
                file_path = await _download_single_excel_file(
                    session=session,
                    start_date=start_date,
                    end_date=end_date,
                    invoice_type=invoice_type,
                    endpoint_kind=endpoint_kind,
                    ttxly=ttxly,
                    temp_dir=temp_dir,
                    flow_code=flow,
                )

                if file_path:
                    downloaded_files.append(file_path)
                    activity.logger.info(f"✅ Downloaded: {os.path.basename(file_path)}")
                else:
                    suffix = f" ttxly={ttxly}" if ttxly is not None else ""
                    activity.logger.warning(
                        f"⚠️ Failed to download flow={flow} ({invoice_type}) from {endpoint_kind}{suffix}"
                    )

                # Delay between downloads to avoid rate limiting
                await asyncio.sleep(3.0)

            except Exception as e:
                suffix = f" ttxly={ttxly}" if ttxly is not None else ""
                activity.logger.error(
                    f"❌ Error downloading flow={flow} ({invoice_type}) from {endpoint_kind}{suffix}: {str(e)}"
                )
                continue
    
    activity.logger.info(f"📊 Downloaded {len(downloaded_files)} Excel files")
    return downloaded_files


async def _download_single_excel_file(
    session: GdtSession,
    start_date: date,
    end_date: date,
    invoice_type: str,
    endpoint_kind: str,
    ttxly: Optional[int],
    temp_dir: str,
    flow_code: Optional[str] = None,
) -> Optional[str]:
    """Download a single Excel file for specific parameters."""
    
    import httpx
    
    # Format dates for API
    start_date_str = start_date.strftime("%d/%m/%Y")
    end_date_str = end_date.strftime("%d/%m/%Y")
    
    # Build search parameters
    base_search = f"tdlap=ge={start_date_str}T00:00:00;tdlap=le={end_date_str}T23:59:59"
    if invoice_type == "purchase" and ttxly is not None:
        search_params = f"{base_search};ttxly=={ttxly}"
    else:
        search_params = base_search

    # Determine export URL based on invoice type and endpoint
    # Use "export-excel-sold" for purchase and "export-excel" for sold, mirroring existing behavior
    path_suffix = "export-excel-sold" if invoice_type == "purchase" else "export-excel"
    export_url = f"https://hoadondientu.gdt.gov.vn:30000/{endpoint_kind}/invoices/{path_suffix}"
    # Some endpoints require a type query param for purchase
    type_query = f"&type={invoice_type}" if invoice_type == "purchase" else ""
    full_url = (
        f"{export_url}?sort=tdlap:desc,khmshdon:asc,shdon:desc&search={search_params}{type_query}"
    )
    
    ttxly_part = f" ttxly={ttxly}" if ttxly is not None else ""
    flow_part = f" flow={flow_code}" if flow_code else ""
    activity.logger.info(f"🔄 Downloading Excel:{flow_part} {invoice_type} from {endpoint_kind}{ttxly_part}")
    
    # Build headers
    headers = _build_request_headers(session)
    
    # Retry logic
    for attempt in range(MAX_RETRIES):
        try:
            if attempt > 0:
                wait_time = 2 ** attempt
                activity.logger.info(f"⏳ Retry attempt {attempt + 1}/{MAX_RETRIES} after {wait_time}s...")
                await asyncio.sleep(wait_time)
            
            async with httpx.AsyncClient(
                cookies=session.cookies or {},
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
                verify=False,
            ) as client:
                response = await client.get(full_url)
                
                if response.status_code == 200:
                    # Check if response is Excel content
                    content_type = response.headers.get("content-type", "")
                    is_excel = (
                        "excel" in content_type.lower() or
                        "spreadsheet" in content_type.lower() or
                        response.content.startswith(b"PK")  # XLSX files are ZIP archives
                    )
                    
                    if not is_excel and len(response.content) < 1000:
                        activity.logger.warning(f"Response might not be Excel. Content-Type: {content_type}")
                        activity.logger.warning(f"Response preview: {response.content[:500]}")
                    
                    # Generate filename
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    ttxly_segment = f"_ttxly{ttxly}" if ttxly is not None else ""
                    flow_segment = f"{flow_code}_" if flow_code else ""
                    filename = f"gdt_export_{flow_segment}{invoice_type}_{endpoint_kind}{ttxly_segment}_{timestamp}.xlsx"
                    file_path = os.path.join(temp_dir, filename)
                    
                    # Save Excel file
                    with open(file_path, "wb") as f:
                        f.write(response.content)
                    
                    file_size_mb = len(response.content) / (1024 * 1024)
                    activity.logger.info(f"✅ Excel downloaded: {filename} ({file_size_mb:.2f} MB)")
                    
                    return file_path
                    
                elif response.status_code == 429:
                    activity.logger.warning(f"Rate limited (429) on attempt {attempt + 1}")
                    if attempt < MAX_RETRIES - 1:
                        wait_time = 10 * (attempt + 1)  # 10s, 20s, 30s
                        activity.logger.info(f"⏳ Rate limit recovery: Waiting {wait_time}s...")
                        await asyncio.sleep(wait_time)
                        continue
                    return None
                    
                elif response.status_code == 401:
                    activity.logger.error(f"Authentication failed (401) - Bearer token may be expired")
                    return None
                    
                else:
                    activity.logger.error(f"Unexpected status code: {response.status_code}")
                    activity.logger.error(f"Response: {response.text[:500]}")
                    if attempt == MAX_RETRIES - 1:
                        return None
                    continue
                    
        except httpx.TimeoutException:
            activity.logger.error(f"Request timeout on attempt {attempt + 1}")
            if attempt == MAX_RETRIES - 1:
                return None
            continue
            
        except Exception as e:
            activity.logger.error(f"Error during download attempt {attempt + 1}: {e}")
            if attempt == MAX_RETRIES - 1:
                return None
            continue
    
    return None


async def _parse_excel_files_to_invoices(
    excel_files: list[str],
    flows: list[str],
) -> list[GdtInvoice]:
    """Parse Excel files and convert to GdtInvoice objects."""
    
    try:
        import pandas as pd
    except ImportError:
        activity.logger.error("❌ pandas is required for Excel processing. Install with: pip install pandas openpyxl")
        raise GDTExcelDiscoveryError("pandas not available for Excel processing")
    
    all_invoices = []
    
    for file_path in excel_files:
        try:
            activity.logger.info(f"📖 Parsing Excel file: {os.path.basename(file_path)}")
            
            # Read Excel file
            df_raw = pd.read_excel(file_path, engine='openpyxl', header=None, dtype=str)
            
            # Find header row
            header_row = None
            for idx_row, row in df_raw.iterrows():
                row_str = ' '.join([str(v) for v in row if pd.notna(v)])
                if any(keyword in row_str.lower() for keyword in ['stt', 'ký hiệu', 'số hóa đơn', 'ngày lập', 'mst', 'tên người']):
                    header_row = idx_row
                    break
            
            if header_row is None:
                activity.logger.warning(f"No header row found in {os.path.basename(file_path)}")
                continue
            
            # Read with proper header
            df = pd.read_excel(file_path, engine='openpyxl', header=header_row, dtype=str)
            df = df.loc[:, ~df.columns.str.contains('^Unnamed')]
            df = df.dropna(how='all')
            
            # Standardize column names
            column_mapping = {
                'STT': 'stt',
                'Ký hiệu mẫu số': 'ky_hieu_mau_so',
                'Ký hiệu hóa đơn': 'ky_hieu_hoa_don',
                'Số hóa đơn': 'so_hoa_don',
                'Ngày lập': 'ngay_lap',
                'MST người bán/MST người xuất hàng': 'mst_nguoi_ban',
                'Tên người bán/Tên người xuất hàng': 'ten_nguoi_ban',
                'MST người mua/MST người nhận hàng': 'mst_nguoi_mua',
                'Tên người mua/Tên người nhận hàng': 'ten_nguoi_mua',
                'Tổng tiền chưa thuế': 'tong_tien_chua_thue',
                'Tổng tiền thuế': 'tong_tien_thue',
                'Tổng tiền thanh toán': 'tong_tien_thanh_toan',
                'Trạng thái hóa đơn': 'trang_thai_hoa_don',
            }
            
            df = df.rename(columns=column_mapping)
            
            # Convert to records
            records = df.to_dict('records')
            
            # Determine flow type from filename
            filename = os.path.basename(file_path).lower()
            # Derive flow type and endpoint from filename segments we wrote earlier
            # example: gdt_export_ban_ra_may_tinh_tien_sold_sco-query_ttxly5_20250101_120000.xlsx
            flow_type = 'ban_ra_dien_tu'  # default fallback
            if 'mua_vao_may_tinh_tien' in filename:
                flow_type = 'mua_vao_may_tinh_tien'
            elif 'mua_vao_dien_tu' in filename:
                flow_type = 'mua_vao_dien_tu'
            elif 'ban_ra_may_tinh_tien' in filename:
                flow_type = 'ban_ra_may_tinh_tien'
            elif 'ban_ra_dien_tu' in filename:
                flow_type = 'ban_ra_dien_tu'
            
            # Convert records to GdtInvoice objects
            file_invoices = []
            for record in records:
                try:
                    # Clean record data
                    cleaned_record = {}
                    for key, value in record.items():
                        if pd.isna(value):
                            cleaned_record[key] = None
                        elif isinstance(value, pd.Timestamp):
                            cleaned_record[key] = value.strftime("%Y-%m-%d")
                        else:
                            cleaned_record[key] = str(value) if value is not None else None
                    
                    # Skip empty records
                    if not any(v for v in cleaned_record.values() if v and str(v).strip()):
                        continue
                    
                    # Parse date
                    date_str = cleaned_record.get('ngay_lap', '')
                    try:
                        if date_str and '/' in date_str:
                            invoice_date = datetime.strptime(date_str, "%d/%m/%Y").strftime("%Y-%m-%d")
                        else:
                            invoice_date = datetime.now().strftime("%Y-%m-%d")
                    except Exception:
                        invoice_date = datetime.now().strftime("%Y-%m-%d")
                    
                    # Determine endpoint kind using filename for reliability
                    endpoint_kind = 'sco-query' if 'sco-query' in filename else 'query'

                    # Create GdtInvoice
                    invoice = GdtInvoice(
                        invoice_id=str(cleaned_record.get('stt', '')),
                        invoice_number=str(cleaned_record.get('so_hoa_don', '')),
                        invoice_date=invoice_date,
                        invoice_type=flow_type,
                        amount=float(cleaned_record.get('tong_tien_thanh_toan', 0) or 0),
                        tax_amount=float(cleaned_record.get('tong_tien_thue', 0) or 0),
                        supplier_name=str(cleaned_record.get('ten_nguoi_ban', '')),
                        supplier_tax_code=str(cleaned_record.get('mst_nguoi_ban', '')),
                        metadata={
                            "khhdon": str(cleaned_record.get('ky_hieu_hoa_don', '')),
                            "khmshdon": str(cleaned_record.get('ky_hieu_mau_so', 1)),
                            "buyer_name": cleaned_record.get('ten_nguoi_mua') or '',  # Don't convert None to "None"
                            "buyer_tax_code": cleaned_record.get('mst_nguoi_mua') or '',  # Don't convert None to "None"
                            "status": str(cleaned_record.get('trang_thai_hoa_don', '')),
                            "flow_type": flow_type,
                            "endpoint_kind": endpoint_kind,
                            "source": "excel_discovery",
                            "excel_file": os.path.basename(file_path),
                        },
                    )
                    
                    file_invoices.append(invoice)
                    
                except Exception as e:
                    activity.logger.warning(f"Failed to parse invoice record: {str(e)}")
                    continue
            
            all_invoices.extend(file_invoices)
            activity.logger.info(f"✅ Parsed {len(file_invoices)} invoices from {os.path.basename(file_path)}")
            
        except Exception as e:
            activity.logger.error(f"❌ Error parsing Excel file {file_path}: {str(e)}")
            continue
    
    activity.logger.info(f"📊 Total invoices parsed from Excel: {len(all_invoices)}")
    return all_invoices


def _build_request_headers(session: GdtSession) -> dict[str, str]:
    """Build HTTP headers for GDT API requests."""
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "vi",
        "Authorization": session.access_token,
        "Content-Type": "application/json",
        "Origin": "https://hoadondientu.gdt.gov.vn",
        "Referer": "https://hoadondientu.gdt.gov.vn/",
        "Host": "hoadondientu.gdt.gov.vn:30000",
        "End-Point": "/tra-cuu/tra-cuu-hoa-don",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }
