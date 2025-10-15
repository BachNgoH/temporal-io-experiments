"""GDT invoice fetching activities - Real implementation."""

import asyncio
import httpx
import json
import os
import tempfile
import zipfile
from datetime import datetime, timedelta
from typing import Optional
from temporalio import activity
from temporal_app.activities.hooks import emit_on_complete

from temporal_app.models import GdtInvoice, GdtSession, InvoiceFetchResult

# GCS imports
try:
    from google.cloud import storage
except ImportError:
    storage = None

# ============================================================================
# GDT Invoice Detail URLs
# ============================================================================
GDT_BASE_URL = "https://hoadondientu.gdt.gov.vn:30000"
GDT_DETAIL_URL = f"{GDT_BASE_URL}/query/invoices/detail"
GDT_DETAIL_SCO_URL = f"{GDT_BASE_URL}/sco-query/invoices/detail"

# XML Export URLs
GDT_XML_EXPORT_URL = f"{GDT_BASE_URL}/query/invoices/export-xml"
GDT_XML_EXPORT_SCO_URL = f"{GDT_BASE_URL}/sco-query/invoices/export-xml"

# ============================================================================
# Configuration
# ============================================================================
REQUEST_TIMEOUT_SECONDS = 30.0
XML_DOWNLOAD_MAX_RETRIES = 3
GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME", "gdt-invoice-xmls")


# ============================================================================
# Helper Functions
# ============================================================================

async def _download_invoice_xml(
    invoice: GdtInvoice,
    session: GdtSession,
    endpoint_kind: str
) -> Optional[str]:
    """
    Download XML file for a specific invoice.
    
    Args:
        invoice: GdtInvoice with invoice parameters
        session: GdtSession with authentication
        endpoint_kind: "query" or "sco-query" to determine endpoint
        
    Returns:
        Path to downloaded XML file or None if failed
    """
    try:
        # Extract required parameters from invoice metadata
        nbmst = invoice.supplier_tax_code or invoice.metadata.get("nbmst", "")
        khhdon = invoice.metadata.get("khhdon", "")
        shdon = invoice.invoice_number
        khmshdon = invoice.metadata.get("khmshdon", "1")

        if not all([nbmst, khhdon, shdon]):
            activity.logger.warning(
                f"Missing required parameters for XML download: nbmst={nbmst}, khhdon={khhdon}, shdon={shdon}"
            )
            return None

        # Build export-xml URL based on endpoint kind
        if endpoint_kind == "sco-query":
            export_url = GDT_XML_EXPORT_SCO_URL
        else:
            export_url = GDT_XML_EXPORT_URL

        # Build parameters for XML export
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

        activity.logger.info(f"📄 Downloading XML: {khhdon}-{shdon} from {export_url}")

        async with httpx.AsyncClient(
            cookies=session.cookies,
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
            verify=False,
        ) as client:
            response = await client.get(export_url, params=params)

            if response.status_code == 200:
                # Create filename using invoice_code and invoice_number
                invoice_code = invoice.metadata.get("khhdon", "unknown")
                invoice_number = invoice.invoice_number
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

                # Check if response is a ZIP file
                content_type = response.headers.get("content-type", "")
                if "zip" in content_type.lower() or response.content.startswith(b"PK"):
                    # Handle ZIP file extraction
                    activity.logger.info(f"📦 Received ZIP file for {invoice_code}-{invoice_number}, extracting...")

                    try:
                        # Save ZIP to temporary file
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as temp_zip:
                            temp_zip.write(response.content)
                            temp_zip_path = temp_zip.name

                        # Extract ZIP file
                        with zipfile.ZipFile(temp_zip_path, "r") as zip_ref:
                            # List all files in the ZIP
                            zip_files = zip_ref.namelist()
                            activity.logger.info(f"📦 ZIP contains files: {zip_files}")

                            xml_file_path = None
                            for file_name in zip_files:
                                if file_name.lower().endswith(".xml"):
                                    # Extract XML file
                                    xml_content = zip_ref.read(file_name)

                                    # Create final XML filename using invoice_code and invoice_number
                                    xml_filename = f"{invoice_code}_{invoice_number}_{timestamp}.xml"
                                    xml_file_path = os.path.join(tempfile.gettempdir(), xml_filename)

                                    # Save extracted XML content
                                    with open(xml_file_path, "wb") as f:
                                        f.write(xml_content)

                                    activity.logger.success(f"✅ Extracted XML from ZIP: {xml_filename}")
                                    break

                            # Clean up temporary ZIP file
                            os.unlink(temp_zip_path)

                            if xml_file_path:
                                return xml_file_path
                            else:
                                activity.logger.warning(f"No XML file found in ZIP for {invoice_code}-{invoice_number}")
                                return None

                    except zipfile.BadZipFile:
                        activity.logger.error(f"Invalid ZIP file received for {invoice_code}-{invoice_number}")
                        return None
                    except Exception as e:
                        activity.logger.error(f"Error extracting ZIP for {invoice_code}-{invoice_number}: {e}")
                        return None

                else:
                    # Handle direct XML response (fallback)
                    xml_filename = f"{invoice_code}_{invoice_number}_{timestamp}.xml"
                    xml_file_path = os.path.join(tempfile.gettempdir(), xml_filename)

                    # Save XML content directly
                    with open(xml_file_path, "wb") as f:
                        f.write(response.content)

                    activity.logger.success(f"✅ Downloaded XML: {xml_filename}")
                    return xml_file_path

            else:
                activity.logger.error(
                    f"❌ Failed to download XML for {invoice_code}-{invoice_number}: {response.status_code}"
                )
                activity.logger.error(f"Response: {response.text[:500]}")
                return None

    except Exception as e:
        activity.logger.error(
            f"Error downloading XML for invoice {invoice.invoice_id}: {e}"
        )
        return None


async def _download_invoice_xml_with_retry(
    invoice: GdtInvoice,
    session: GdtSession,
    endpoint_kind: str,
    max_retries: int = XML_DOWNLOAD_MAX_RETRIES,
) -> Optional[str]:
    """
    Download XML file with retry logic for failed downloads.
    
    Args:
        invoice: GdtInvoice with invoice parameters
        session: GdtSession with authentication
        endpoint_kind: "query" or "sco-query" to determine endpoint
        max_retries: Maximum number of retry attempts
        
    Returns:
        Path to downloaded XML file or None if failed
    """
    invoice_code = invoice.metadata.get("khhdon", "unknown")
    invoice_number = invoice.invoice_number

    for attempt in range(max_retries):
        try:
            activity.logger.info(
                f"📄 Downloading XML {invoice_code}-{invoice_number} (attempt {attempt + 1}/{max_retries})"
            )

            xml_path = await _download_invoice_xml(invoice, session, endpoint_kind)

            if xml_path:
                if attempt > 0:
                    activity.logger.success(
                        f"✅ Successfully downloaded {invoice_code}-{invoice_number} on retry attempt {attempt + 1}"
                    )
                return xml_path

            # If first attempt fails, wait before retry
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 2  # Exponential backoff: 2s, 4s, 6s
                activity.logger.info(f"⏳ Waiting {wait_time}s before retry {attempt + 2}...")
                await asyncio.sleep(wait_time)

        except Exception as e:
            activity.logger.error(f"❌ Attempt {attempt + 1} failed for {invoice_code}-{invoice_number}: {e}")
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 2
                activity.logger.info(f"⏳ Waiting {wait_time}s before retry {attempt + 2}...")
                await asyncio.sleep(wait_time)

    activity.logger.error(f"🔴 Failed to download XML for {invoice_code}-{invoice_number} after {max_retries} attempts")
    return None


async def _upload_xml_to_gcs(
    xml_file_path: str,
    invoice: GdtInvoice,
    session: GdtSession
) -> Optional[str]:
    """
    Upload XML file to GCS and return the GCS URL.
    
    Args:
        xml_file_path: Local path to XML file
        invoice: GdtInvoice for metadata
        session: GdtSession for company_id
        
    Returns:
        GCS URL of uploaded file or None if failed
    """
    if not storage:
        activity.logger.warning("Google Cloud Storage not available - skipping GCS upload")
        return None

    if not GCS_BUCKET_NAME:
        activity.logger.warning("No GCS bucket name configured - skipping GCS upload")
        return None

    try:
        # Verify file exists and has content
        if not os.path.exists(xml_file_path):
            activity.logger.warning(f"XML file does not exist: {xml_file_path}")
            return None

        file_size = os.path.getsize(xml_file_path)
        if file_size == 0:
            activity.logger.warning(f"Skipping empty XML file: {xml_file_path}")
            return None

        # Create GCS folder path
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        company_id = session.company_id
        invoice_code = invoice.metadata.get("khhdon", "unknown")
        invoice_number = invoice.invoice_number
        
        # Create GCS blob path using invoice_code and invoice_number
        filename = f"{invoice_code}_{invoice_number}_{timestamp}.xml"
        blob_path = f"gdt-invoices/{company_id}/{timestamp}/{filename}"

        # Initialize GCS client
        client = storage.Client()
        bucket = client.bucket(GCS_BUCKET_NAME)

        # Upload file with timeout handling
        blob = bucket.blob(blob_path)
        blob.upload_from_filename(xml_file_path, timeout=60)

        gcs_url = f"gs://{GCS_BUCKET_NAME}/{blob_path}"
        activity.logger.success(f"✅ Uploaded XML to GCS: {gcs_url}")

        # Clean up local file
        try:
            os.unlink(xml_file_path)
            activity.logger.debug(f"Cleaned up local XML file: {xml_file_path}")
        except Exception as cleanup_error:
            activity.logger.warning(f"Failed to clean up local file {xml_file_path}: {cleanup_error}")

        return gcs_url

    except Exception as e:
        activity.logger.error(f"❌ Failed to upload XML to GCS: {e}")
        return None


# ============================================================================
# Main Activity
# ============================================================================
@activity.defn
@emit_on_complete(
    event_name="fetch.completed",
)
async def fetch_invoice(
    invoice: GdtInvoice,
    session: GdtSession,
) -> InvoiceFetchResult:
    """
    Fetch detailed invoice JSON from GDT portal and download XML file.

    Flow:
    1. Build detail API URL with invoice parameters
    2. Download invoice JSON with full details including line items (hdhhdvu)
    3. Download XML file for the invoice with retry logic
    4. Upload XML file to GCS and return GCS URL
    5. Return invoice data with JSON content and XML GCS URL

    Rate Limiting Strategy:
    - Let GDT handle their own rate limiting (429 responses)
    - Temporal automatically retries with exponential backoff
    - No manual rate limiting - trust GDT's rate limit headers

    Args:
        invoice: GdtInvoice with invoice_id, metadata (khhdon, nbmst, etc.)
        session: GdtSession with bearer token and cookies

    Returns:
        InvoiceFetchResult with JSON content, XML GCS URL, and download status
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

                        # Download XML file and upload to GCS
                        xml_gcs_url = None
                        xml_download_status = "not_attempted"
                        
                        # try:
                        #     activity.logger.info(f"📄 Starting XML download for invoice {invoice.invoice_id}")
                            
                        #     # Download XML with retry logic
                        #     xml_file_path = await _download_invoice_xml_with_retry(
                        #         invoice, session, endpoint_kind
                        #     )
                            
                        #     if xml_file_path:
                        #         # Upload to GCS
                        #         xml_gcs_url = await _upload_xml_to_gcs(xml_file_path, invoice, session)
                                
                        #         if xml_gcs_url:
                        #             xml_download_status = "success"
                        #             activity.logger.success(f"✅ XML successfully downloaded and uploaded to GCS: {xml_gcs_url}")
                        #         else:
                        #             xml_download_status = "failed"
                        #             activity.logger.warning(f"⚠️ XML downloaded but GCS upload failed for invoice {invoice.invoice_id}")
                        #     else:
                        #         xml_download_status = "failed"
                        #         activity.logger.warning(f"⚠️ XML download failed for invoice {invoice.invoice_id}")
                                
                        # except Exception as xml_error:
                        #     xml_download_status = "failed"
                        #     activity.logger.error(f"❌ XML download/upload error for invoice {invoice.invoice_id}: {xml_error}")

                        # Prepare metadata without status field
                        invoice_metadata = getattr(invoice, "metadata", {}).copy()
                        invoice_metadata.pop("status", None)  # Exclude status from webhook payload

                        # Return typed result; flatten invoice_detail into top level
                        return InvoiceFetchResult(
                            invoice_id=invoice.invoice_id,
                            success=True,
                            data={
                                "company_id": getattr(session, "company_id", None),
                                "invoice_id": invoice.invoice_id,
                                "invoice_number": invoice.invoice_number,
                                "line_items": line_items,
                                "metadata": invoice_metadata,
                                **invoice_detail,  # Flatten invoice_detail fields to top level
                            },
                            xml_gcs_url=xml_gcs_url,
                            xml_download_status=xml_download_status,
                        )
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
