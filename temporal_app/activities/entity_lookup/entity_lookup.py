"""Company Lookup Activities - Extract data from GDT and enrich with thuvienphapluat.vn."""

import os
import time
import tempfile
import json
from datetime import datetime
from typing import Dict, Any, Optional

from temporalio import activity
from temporal_app.activities.entity_lookup.thuvien_activity import search_thuvienphapluat_activity
from temporalio.exceptions import ApplicationError
from app.config import settings
from temporal_app.activities.common.browser import launch_chromium_page

# ============================================================================
# Custom Exceptions
# ============================================================================

class CompanyLookupError(ApplicationError):
    """Retriable company lookup error."""
    def __init__(self, message: str):
        super().__init__(message, non_retryable=False)

class CompanyLookupInvalidDataError(ApplicationError):
    """Non-retriable company lookup error."""
    def __init__(self, message: str):
        super().__init__(message, non_retryable=True)

# ============================================================================
# Data Models
# ============================================================================

class CompanyDataRequest:
    """Request for company data lookup."""
    def __init__(self, search_term: str, search_type: str = "tax_code", list_mode: bool = False):
        self.search_term = search_term
        self.search_type = search_type
        self.list_mode = list_mode

class CompanyInfo:
    """Company information."""
    def __init__(self, tax_code: Optional[str] = None, company_name: Optional[str] = None,
                 address: Optional[str] = None, legal_representative: Optional[str] = None,
                 business_activities: Optional[list] = None, registration_date: Optional[str] = None,
                 status: Optional[str] = None, processing_time: Optional[float] = None):
        self.tax_code = tax_code
        self.company_name = company_name
        self.address = address
        self.legal_representative = legal_representative
        self.business_activities = business_activities or []
        self.registration_date = registration_date
        self.status = status
        self.processing_time = processing_time

class CompanyDataResponse:
    """Response for company data lookup."""
    def __init__(self, success: bool, message: str, data: Optional[CompanyInfo] = None,
                 companies: Optional[list[CompanyInfo]] = None, processing_time: float = 0.0,
                 errors: list[str] = None):
        self.success = success
        self.message = message
        self.data = data
        self.companies = companies
        self.processing_time = processing_time
        self.errors = errors or []

# ============================================================================
# Constants
# ============================================================================

CAPTCHA_EXTRACTION_PROMPT = """
This is a CAPTCHA image from a Vietnamese government website.
Please read and return ONLY the code (usually 5 characters, mix of normal case letters and numbers).
The code contains mostly normal case letters (not uppercase) and numbers.
Common characters include: a-z, A-Z, 0-9.
Do not return any explanation, just the code.
"""

# ============================================================================
# Activities
# ============================================================================

@activity.defn
async def extract_from_gdt(search_term: str, extraction_folder: str) -> Dict[str, Any]:
    """
    Extract company data from Vietnamese government tax portal.
    
    Args:
        search_term: Tax code to search for
        extraction_folder: Folder to save extraction data
        
    Returns:
        Dictionary containing extracted data or error information
        
    Raises:
        CompanyLookupError: If extraction fails (retriable)
        CompanyLookupInvalidDataError: If data is invalid (non-retriable)
    """
    activity.logger.info(f"Extracting data from GDT portal for tax code: {search_term}")

    # If caller didn't provide a folder, create a default one based on workflow run_id
    if not extraction_folder:
        run_id = activity.info().workflow_run_id or "unknown"
        extraction_folder = os.path.join(
            "/tmp",
            "entity_lookup",
            f"{run_id}_{search_term}",
        )
        os.makedirs(extraction_folder, exist_ok=True)
        activity.logger.info(f"Created default extraction folder: {extraction_folder}")
    
    try:
        # Use shared Playwright launcher (no BrowserManager)
        async with launch_chromium_page(headless=True) as (_, _, page):
            activity.logger.info("Setting up browser for GDT portal")

            # Navigate to GDT portal
            base_url = "https://tracuunnt.gdt.gov.vn/tcnnt/mstdn.jsp"
            await page.goto(base_url)
            await page.wait_for_timeout(3000)
            activity.logger.info(f"Navigated to: {base_url}")

            # Fill tax code
            await _fill_tax_code(page, search_term)

            # Handle CAPTCHA with AI
            captcha_success = await _solve_captcha_with_ai(page, extraction_folder, search_term)
            if not captcha_success:
                raise CompanyLookupError("CAPTCHA solving failed")

            # Take screenshot of results page
            results_screenshot = os.path.join(extraction_folder, "gdt_results.png")
            await page.screenshot(path=results_screenshot, full_page=True)
            activity.logger.info(f"GDT results screenshot: {results_screenshot}")

            # Extract the simple table
            table_data = await _extract_simple_table(page)

            # Save GDT results to JSON
            gdt_json_path = os.path.join(extraction_folder, "gdt_results.json")
            with open(gdt_json_path, 'w', encoding='utf-8') as f:
                json.dump(table_data, f, indent=2, ensure_ascii=False)
            activity.logger.info(f"GDT results saved: {gdt_json_path}")

            activity.logger.info("Successfully extracted GDT table data")
            return table_data

    except Exception as e:
        activity.logger.error(f"Error extracting from GDT: {e}")
        raise CompanyLookupError(f"GDT extraction failed: {str(e)}")

@activity.defn
async def enrich_with_thuvienphapluat(request: CompanyDataRequest, gdt_data: Dict[str, Any], 
                                     extraction_folder: str) -> Dict[str, Any]:
    """
    Enrich GDT data with thuvienphapluat.vn information.
    
    Args:
        request: Company lookup request
        gdt_data: Data extracted from GDT
        extraction_folder: Folder to save enrichment data
        
    Returns:
        Dictionary containing combined data
        
    Raises:
        CompanyLookupError: If enrichment fails (retriable)
    """
    activity.logger.info("Enriching with thuvienphapluat.vn data...")

    # Ensure folder exists even if not provided
    if not extraction_folder:
        run_id = activity.info().workflow_run_id or "unknown"
        term = getattr(request, "search_term", "unknown")
        extraction_folder = os.path.join(
            "/tmp",
            "entity_lookup",
            f"{run_id}_{term}",
        )
        os.makedirs(extraction_folder, exist_ok=True)
        activity.logger.info(f"Created default extraction folder: {extraction_folder}")
    
    try:
        # Functional crawler (no external class deps)
        enrichment_folder = os.path.join(extraction_folder, "enrichment")
        os.makedirs(enrichment_folder, exist_ok=True)
        thuvien_response = await search_thuvienphapluat_activity(
            {
                "search_term": request.search_term,
                "search_type": getattr(request, "search_type", "company_name") or "company_name",
                "list_mode": getattr(request, "list_mode", False),
            },
            enrichment_folder,
        )
        
        # Save thuvienphapluat results to JSON
        thuvien_json_path = os.path.join(extraction_folder, "thuvienphapluat_results.json")
        thuvien_data = {
            "success": thuvien_response.success,
            "message": thuvien_response.message,
            "data": _company_info_to_dict(thuvien_response.data) if thuvien_response.data else None,
            "companies": [_company_info_to_dict(c) for c in thuvien_response.companies] if thuvien_response.companies else None,
            "processing_time": thuvien_response.processing_time,
            "errors": thuvien_response.errors
        }
        with open(thuvien_json_path, 'w', encoding='utf-8') as f:
            json.dump(thuvien_data, f, indent=2, ensure_ascii=False)
        activity.logger.info(f"Thuvienphapluat results saved: {thuvien_json_path}")
        
        # Combine data (GDT takes priority)
        combined_data = {}
        
        # Start with thuvienphapluat data as base
        if thuvien_response.get("success") and thuvien_response.get("data"):
            combined_data = thuvien_response["data"]
            activity.logger.info("Added thuvienphapluat.vn enrichment data")
        
        # GDT data takes PRIORITY and OVERRIDES thuvienphapluat data
        if not gdt_data.get("error") and gdt_data.get("results"):
            gdt_result = gdt_data["results"][0]  # First result
            
            # OVERRIDE with GDT data
            if gdt_result.get("tax_code"):
                combined_data["tax_code"] = gdt_result["tax_code"]
            
            if gdt_result.get("company_name"):
                combined_data["company_name"] = gdt_result["company_name"]
            
            if gdt_result.get("address"):
                combined_data["address"] = gdt_result["address"]
            
            if gdt_result.get("status"):
                combined_data["status"] = gdt_result["status"]
            
            # Add GDT-specific fields
            if gdt_result.get("tax_authority"):
                combined_data["tax_authority"] = gdt_result["tax_authority"]
            
            activity.logger.info("Applied GDT data as PRIMARY source")
        elif gdt_data.get("error") and not combined_data:
            return gdt_data
        
        # Ensure tax_code is set
        combined_data["tax_code"] = request.search_term
        
        # Save final combined results to JSON
        combined_json_path = os.path.join(extraction_folder, "final_combined_results.json")
        with open(combined_json_path, 'w', encoding='utf-8') as f:
            json.dump(combined_data, f, indent=2, ensure_ascii=False)
        activity.logger.info(f"Final combined results saved: {combined_json_path}")
        
        activity.logger.info("Successfully combined GDT + thuvienphapluat data")
        return combined_data
        
    except Exception as e:
        activity.logger.error(f"Error enriching with thuvienphapluat: {e}")
        # If enrichment fails, return GDT data only
        return gdt_data

@activity.defn
async def search_by_company_name_only(request: CompanyDataRequest, extraction_folder: str) -> Dict[str, Any]:
    """
    Search by company name using ONLY thuvienphapluat.vn (no gov.gdt).
    
    Args:
        request: Company lookup request
        extraction_folder: Folder to save search data
        
    Returns:
        Dictionary containing search results
        
    Raises:
        CompanyLookupError: If search fails (retriable)
    """
    activity.logger.info(f"Searching company name '{request.search_term}' on thuvienphapluat.vn only")

    # Ensure folder exists even if not provided
    if not extraction_folder:
        run_id = activity.info().workflow_run_id or "unknown"
        term = getattr(request, "search_term", "unknown")
        extraction_folder = os.path.join(
            "/tmp",
            "entity_lookup",
            f"{run_id}_{term}",
        )
        os.makedirs(extraction_folder, exist_ok=True)
        activity.logger.info(f"Created default extraction folder: {extraction_folder}")
    
    try:
        # Functional crawler (no external class deps)
        thuvien_response = await search_thuvienphapluat_activity(
            {
                "search_term": request.search_term,
                "search_type": "company_name",
                "list_mode": getattr(request, "list_mode", False),
            },
            extraction_folder,
        )
        
        # Save thuvienphapluat results to JSON
        thuvien_json_path = os.path.join(extraction_folder, "thuvienphapluat_company_name_results.json")
        thuvien_data = {
            "success": thuvien_response.success,
            "message": thuvien_response.message,
            "data": _company_info_to_dict(thuvien_response.data) if thuvien_response.data else None,
            "companies": [_company_info_to_dict(c) for c in thuvien_response.companies] if thuvien_response.companies else None,
            "processing_time": thuvien_response.processing_time,
            "errors": thuvien_response.errors
        }
        with open(thuvien_json_path, 'w', encoding='utf-8') as f:
            json.dump(thuvien_data, f, indent=2, ensure_ascii=False)
        activity.logger.info(f"Thuvienphapluat company name results saved: {thuvien_json_path}")
        
        if thuvien_response.get("success"):
            if request.list_mode and thuvien_response.get("companies"):
                return {
                    "companies": thuvien_response["companies"],
                    "total_found": len(thuvien_response["companies"])
                }
            elif thuvien_response.get("data"):
                return thuvien_response["data"]
            else:
                return {"error": "No company data found"}
        else:
            return {"error": thuvien_response.message or "Search failed"}
            
    except Exception as e:
        activity.logger.error(f"Error searching by company name: {e}")
        raise CompanyLookupError(f"Company name search failed: {str(e)}")

# ============================================================================
# Helper Functions
# ============================================================================

async def _fill_tax_code(page, tax_code: str):
    """Fill the tax code input field."""
    try:
        tax_input = page.locator("input[name='mst']")
        await tax_input.fill(tax_code)
        activity.logger.info(f"Filled tax code: {tax_code}")
    except Exception as e:
        activity.logger.error(f"Error filling tax code: {e}")
        raise

async def _solve_captcha_with_ai(page, extraction_folder: str, search_term: str) -> bool:
    """Solve CAPTCHA using AI with retry logic."""
    max_retries = 3
    for attempt in range(max_retries):
        activity.logger.info(f"CAPTCHA attempt {attempt + 1}/{max_retries}")
        
        try:
            # Take screenshot
            screenshot_path = os.path.join(extraction_folder, f"captcha_attempt_{attempt + 1}.png")
            await page.screenshot(path=screenshot_path)
            
            # Extract and solve CAPTCHA
            captcha_code = await _extract_captcha_with_ai(screenshot_path)
            if not captcha_code:
                continue
            
            # Fill CAPTCHA
            captcha_input = page.locator("input[name='captcha']")
            await captcha_input.fill(captcha_code)
            activity.logger.info(f"Filled CAPTCHA: {captcha_code}")
            
            # Submit and check
            submit_btn = page.locator("input[type='button'][onclick='search();']")
            await submit_btn.click()
            await page.wait_for_timeout(2000)
            
            # Check for error
            page_content = await page.content()
            if "Vui lòng nhập đúng mã xác nhận!" in page_content:
                activity.logger.warning(f"CAPTCHA error on attempt {attempt + 1}")
                if attempt < max_retries - 1:
                    await page.reload()
                    await page.wait_for_timeout(3000)
                    await _fill_tax_code(page, search_term)
                    continue
            else:
                activity.logger.info(f"CAPTCHA solved on attempt {attempt + 1}")
                return True
                
        except Exception as e:
            activity.logger.error(f"CAPTCHA attempt {attempt + 1} failed: {e}")
            continue
    
    return False

async def _extract_captcha_with_ai(screenshot_path: str) -> Optional[str]:
    """Extract CAPTCHA using AI Vision."""
    try:
        # CAPTCHA coordinates for Vietnamese tax lookup site
        CAPTCHA_X = 670
        CAPTCHA_Y = 340
        CAPTCHA_WIDTH = 150
        CAPTCHA_HEIGHT = 50
        
        from PIL import Image
        img = Image.open(screenshot_path)
        captcha_region = img.crop((CAPTCHA_X, CAPTCHA_Y, CAPTCHA_X + CAPTCHA_WIDTH, CAPTCHA_Y + CAPTCHA_HEIGHT))
        
        captcha_path = screenshot_path.replace(".png", "_captcha.png")
        captcha_region.save(captcha_path)
        
        # Run OCR with Gemini and return the code
        return await _ocr_with_gemini(captcha_path)
        
    except Exception as e:
        activity.logger.error(f"Error extracting CAPTCHA: {e}")
        return None


async def _ocr_with_gemini(captcha_path: str) -> Optional[str]:
    """OCR using Gemini Vision with unified configuration and white background optimization."""
    try:
        from PIL import Image
        from google import genai
        
        # Load image
        img = Image.open(captcha_path)
        prompt = CAPTCHA_EXTRACTION_PROMPT.strip()

        # Ensure white background for better recognition (align with SVG flow)
        if img.mode in ('RGBA', 'LA', 'P'):
            white_bg = Image.new('RGB', img.size, 'white')
            if img.mode == 'P':
                img = img.convert('RGBA')
            white_bg.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
            img = white_bg
        elif img.mode != 'RGB':
            white_bg = Image.new('RGB', img.size, 'white')
            white_bg.paste(img)
            img = white_bg

        # Initialize Gemini client using settings
        if settings.CAPTCHA_USE_VERTEX and settings.GCP_PROJECT_ID:
            client = genai.Client(vertexai=True, project=settings.GCP_PROJECT_ID, location=settings.GCP_REGION)
        elif settings.GEMINI_API_KEY:
            client = genai.Client(api_key=settings.GEMINI_API_KEY)
        else:
            activity.logger.warning("No Gemini credentials configured")
            return None

        response = client.models.generate_content(
            model=settings.CAPTCHA_MODEL,
            contents=[img, prompt]
        )

        if response and hasattr(response, 'text') and response.text:
            captcha_code = response.text.strip().split("\n")[0]
            # Basic validation similar to gdt_auth
            if captcha_code and len(captcha_code) >= 5 and captcha_code.isalnum():
                activity.logger.info(f"Gemini read CAPTCHA: {captcha_code}")
                return captcha_code
            activity.logger.warning(f"Invalid CAPTCHA format from Gemini: '{captcha_code}'")
            return None

        activity.logger.error("Gemini returned empty response for CAPTCHA image")
        return None
        
    except Exception as e:
        activity.logger.error(f"Gemini OCR failed: {e}")
        return None

async def _extract_simple_table(page) -> Dict[str, Any]:
    """Extract the simple results table from the GDT results page."""
    try:
        activity.logger.info("Extracting simple results table...")
        
        # Find the specific table inside resultContainer with class ta_border
        table = page.locator('#resultContainer table.ta_border')
        
        table_count = await table.count()
        activity.logger.info(f"Found {table_count} resultContainer tables")
        
        if await table.count() == 0:
            activity.logger.warning("Results table not found")
            return {"error": "Results table not found"}

        # Extract table rows from tbody
        rows = table.locator("tbody tr")
        row_count = await rows.count()
        activity.logger.info(f"Found {row_count} rows in tbody")

        results = []
        for i in range(row_count):
            try:
                row = rows.nth(i)
                cells = row.locator("td")
                cell_count = await cells.count()
                
                activity.logger.info(f"Row {i}: {cell_count} cells")
                
                # Skip rows with no cells
                if cell_count == 0:
                    continue
                
                # Get first cell text to check if it's header
                first_cell_text = await cells.nth(0).text_content()
                first_cell_clean = first_cell_text.strip().upper() if first_cell_text else ""
                
                # Skip header row
                if first_cell_clean == "STT":
                    continue

                # Process data rows (should have 6 columns)
                if cell_count >= 6:
                    # Extract company name (may be in a div)
                    company_name_cell = cells.nth(2)
                    company_name_div = company_name_cell.locator("div")
                    if await company_name_div.count() > 0:
                        company_name = await company_name_div.text_content()
                    else:
                        company_name = await company_name_cell.text_content()
                    
                    row_data = {
                        "stt": (await cells.nth(0).text_content()).strip(),
                        "tax_code": (await cells.nth(1).text_content()).strip(),
                        "company_name": company_name.strip() if company_name else "",
                        "address": (await cells.nth(3).text_content()).strip(),
                        "tax_authority": (await cells.nth(4).text_content()).strip(),
                        "status": (await cells.nth(5).text_content()).strip(),
                    }
                    results.append(row_data)
                    activity.logger.info(f"Extracted row {i}: {row_data['company_name']}")
                else:
                    activity.logger.warning(f"Row {i} has only {cell_count} cells, skipping")
                    
            except Exception as e:
                activity.logger.error(f"Error processing row {i}: {e}")
                continue

        return {"results": results, "total_found": len(results)}

    except Exception as e:
        activity.logger.error(f"Error extracting table: {e}")
        return {"error": str(e)}

def _company_info_to_dict(company_info: CompanyInfo) -> Dict[str, Any]:
    """Convert CompanyInfo object to dictionary."""
    return {
        "tax_code": company_info.tax_code,
        "company_name": company_info.company_name,
        "address": company_info.address,
        "legal_representative": company_info.legal_representative,
        "business_activities": company_info.business_activities or [],
        "registration_date": company_info.registration_date,
        "status": company_info.status,
    }

