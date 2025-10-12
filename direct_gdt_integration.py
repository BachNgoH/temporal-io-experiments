"""
Direct API Crawler for Vietnamese Tax Portal
Makes direct API calls to capture invoice data without UI interaction
"""

import asyncio
import json
import os
import zipfile
import tempfile
from datetime import datetime, date
from typing import List, Dict, Any, Optional
from loguru import logger
from playwright.async_api import Page
import httpx

try:
    from google.cloud import storage
except ImportError:
    storage = None
    logger.warning("Google Cloud Storage not available - GCS upload disabled")

from app.schemas.tax_services import InvoiceFlow
from app.core.config import settings
from app.services.langfuse_service import langfuse_trace_async, langfuse_span_async
from app.core.rate_limiter import rate_limit_gdt_requests, rate_limited_gather


class DirectAPICrawler:
    """Direct API crawler that captures session and makes API calls"""

    def __init__(self, page: Page, extraction_folder: str, session_cookies=None, session_headers=None, username: str = None):
        self.page = page
        self.extraction_folder = extraction_folder
        self.session_cookies = session_cookies
        self.session_headers = session_headers
        self.username = username  # Store username for rate limiting

    @langfuse_trace_async(
        name="extract_session_info",
        metadata={"component": "session_management", "operation": "extract_cookies_and_token"}
    )
    async def extract_session_info(self) -> bool:
        """Extract session cookies, headers, and Bearer token from the logged-in page"""
        try:
            # If session info is already provided, skip extraction
            if self.session_cookies and self.session_headers and "Authorization" in self.session_headers:
                logger.info("🔄 Using pre-captured session info")
                return True
            
            # If no page is available (e.g., in V2 implementation), skip extraction
            if not self.page:
                logger.info("🔄 No page available, using provided session info")
                return bool(self.session_cookies and self.session_headers)
            
            # Get all cookies from the current context
            cookies = await self.page.context.cookies()

            # Convert cookies to a format suitable for httpx
            self.session_cookies = {}
            for cookie in cookies:
                if "hoadondientu.gdt.gov.vn" in cookie.get("domain", ""):
                    self.session_cookies[cookie["name"]] = cookie["value"]

            # Try to capture Bearer token from network requests
            bearer_token = await self._capture_bearer_token()

            # Headers based on the actual tax portal request format
            self.session_headers = {
                "Accept": "application/json, text/plain, */*",
                "Accept-Encoding": "gzip, deflate, br, zstd",
                "Accept-Language": "vi",  # Vietnamese language as shown in the request
                "Connection": "keep-alive",
                "Host": "hoadondientu.gdt.gov.vn:30000",
                "Origin": "https://hoadondientu.gdt.gov.vn",
                "Referer": "https://hoadondientu.gdt.gov.vn/",
                "Sec-Ch-Ua": '"Not)A;Brand";v="8", "Chromium";v="138", "Microsoft Edge";v="138"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"macOS"',
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-site",
                "User-Agent": await self.page.evaluate("() => navigator.userAgent"),
                "End-Point": "/tra-cuu/tra-cuu-hoa-don",  # Custom header from tax portal
            }

            # Add Bearer token if captured
            if bearer_token:
                self.session_headers["Authorization"] = f"Bearer {bearer_token}"
                logger.success(f"Captured Bearer token: {bearer_token[:50]}...")
            else:
                logger.warning("No Bearer token captured - API requests may fail")

            logger.info(f"Extracted {len(self.session_cookies)} session cookies")
            logger.info(f"Session cookies: {list(self.session_cookies.keys())}")
            logger.info(f"Headers configured with {len(self.session_headers)} fields")

            return len(self.session_cookies) > 0 or bearer_token is not None

        except Exception as e:
            logger.error(f"Error extracting session info: {e}")
            return False

    @langfuse_span_async(name="capture_bearer_token")
    async def _capture_bearer_token(self) -> Optional[str]:
        """Capture Bearer token by triggering actual search and monitoring network requests"""
        try:
            # Method 1: Try to get token from localStorage first
            try:
                token = await self.page.evaluate("""
                    () => {
                        // Check common token storage locations including window variables
                        const locations = [
                            'token',
                            'authToken',
                            'auth_token',
                            'access_token',
                            'bearer_token',
                            'jwt_token',
                            'Authorization'
                        ];

                        // Check localStorage and sessionStorage
                        for (const key of locations) {
                            const value = localStorage.getItem(key) || sessionStorage.getItem(key);
                            if (value) {
                                return value;
                            }
                        }

                        // Check window variables
                        if (window.token) return window.token;
                        if (window.authToken) return window.authToken;
                        if (window.access_token) return window.access_token;
                        
                        // Check if there's any variable containing 'Bearer'
                        for (let key in localStorage) {
                            const val = localStorage.getItem(key);
                            if (val && val.includes('Bearer ')) {
                                return val;
                            }
                        }

                        return null;
                    }
                """)

                if token:
                    logger.info("Bearer token found in browser storage")
                    clean_token = token.replace("Bearer ", "").strip()
                    # Validate token format (should be base64-like)
                    if len(clean_token) > 20:
                        return clean_token
                    else:
                        logger.warning(f"Token seems too short: {len(clean_token)} chars")

            except Exception as e:
                logger.warning(f"Could not access browser storage: {e}")

            # Method 2: Multiple attempts to trigger search and capture token
            captured_token = {"value": None}
            request_count = {"count": 0}
            
            # Enhanced request capture with better detection
            async def capture_request(request):
                request_count["count"] += 1
                auth_header = request.headers.get("authorization")
                
                # Log all API requests for debugging
                if "hoadondientu.gdt.gov.vn:30000" in request.url:
                    logger.info(f"API request to: {request.url}")
                    logger.info(f"Headers: {dict(request.headers)}")
                
                if auth_header and auth_header.startswith("Bearer "):
                    captured_token["value"] = auth_header.replace("Bearer ", "").strip()
                    logger.success(f"✅ Bearer token captured from API request: {request.url}")
                    logger.info(f"Token preview: {captured_token['value'][:50]}...")
                elif "hoadondientu.gdt.gov.vn:30000" in request.url:
                    logger.warning(f"⚠️  API request without Bearer token: {request.url}")

            # Listen for requests
            self.page.on("request", capture_request)

            # Try multiple methods to trigger API calls
            for method_num in range(1, 4):  # 3 different methods
                if captured_token["value"]:
                    break
                    
                logger.info(f"🔍 Bearer token capture method {method_num}/3")
                
                try:
                    if method_num == 1:
                        # Method 1: Navigate to search page and wait for initial load
                        current_url = self.page.url
                        logger.info(f"Current URL: {current_url}")
                        if "tra-cuu-hoa-don" not in current_url:
                            logger.info("Navigating to invoice search page...")
                            await self.page.goto("https://hoadondientu.gdt.gov.vn/tra-cuu/tra-cuu-hoa-don")
                            await self.page.wait_for_timeout(5000)  # Longer wait for page load
                        
                        # Just wait for any network activity from page load
                        await self.page.wait_for_timeout(3000)
                        
                    elif method_num == 2:
                        # Method 2: Try to trigger search by filling forms
                        logger.info("Attempting to fill date fields and trigger search...")
                        from datetime import date, timedelta
                        
                        today = date.today()
                        yesterday = today - timedelta(days=1)
                        
                        # Try multiple date field selectors
                        date_selectors = [
                            "input[placeholder*='Từ ngày']",
                            "input[placeholder*='từ ngày']", 
                            "input[name*='fromDate']",
                            "input[id*='fromDate']",
                            ".ant-picker-input input",
                            "[class*='date'] input"
                        ]
                        
                        filled_date = False
                        for selector in date_selectors:
                            try:
                                date_field = self.page.locator(selector).first
                                if await date_field.count() > 0:
                                    await date_field.clear()
                                    await date_field.fill(yesterday.strftime("%d/%m/%Y"))
                                    filled_date = True
                                    logger.info(f"Filled date field with selector: {selector}")
                                    break
                            except:
                                continue
                        
                        if filled_date:
                            await self.page.wait_for_timeout(1000)
                            
                            # Try to find and click search button
                            search_selectors = [
                                "button:has-text('Tìm kiếm')",
                                "button[type='submit']",
                                ".ant-btn-primary",
                                "button.ant-btn"
                            ]
                            
                            for selector in search_selectors:
                                try:
                                    search_btn = self.page.locator(selector).first
                                    if await search_btn.count() > 0:
                                        await search_btn.click()
                                        logger.info(f"Clicked search button: {selector}")
                                        await self.page.wait_for_timeout(5000)
                                        break
                                except:
                                    continue
                        
                    elif method_num == 3:
                        # Method 3: Page refresh to trigger network activity
                        logger.info("🔄 Page refresh to trigger network requests...")
                        await self.page.reload()
                        await self.page.wait_for_timeout(5000)
                        
                except Exception as e:
                    logger.warning(f"Method {method_num} failed: {e}")
                    
                # Check if we got token after this method
                if captured_token["value"]:
                    logger.success(f"✅ Bearer token captured using method {method_num}!")
                    break
                else:
                    logger.warning(f"Method {method_num} did not capture Bearer token")

            # Remove listener
            self.page.remove_listener("request", capture_request)

            logger.info(f"Total network requests monitored: {request_count['count']}")
            
            if not captured_token["value"]:
                logger.error("❌ Failed to capture Bearer token with all methods")
                # Try one more method - check if there are any tokens in the page source
                try:
                    page_content = await self.page.content()
                    import re
                    # Look for JWT-like patterns in the page
                    jwt_pattern = r'eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+'
                    matches = re.findall(jwt_pattern, page_content)
                    if matches:
                        logger.info(f"Found potential JWT token in page source")
                        return matches[0]
                except Exception as e:
                    logger.warning(f"Could not search page source for tokens: {e}")

            return captured_token["value"]

        except Exception as e:
            logger.error(f"Error capturing Bearer token: {e}")
            return None

    @langfuse_trace_async(
        name="make_api_request",
        metadata={"component": "api_client", "operation": "tax_portal_api_call"}
    )
    async def make_api_request(
        self, endpoint_url: str, flow_name: str, start_date: date, end_date: date, max_retries: int = 3
    ) -> Optional[Dict[str, Any]]:
        """Make API request to the tax portal endpoint with retry logic for ALL ttxly values [5, 6, 8]"""
        
        # Format dates for the API (use the correct format from the actual request)
        start_date_str = start_date.strftime("%d/%m/%Y")
        end_date_str = end_date.strftime("%d/%m/%Y")

        # Base search parameters
        base_search_params = f"tdlap=ge={start_date_str}T00:00:00;tdlap=le={end_date_str}T23:59:59"
        
        # Define ttxly values to crawl - ALL processing statuses [5, 6, 8]
        ttxly_values = [5, 6, 8] if "purchase" in endpoint_url else [None]  # Only for purchase invoices

        all_combined_data = {"datas": [], "total": 0}

        # Optimized: Process all ttxly values in parallel using asyncio.gather()
        logger.info(f"🚀 Processing {len(ttxly_values)} ttxly values in PARALLEL for {flow_name}")

        async def fetch_ttxly_data(ttxly):
            """Helper function to fetch data for a single ttxly value"""
            if ttxly is not None:
                logger.info(f"🔄 Making API request for {flow_name} with ttxly={ttxly}")
                search_params = f"{base_search_params};ttxly=={ttxly}"
            else:
                logger.info(f"🔄 Making API request for {flow_name} (no ttxly filter)")
                search_params = base_search_params

            # Build full URL with parameters
            full_url = f"{endpoint_url}?sort=tdlap:desc,khmshdon:asc,shdon:desc&search={search_params}"

            # Make request with retry logic for this specific ttxly value
            ttxly_data = await self._make_single_api_request(full_url, flow_name, ttxly, max_retries)

            if ttxly_data and ttxly_data.get("datas"):
                logger.success(f"✅ Found {len(ttxly_data['datas'])} invoices for {flow_name} with ttxly={ttxly}")
                return ttxly_data
            else:
                logger.warning(f"⚠️ No data for {flow_name} with ttxly={ttxly}")
                return None

        # Execute all ttxly requests in parallel
        results = await asyncio.gather(*[fetch_ttxly_data(ttxly) for ttxly in ttxly_values])

        # Combine all results
        for result in results:
            if result and result.get("datas"):
                all_combined_data["datas"].extend(result["datas"])
                all_combined_data["total"] += len(result["datas"])

        # Return combined results or None if no data found
        if all_combined_data["datas"]:
            logger.success(f"✅ Combined total: {len(all_combined_data['datas'])} invoices for {flow_name}")
            return all_combined_data
        else:
            logger.warning(f"⚠️ No data found for {flow_name} across all ttxly values")
            return None

    @langfuse_span_async(name="make_single_api_request")
    async def _make_single_api_request(
        self, full_url: str, flow_name: str, ttxly: Optional[int], max_retries: int = 3
    ) -> Optional[Dict[str, Any]]:
        """Make a single API request with retry logic and pagination support using state parameter"""
        
        all_data = {"datas": [], "total": 0}
        page = 0
        state_token = None  # Vietnamese tax portal uses state parameter for pagination
        page_size = 50  # Optimized: Moderate page size for better performance (100 caused 500 errors)
        
        full_url += f"&size={page_size}"
        
        while True:
            # Build paginated URL - first page doesn't need state parameter
            if state_token:
                # For subsequent pages, add the state parameter
                paginated_url = f"{full_url}&state={state_token}"
            else:
                # First page - use the original URL as is
                paginated_url = full_url
            
            # Make request with retry logic for this page
            page_data = await self._make_single_page_request(paginated_url, flow_name, ttxly, page, max_retries)
            
            if not page_data or not page_data.get("datas"):
                # No more data or error occurred
                break
                
            # Add this page's data to combined results
            current_page_count = len(page_data["datas"])
            all_data["datas"].extend(page_data["datas"])
            all_data["total"] = page_data.get("total", len(all_data["datas"]))
            
            ttxly_suffix = f" (ttxly={ttxly})" if ttxly is not None else ""
            logger.info(f"📄 Page {page}: {current_page_count} invoices for {flow_name}{ttxly_suffix}")
            
            # Extract state token for next page from response
            state_token = page_data.get("state")  # Vietnamese tax portal returns state for next page
            
            if state_token:
                logger.info(f"🔗 Next page state token: {state_token[:50]}..." if len(state_token) > 50 else f"🔗 Next page state token: {state_token}")
            else:
                logger.info("🔚 No state token - likely last page")
            
            # Check pagination termination conditions
            if (
                len(all_data["datas"]) >= all_data["total"] or  # Have all data
                current_page_count < page_size or  # Last page (not full)
                not state_token  # No more pages (no state token)
            ):
                break
                
            page += 1
            
            # Safety limit to prevent infinite loops
            if page >= 100:  # Max 100 pages
                logger.warning(f"⚠️ Reached maximum page limit (100) for {flow_name}{ttxly_suffix}")
                break
                
            # Small delay between pages to be respectful to the server (optimized)
            await asyncio.sleep(0.1)
        
        if all_data["datas"]:
            ttxly_suffix = f" (ttxly={ttxly})" if ttxly is not None else ""
            logger.success(f"✅ Pagination complete for {flow_name}{ttxly_suffix}: {len(all_data['datas'])} total invoices across {page + 1} pages")
            return all_data
        else:
            return None

    @langfuse_span_async(name="make_single_page_request")
    @rate_limit_gdt_requests("self")
    async def _make_single_page_request(
        self, paginated_url: str, flow_name: str, ttxly: Optional[int], page: int, max_retries: int = 3
    ) -> Optional[Dict[str, Any]]:
        """Make a single page API request with retry logic"""
        
        for attempt in range(max_retries):
            try:
                ttxly_suffix = f" (ttxly={ttxly})" if ttxly is not None else ""
                attempt_msg = f"Attempt {attempt + 1}/{max_retries}" if max_retries > 1 else ""
                logger.info(f"🔄 API request for {flow_name}{ttxly_suffix} {attempt_msg}")

                logger.info(f"Making API request to: {paginated_url}")
                logger.info(f"Using headers: {list(self.session_headers.keys())}")
                logger.info(
                    f"Authorization header present: {'Authorization' in self.session_headers}"
                )

                # Create custom headers for this specific request
                request_headers = self.session_headers.copy()

                # Add the action header based on flow type (URL encoded Vietnamese text)
                action_map = {
                    "ban_ra_dien_tu": "T%C3%ACm%20ki%E1%BA%BFm%20(h%C3%B3a%20%C4%91%C6%A1n%20%C4%91i%E1%BB%87n%20t%E1%BB%AD%20b%C3%A1n%20ra)",
                    "ban_ra_may_tinh_tien": "T%C3%ACm%20ki%E1%BA%BFm%20(h%C3%B3a%20%C4%91%C6%A1n%20m%C3%A1y%20t%C3%ADnh%20ti%E1%BB%81n%20b%C3%A1n%20ra)",
                    "mua_vao_dien_tu": "T%C3%ACm%20ki%E1%BA%BFm%20(h%C3%B3a%20%C4%91%C6%A1n%20%C4%91i%E1%BB%87n%20t%E1%BB%AD%20mua%20v%C3%A0o)",
                    "mua_vao_may_tinh_tien": "T%C3%ACm%20ki%E1%BA%BFm%20(h%C3%B3a%20%C4%91%C6%A1n%20m%C3%A1y%20t%C3%ADnh%20ti%E1%BB%81n%20mua%20v%C3%A0o)",
                }

                if flow_name in action_map:
                    request_headers["Action"] = action_map[flow_name]

                async with httpx.AsyncClient(
                    cookies=self.session_cookies or {},
                    headers=request_headers,
                    timeout=30.0,
                    verify=False,  # nosec B501 # Skip SSL verification for government portal
                ) as client:
                    cookie_count = len(self.session_cookies) if self.session_cookies else 0
                    logger.info(
                        f"Making GET request with {cookie_count} cookies"
                    )
                    response = await client.get(paginated_url)

                    logger.info(f"Response status: {response.status_code}")
                    logger.info(f"Response headers: {dict(response.headers)}")

                    if response.status_code == 200:
                        try:
                            data = response.json()
                            ttxly_suffix = f" (ttxly={ttxly})" if ttxly is not None else ""
                            logger.success(f"✅ API request successful for {flow_name}{ttxly_suffix}")
                            logger.info(f"Response data keys: {list(data.keys())}")
                            logger.info(
                                f"Invoices found: {len(data.get('datas', []))}, total: {data.get('total', 0)}"
                            )

                            # Log some sample data if available
                            if data.get("datas") and len(data["datas"]) > 0:
                                sample = data["datas"][0]
                                logger.info(
                                    f"Sample invoice: {sample.get('khhdon', 'N/A')}-{sample.get('shdon', 'N/A')}"
                                )

                            # Optimized: Skip file I/O for API responses (only save if debugging)
                            # This reduces I/O overhead significantly
                            # To enable, set SAVE_API_RESPONSES=true in environment
                            save_responses = os.getenv("SAVE_API_RESPONSES", "false").lower() == "true"
                            if save_responses:
                                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                                ttxly_suffix_file = f"_ttxly{ttxly}" if ttxly is not None else ""
                                filename = f"api_response_{flow_name}{ttxly_suffix_file}_page{page}_{timestamp}.json"
                                file_path = os.path.join(self.extraction_folder, filename)

                                with open(file_path, "w", encoding="utf-8") as f:
                                    json.dump(
                                        {
                                            "flow_name": flow_name,
                                            "ttxly": ttxly,
                                            "page": page,
                                            "endpoint_url": paginated_url,
                                            "request_headers": request_headers,
                                            "response_status": response.status_code,
                                            "timestamp": timestamp,
                                            "response_data": data,
                                        },
                                        f,
                                        ensure_ascii=False,
                                        indent=2,
                                    )

                                logger.info(f"Saved API response to: {filename}")
                            return data

                        except json.JSONDecodeError as e:
                            logger.error(
                                f"Failed to parse JSON response for {flow_name}{ttxly_suffix}: {e}"
                            )
                            logger.error(f"Raw response: {response.text[:1000]}")
                            return None

                    elif response.status_code == 500:
                        # Server error - retry with exponential backoff
                        ttxly_suffix = f" (ttxly={ttxly})" if ttxly is not None else ""
                        logger.warning(
                            f"⚠️ Server error (500) for {flow_name}{ttxly_suffix} on attempt {attempt + 1}/{max_retries}"
                        )
                        logger.error(f"Response body: {response.text[:1000]}")
                        
                        if attempt < max_retries - 1:
                            wait_time = 2 ** (attempt+1)  # Exponential backoff: 1s, 2s, 4s
                            logger.info(f"⏳ Retrying in {wait_time} seconds...")
                            await asyncio.sleep(wait_time)
                            continue
                        else:
                            logger.error(f"❌ Failed after {max_retries} attempts for {flow_name}{ttxly_suffix}")
                            # Save error response for debugging
                            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                            ttxly_suffix_file = f"_ttxly{ttxly}" if ttxly is not None else ""
                            error_filename = f"api_error_{flow_name}{ttxly_suffix_file}_500_{timestamp}.txt"
                            error_file_path = os.path.join(self.extraction_folder, error_filename)

                            with open(error_file_path, "w", encoding="utf-8") as f:
                                f.write(f"URL: {paginated_url}\n")
                                f.write(f"Status: {response.status_code}\n")
                                f.write(f"Headers: {dict(response.headers)}\n")
                                f.write(f"Body: {response.text}\n")
                            return None
                    
                    else:
                        # Other HTTP errors - don't retry
                        ttxly_suffix = f" (ttxly={ttxly})" if ttxly is not None else ""
                        logger.error(
                            f"❌ API request failed for {flow_name}{ttxly_suffix}: {response.status_code}"
                        )
                        logger.error(f"Response headers: {dict(response.headers)}")
                        logger.error(f"Response body: {response.text[:1000]}")

                        # Save error response for debugging
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        ttxly_suffix_file = f"_ttxly{ttxly}" if ttxly is not None else ""
                        error_filename = f"api_error_{flow_name}{ttxly_suffix_file}_{response.status_code}_{timestamp}.txt"
                        error_file_path = os.path.join(
                            self.extraction_folder, error_filename
                        )

                        with open(error_file_path, "w", encoding="utf-8") as f:
                            f.write(f"URL: {paginated_url}\n")
                            f.write(f"Status: {response.status_code}\n")
                            f.write(f"Headers: {dict(response.headers)}\n")
                            f.write(f"Body: {response.text}\n")

                        return None

            except Exception as e:
                ttxly_suffix = f" (ttxly={ttxly})" if ttxly is not None else ""
                logger.error(f"❌ Exception during API request for {flow_name}{ttxly_suffix} (attempt {attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.info(f"⏳ Retrying in {wait_time} seconds due to exception...")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    logger.error(f"❌ Failed after {max_retries} attempts for {flow_name}{ttxly_suffix}")
                    return None
        
        return None

    @langfuse_trace_async(
        name="get_invoice_listings_only",
        metadata={"component": "invoice_extraction", "operation": "list_invoices"}
    )
    async def get_invoice_listings_only(
        self, start_date: date, end_date: date, flows: List[InvoiceFlow]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Get invoice listings without downloading any files - clean separation of concerns"""
        
        logger.info(f"📋 Starting invoice listing only for {len(flows)} flows")
        logger.info(f"📅 Date range: {start_date} to {end_date}")
        logger.info(f"🔄 Flows to process: {[f.value for f in flows]}")

        # First, extract session information (optional if already provided)
        session_extracted = await self.extract_session_info()
        if not session_extracted:
            logger.warning("⚠️  Session extraction failed, but attempting to continue with provided session info")
            if not (self.session_cookies or self.session_headers):
                logger.error("❌ No session information available at all - cannot proceed")
                return {}

        # Define the API endpoints for each flow
        api_endpoints = {
            InvoiceFlow.BAN_RA_DIEN_TU: {
                "url": "https://hoadondientu.gdt.gov.vn:30000/query/invoices/sold",
                "name": "ban_ra_dien_tu",
            },
            InvoiceFlow.BAN_RA_MAY_TINH_TIEN: {
                "url": "https://hoadondientu.gdt.gov.vn:30000/sco-query/invoices/sold",
                "name": "ban_ra_may_tinh_tien",
            },
            InvoiceFlow.MUA_VAO_DIEN_TU: {
                "url": "https://hoadondientu.gdt.gov.vn:30000/query/invoices/purchase",
                "name": "mua_vao_dien_tu",
            },
            InvoiceFlow.MUA_VAO_MAY_TINH_TIEN: {
                "url": "https://hoadondientu.gdt.gov.vn:30000/sco-query/invoices/purchase",
                "name": "mua_vao_may_tinh_tien",
            },
        }

        flow_invoice_data = {}

        # Optimized: Process all flows in parallel using asyncio.gather()
        logger.info(f"🚀 Processing {len(flows)} flows in PARALLEL for faster performance")

        async def fetch_flow_data(flow):
            """Helper function to fetch data for a single flow"""
            if flow not in api_endpoints:
                logger.warning(f"⚠️ Unknown flow type: {flow} - skipping")
                return None, []

            endpoint_info = api_endpoints[flow]
            flow_name = endpoint_info['name']

            logger.info(f"🔄 Getting listings for flow: {flow_name}")

            try:
                # Make API request with retry logic (configurable retries)
                max_retries = getattr(settings, 'TAX_CRAWLER_MAX_RETRIES', 5)
                response_data = await self.make_api_request(
                    endpoint_info["url"], endpoint_info["name"], start_date, end_date, max_retries=max_retries
                )

                if response_data and response_data.get("datas"):
                    invoices_found = len(response_data["datas"])
                    logger.success(f"✅ Found {invoices_found} invoices for {flow_name}")
                    return flow_name, response_data["datas"]
                else:
                    logger.warning(f"⚠️ No data received for flow: {flow_name}")
                    return flow_name, []

            except Exception as flow_error:
                logger.error(f"❌ Error getting listings for flow {flow_name}: {flow_error}")
                return flow_name, []

        # Execute all flow requests in parallel
        results = await asyncio.gather(*[fetch_flow_data(flow) for flow in flows])

        # Collect results into flow_invoice_data dictionary
        for result in results:
            if result and result[0]:  # Check if we got a valid result
                flow_name, invoices = result
                flow_invoice_data[flow_name] = invoices

        total_invoices = sum(len(invoices) for invoices in flow_invoice_data.values())
        logger.success(f"🎉 Parallel invoice listing completed! Total: {total_invoices} invoices across {len(flow_invoice_data)} flows")

        return flow_invoice_data

    @langfuse_trace_async(
        name="download_files_for_invoices",
        metadata={"component": "file_download", "operation": "download_xml_json"}
    )
    async def download_files_for_invoices(
        self, flow_invoice_data: Dict[str, List[Dict[str, Any]]], download_xml: bool = True, download_json_detail: bool = False
    ) -> List[Dict[str, Any]]:
        """Download XML/JSON files for previously listed invoices - PARALLEL processing for maximum speed"""

        logger.info(f"📥 Starting PARALLEL file downloads for invoices")
        logger.info(f"🔧 Download XML: {download_xml}, Download JSON detail: {download_json_detail}")

        # Define endpoint info for download URL determination
        api_endpoints = {
            "ban_ra_dien_tu": "https://hoadondientu.gdt.gov.vn:30000/query/invoices/sold",
            "ban_ra_may_tinh_tien": "https://hoadondientu.gdt.gov.vn:30000/sco-query/invoices/sold",
            "mua_vao_dien_tu": "https://hoadondientu.gdt.gov.vn:30000/query/invoices/purchase",
            "mua_vao_may_tinh_tien": "https://hoadondientu.gdt.gov.vn:30000/sco-query/invoices/purchase",
        }

        @langfuse_span_async(name="download_single_invoice")
        async def download_single_invoice(flow_name: str, invoice_json: Dict[str, Any], invoice_index: int, global_index: int):
            """Helper function to download files for a single invoice"""
            try:
                xml_downloaded = None
                json_detail_downloaded = None

                # Download XML if requested
                if download_xml:
                    base_endpoint_url = api_endpoints.get(flow_name, "")
                    max_retries = getattr(settings, 'TAX_CRAWLER_MAX_RETRIES', 3)
                    xml_downloaded = await self._download_invoice_xml_with_retry(
                        invoice_json,
                        flow_name,
                        base_endpoint_url,
                        max_retries=max_retries,
                    )

                # Download JSON detail if requested or as fallback
                if download_json_detail or (download_xml and (not xml_downloaded or not xml_downloaded.endswith('.xml'))):
                    if download_xml and (not xml_downloaded or not xml_downloaded.endswith('.xml')):
                        logger.info(f"XML download failed for {invoice_json.get('khhdon')}-{invoice_json.get('shdon')}, trying JSON detail as fallback...")
                    json_detail_downloaded = await self.download_invoice_json_detail(
                        invoice_json,
                        flow_name
                    )

                # Check download success
                download_successful = (xml_downloaded and xml_downloaded.endswith('.xml')) or json_detail_downloaded
                if json_detail_downloaded and (not xml_downloaded or not xml_downloaded.endswith('.xml')):
                    logger.success(f"✅ JSON detail downloaded for {invoice_json.get('khhdon')}-{invoice_json.get('shdon')}")

                # Process invoice record - ALWAYS include invoice even if downloads failed
                file_downloaded = xml_downloaded if xml_downloaded and xml_downloaded.endswith('.xml') else json_detail_downloaded
                invoice_record = await self._process_api_invoice(
                    invoice_json, flow_name, invoice_index + 1, file_downloaded, json_detail_downloaded
                )

                if invoice_record:
                    invoice_record['xml_download_status'] = 'success' if xml_downloaded and xml_downloaded.endswith('.xml') else 'failed'
                    invoice_record['global_row_number'] = global_index
                    return (invoice_record, download_successful, None)
                else:
                    # Create minimal record if processing failed
                    logger.warning(f"⚠️ Failed to process invoice {invoice_index+1}, creating minimal record")
                    minimal_record = {
                        "page_number": 1,
                        "row_number": invoice_index + 1,
                        "global_row_number": global_index,
                        "invoice_code": str(invoice_json.get("khhdon", "")),
                        "invoice_number": str(invoice_json.get("shdon", "")),
                        "invoice_id": str(invoice_json.get("id", "")),
                        "timestamp": datetime.now().strftime("%Y%m%d_%H%M%S"),
                        "flow_type": flow_name,
                        "data": invoice_json,
                        "extraction_method": "direct_api",
                        "xml_file_path": xml_downloaded,
                        "xml_download_status": 'success' if xml_downloaded and xml_downloaded.endswith('.xml') else 'failed',
                        "processing_status": "minimal_record_created",
                        "nbmst": str(invoice_json.get("nbmst", "")),
                        "nbten": str(invoice_json.get("nbten", "")),
                        "tdlap": str(invoice_json.get("tdlap", "")),
                        "tgtttbso": str(invoice_json.get("tgtttbso", "")),
                    }
                    return (minimal_record, download_successful, None)

            except Exception as invoice_error:
                logger.error(f"❌ Error processing invoice {invoice_index+1} in {flow_name}: {invoice_error}")
                return (None, False, invoice_error)

        # Optimized: Process all flows and invoices in parallel using asyncio.gather()
        all_tasks = []
        task_metadata = []  # Track (flow_name, invoice_count) for each task group
        global_index = 0

        for flow_name, invoices in flow_invoice_data.items():
            if not invoices:
                continue

            logger.info(f"🚀 Preparing PARALLEL downloads for {len(invoices)} invoices in flow: {flow_name}")

            # Create tasks for all invoices in this flow
            for j, invoice_json in enumerate(invoices):
                global_index += 1
                task = download_single_invoice(flow_name, invoice_json, j, global_index)
                all_tasks.append(task)
                task_metadata.append((flow_name, j))

        if not all_tasks:
            logger.warning("⚠️ No invoices to download")
            return []

        logger.info(f"🚀 Executing {len(all_tasks)} invoice downloads with RATE LIMITING...")

        # Execute all downloads with rate limiting to prevent 429 errors
        results = await rate_limited_gather(*all_tasks, username=self.username)

        # Process results and calculate statistics
        all_invoice_data = []
        flow_stats = {}  # Track stats per flow

        for i, (result, download_successful, error) in enumerate(results):
            flow_name, _ = task_metadata[i]

            # Initialize flow stats if needed
            if flow_name not in flow_stats:
                flow_stats[flow_name] = {"total": 0, "successful": 0, "failed": 0}

            flow_stats[flow_name]["total"] += 1

            if result:
                all_invoice_data.append(result)
                if download_successful:
                    flow_stats[flow_name]["successful"] += 1
                else:
                    flow_stats[flow_name]["failed"] += 1
            else:
                flow_stats[flow_name]["failed"] += 1

        # Report flow results
        for flow_name, stats in flow_stats.items():
            success_rate = (stats["successful"] / stats["total"]) * 100 if stats["total"] > 0 else 0
            logger.success(
                f"✅ Flow {flow_name} downloads completed: {stats['total']} invoices, "
                f"{stats['successful']} files downloaded successfully ({success_rate:.1f}% success rate)"
            )

        logger.success(f"🎉 PARALLEL file downloads completed! Total invoices processed: {len(all_invoice_data)}")
        return all_invoice_data

    def _split_date_range_by_month(self, start_date: date, end_date: date) -> List[tuple[date, date]]:
        """
        Split date range into monthly chunks to comply with GDT API limits
        
        Args:
            start_date: Start date of the range
            end_date: End date of the range
            
        Returns:
            List of (start_date, end_date) tuples for each month
        """
        from calendar import monthrange
        
        if start_date > end_date:
            logger.error(f"Invalid date range: start_date ({start_date}) > end_date ({end_date})")
            return []
        
        date_chunks = []
        current_start = start_date
        
        while current_start <= end_date:
            # Calculate the last day of the current month
            year = current_start.year
            month = current_start.month
            _, last_day_of_month = monthrange(year, month)
            
            # End of current chunk is either end of month or the final end_date
            current_end = min(
                date(year, month, last_day_of_month),
                end_date
            )
            
            date_chunks.append((current_start, current_end))
            logger.debug(f"Date chunk: {current_start} to {current_end}")
            
            # Move to the first day of next month
            if current_end == end_date:
                break
            
            if month == 12:
                current_start = date(year + 1, 1, 1)
            else:
                current_start = date(year, month + 1, 1)
        
        logger.info(f"Split date range {start_date} to {end_date} into {len(date_chunks)} monthly chunks")
        return date_chunks

    @langfuse_span_async(name="convert_excel_json_to_flow_invoice_data")
    def _convert_excel_json_to_flow_invoice_data(
        self, 
        json_file_path: str, 
        flows: List[InvoiceFlow]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Convert Excel-generated JSON to flow_invoice_data format for download_files_for_invoices
        
        Args:
            json_file_path: Path to the combined JSON file from Excel export
            flows: List of invoice flows to organize data by
            
        Returns:
            Dictionary in flow_invoice_data format: {flow_name: [invoice_dicts]}
        """
        try:
            if not os.path.exists(json_file_path):
                logger.error(f"JSON file not found: {json_file_path}")
                return {}
            
            # Read the combined JSON file
            with open(json_file_path, 'r', encoding='utf-8') as f:
                json_data = json.load(f)
            
            if not json_data.get('invoices'):
                logger.warning("No invoices found in JSON file")
                return {}
            
            logger.info(f"Converting {len(json_data['invoices'])} Excel invoices to flow_invoice_data format")
            
            # Excel to API field mapping
            excel_to_api_mapping = {
                "ky_hieu_hoa_don": "khhdon",           # Ký hiệu hóa đơn
                "so_hoa_don": "shdon",                 # Số hóa đơn
                "mst_nguoi_ban": "nbmst",              # MST người bán
                "ten_nguoi_ban": "nbten",              # Tên người bán
                "mst_nguoi_mua": "nmten",              # MST người mua
                "ten_nguoi_mua": "nmten",              # Tên người mua
                "ngay_lap": "tdlap",                   # Ngày lập
                "tong_tien_thanh_toan": "tgtttbso",    # Tổng tiền thanh toán
                "tong_tien_chua_thue": "tgtcthue",     # Tổng tiền chưa thuế
                "tong_tien_thue": "tgtthue",           # Tổng tiền thuế
                "trang_thai_hoa_don": "tthai",         # Trạng thái hóa đơn
                "stt": "id",                           # STT as ID
                "ky_hieu_mau_so": "khmshdon,"          # Ký hiệu mẫu số hóa đơn
            }
            
            # Initialize flow_invoice_data
            flow_invoice_data = {}
            
            # Map flows to flow names
            flow_mapping = {
                InvoiceFlow.BAN_RA_DIEN_TU: "ban_ra_dien_tu",
                InvoiceFlow.BAN_RA_MAY_TINH_TIEN: "ban_ra_may_tinh_tien",
                InvoiceFlow.MUA_VAO_DIEN_TU: "mua_vao_dien_tu",
                InvoiceFlow.MUA_VAO_MAY_TINH_TIEN: "mua_vao_may_tinh_tien",
            }
            
            # Initialize each flow with empty list
            for flow in flows:
                if flow in flow_mapping:
                    flow_invoice_data[flow_mapping[flow]] = []
            
            # Process each Excel invoice
            for index, excel_invoice in enumerate(json_data['invoices'], 1):
                try:
                    # Convert Excel format to API format
                    api_invoice = {}
                    
                    # Map Excel fields to API fields
                    for excel_field, api_field in excel_to_api_mapping.items():
                        if excel_field in excel_invoice and excel_invoice[excel_field] is not None:
                            value = excel_invoice[excel_field]
                            # Clean up the value - ensure it's not just whitespace
                            if isinstance(value, str) and value.strip():
                                api_invoice[api_field] = value.strip()
                            elif value not in [None, "", "nan", "NaN"]:
                                api_invoice[api_field] = str(value)
                    
                    # Ensure required fields exist
                    if not api_invoice.get("khhdon") or not api_invoice.get("shdon"):
                        logger.warning(f"Skipping invoice {index} - missing required fields (khhdon or shdon)")
                        continue
                    
                    # Add default/computed fields that would normally come from API
                    api_invoice["id"] = api_invoice.get("id", index)
                    # api_invoice["khmshdon"] = 1  # Default invoice type
                    
                    # Convert date format if needed (Excel: dd/mm/yyyy -> API format)
                    if "tdlap" in api_invoice:
                        tdlap_value = api_invoice["tdlap"]
                        # If it's in dd/mm/yyyy format, convert to ISO format or keep as is
                        if isinstance(tdlap_value, str) and "/" in tdlap_value:
                            try:
                                # Parse dd/mm/yyyy and convert to standard format
                                from datetime import datetime
                                parsed_date = datetime.strptime(tdlap_value, "%d/%m/%Y")
                                api_invoice["tdlap"] = parsed_date.strftime("%Y-%m-%dT%H:%M:%S")
                            except ValueError:
                                # Keep original value if parsing fails
                                pass
                    
                    # Determine flow type from source file or distribute based on flows
                    source_file = excel_invoice.get('_source_file', '')
                    target_flow_name = None
                    
                    # Try to determine flow type from source filename
                    if 'purchase' in source_file.lower():
                        if InvoiceFlow.MUA_VAO_DIEN_TU in flows:
                            target_flow_name = "mua_vao_dien_tu"
                        elif InvoiceFlow.MUA_VAO_MAY_TINH_TIEN in flows:
                            target_flow_name = "mua_vao_may_tinh_tien"
                    elif 'sold' in source_file.lower():
                        if InvoiceFlow.BAN_RA_DIEN_TU in flows:
                            target_flow_name = "ban_ra_dien_tu"
                        elif InvoiceFlow.BAN_RA_MAY_TINH_TIEN in flows:
                            target_flow_name = "ban_ra_may_tinh_tien"
                    
                    # If no specific mapping found, use the first available flow
                    if not target_flow_name and flows:
                        target_flow_name = flow_mapping.get(flows[0])
                    
                    # Add invoice to the appropriate flow
                    if target_flow_name and target_flow_name in flow_invoice_data:
                        flow_invoice_data[target_flow_name].append(api_invoice)
                        logger.debug(f"Added invoice {api_invoice.get('khhdon')}-{api_invoice.get('shdon')} to {target_flow_name}")
                    else:
                        logger.warning(f"No target flow found for invoice {index}")
                    
                except Exception as e:
                    logger.error(f"Error processing Excel invoice {index}: {e}")
                    continue
            
            # Log summary
            total_converted = sum(len(invoices) for invoices in flow_invoice_data.values())
            logger.success(f"✅ Successfully converted {total_converted} invoices to flow_invoice_data format")
            
            for flow_name, invoices in flow_invoice_data.items():
                if invoices:
                    logger.info(f"  📊 {flow_name}: {len(invoices)} invoices")
            
            return flow_invoice_data
            
        except Exception as e:
            logger.error(f"Error converting Excel JSON to flow_invoice_data: {e}")
            return {}

    @langfuse_trace_async(
        name="crawl_all_flows_direct",
        metadata={"component": "main_orchestrator", "operation": "crawl_all_invoice_flows"}
    )
    async def crawl_all_flows_direct(
        self, 
        start_date: date, 
        end_date: date, 
        flows: List[InvoiceFlow],
        use_excel_import: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Crawl all flows using direct API calls or Excel import with sequential monthly processing
        
        Args:
            start_date: Start date for extraction
            end_date: End date for extraction
            flows: List of invoice flows to process
            use_excel_import: Whether to use Excel import instead of API calls
            
        Returns:
            List of processed invoice data
        """
        
        # Split date range into monthly chunks to comply with GDT API limits
        date_chunks = self._split_date_range_by_month(start_date, end_date)
        
        if len(date_chunks) > 1:
            logger.info(f"📅 Processing {len(date_chunks)} monthly chunks SEQUENTIALLY to avoid rate limiting")
        
        all_results = []
        
        for chunk_idx, (chunk_start, chunk_end) in enumerate(date_chunks, 1):
            logger.info(f"🔄 Processing chunk {chunk_idx}/{len(date_chunks)} SEQUENTIALLY: {chunk_start} to {chunk_end}")
            
            try:
                if use_excel_import:
                    chunk_results = await self._process_excel_chunk(chunk_start, chunk_end, flows, chunk_idx)
                else:
                    chunk_results = await self._process_api_chunk(chunk_start, chunk_end, flows, chunk_idx)
                
                if chunk_results:
                    all_results.extend(chunk_results)
                    logger.success(f"✅ Chunk {chunk_idx} completed: {len(chunk_results)} invoices")
                else:
                    logger.warning(f"⚠️ Chunk {chunk_idx} returned no results")
                
                # Add mandatory delay between chunks to avoid 429 errors
                if chunk_idx < len(date_chunks):
                    delay_seconds = 3.0  # Increased delay to prevent rate limiting
                    logger.info(f"⏳ Waiting {delay_seconds} seconds before next chunk to avoid rate limiting...")
                    await asyncio.sleep(delay_seconds)
                    
            except Exception as e:
                logger.error(f"❌ Error processing chunk {chunk_idx} ({chunk_start} to {chunk_end}): {e}")
                
                # Add delay even on error to prevent cascading rate limit issues
                if chunk_idx < len(date_chunks):
                    delay_seconds = 5.0  # Longer delay after error
                    logger.info(f"⏳ Error recovery: Waiting {delay_seconds} seconds before next chunk...")
                    await asyncio.sleep(delay_seconds)
                    
                # Continue with next chunk rather than failing completely
                continue
        
        total_invoices = len(all_results)
        logger.success(f"🎉 Sequential processing completed! Total: {total_invoices} invoices from {len(date_chunks)} monthly chunks")
        
        # Log summary by flow type
        if all_results:
            flow_summary = {}
            for invoice in all_results:
                flow_type = invoice.get('flow_type', 'unknown')
                flow_summary[flow_type] = flow_summary.get(flow_type, 0) + 1
            
            for flow_type, count in flow_summary.items():
                logger.info(f"  📊 {flow_type}: {count} invoices")
        
        return all_results
    
    async def _process_excel_chunk(
        self, 
        chunk_start: date, 
        chunk_end: date, 
        flows: List[InvoiceFlow], 
        chunk_idx: int
    ) -> List[Dict[str, Any]]:
        """Process a single monthly chunk using Excel import (sequential processing)"""
        
        logger.info(f"📊 Excel mode - Sequential chunk {chunk_idx}: {chunk_start} to {chunk_end}")
        
        try:
            # Map flows to invoice types for Excel export
            invoice_types = []
            if any(flow in [InvoiceFlow.MUA_VAO_DIEN_TU, InvoiceFlow.MUA_VAO_MAY_TINH_TIEN] for flow in flows):
                invoice_types.append("purchase")
            if any(flow in [InvoiceFlow.BAN_RA_DIEN_TU, InvoiceFlow.BAN_RA_MAY_TINH_TIEN] for flow in flows):
                invoice_types.append("sold")
            
            # Default to both if no specific flows matched
            if not invoice_types:
                invoice_types = ["purchase", "sold"]
            
            # Download and combine Excel files for this chunk (sequential processing)
            excel_results = await self.download_and_combine_excel_export(
                start_date=chunk_start,
                end_date=chunk_end,
                invoice_types=invoice_types,
                ttxly_values=[5, 6, 8],  # All processing statuses
                combine_to_json=True,
                delay_between_downloads=3.0  # Increased delay to prevent rate limiting
            )
            
            json_file_path = excel_results.get("json_file")
            
            if not json_file_path:
                logger.error(f"❌ Chunk {chunk_idx}: Failed to download and convert Excel files to JSON")
                logger.info(f"🔄 Chunk {chunk_idx}: Falling back to API-based extraction...")
                return await self._process_api_chunk(chunk_start, chunk_end, flows, chunk_idx)
            
            logger.success(f"✅ Chunk {chunk_idx}: Excel files downloaded and converted to JSON: {json_file_path}")
            
            # Convert Excel JSON to flow_invoice_data format
            logger.info(f"🔄 Chunk {chunk_idx}: Converting Excel JSON to flow_invoice_data format...")
            flow_invoice_data = self._convert_excel_json_to_flow_invoice_data(json_file_path, flows)
            
            if not flow_invoice_data:
                logger.error(f"❌ Chunk {chunk_idx}: Failed to convert Excel JSON to flow_invoice_data format")
                logger.info(f"🔄 Chunk {chunk_idx}: Falling back to API-based extraction...")
                return await self._process_api_chunk(chunk_start, chunk_end, flows, chunk_idx)
            
            # Use existing download logic to download XML/JSON files (sequential processing)
            logger.info(f"📥 Chunk {chunk_idx}: Downloading XML/JSON files using existing logic...")
            chunk_results = await self.download_files_for_invoices(
                flow_invoice_data, 
                download_xml=True, 
                download_json_detail=True  # Enable JSON fallback
            )
            
            return chunk_results or []
            
        except Exception as e:
            logger.error(f"❌ Excel processing error for chunk {chunk_idx}: {e}")
            logger.info(f"🔄 Chunk {chunk_idx}: Falling back to API-based extraction...")
            return await self._process_api_chunk(chunk_start, chunk_end, flows, chunk_idx)
    
    async def _process_api_chunk(
        self, 
        chunk_start: date, 
        chunk_end: date, 
        flows: List[InvoiceFlow], 
        chunk_idx: int
    ) -> List[Dict[str, Any]]:
        """Process a single monthly chunk using API calls (sequential processing)"""
        
        logger.info(f"🔗 API mode - Sequential chunk {chunk_idx}: {chunk_start} to {chunk_end}")
        
        try:
            # Use the existing separated methods (already sequential)
            flow_invoice_data = await self.get_invoice_listings_only(chunk_start, chunk_end, flows)
            
            if not flow_invoice_data:
                logger.error(f"❌ Chunk {chunk_idx}: No invoice listings retrieved")
                return []
            
            # Download files for all listed invoices (sequential processing)
            chunk_results = await self.download_files_for_invoices(
                flow_invoice_data, 
                download_xml=True, 
                download_json_detail=True  # Enable JSON fallback
            )
            
            return chunk_results or []
            
        except Exception as e:
            logger.error(f"❌ API processing error for chunk {chunk_idx}: {e}")
            return []
    
    async def _process_excel_chunk(
        self, 
        chunk_start: date, 
        chunk_end: date, 
        flows: List[InvoiceFlow], 
        chunk_idx: int
    ) -> List[Dict[str, Any]]:
        """Process a single monthly chunk using Excel import"""
        
        logger.info(f"📊 Excel mode - Chunk {chunk_idx}: {chunk_start} to {chunk_end}")
        
        try:
            # Map flows to invoice types for Excel export
            invoice_types = []
            if any(flow in [InvoiceFlow.MUA_VAO_DIEN_TU, InvoiceFlow.MUA_VAO_MAY_TINH_TIEN] for flow in flows):
                invoice_types.append("purchase")
            if any(flow in [InvoiceFlow.BAN_RA_DIEN_TU, InvoiceFlow.BAN_RA_MAY_TINH_TIEN] for flow in flows):
                invoice_types.append("sold")
            
            # Default to both if no specific flows matched
            if not invoice_types:
                invoice_types = ["purchase", "sold"]
            
            # Download and combine Excel files for this chunk
            excel_results = await self.download_and_combine_excel_export(
                start_date=chunk_start,
                end_date=chunk_end,
                invoice_types=invoice_types,
                ttxly_values=[5, 6, 8],  # All processing statuses
                combine_to_json=True,
                delay_between_downloads=2.0
            )
            
            json_file_path = excel_results.get("json_file")
            
            if not json_file_path:
                logger.error(f"❌ Chunk {chunk_idx}: Failed to download and convert Excel files to JSON")
                logger.info(f"🔄 Chunk {chunk_idx}: Falling back to API-based extraction...")
                return await self._process_api_chunk(chunk_start, chunk_end, flows, chunk_idx)
            
            logger.success(f"✅ Chunk {chunk_idx}: Excel files downloaded and converted to JSON: {json_file_path}")
            
            # Convert Excel JSON to flow_invoice_data format
            logger.info(f"🔄 Chunk {chunk_idx}: Converting Excel JSON to flow_invoice_data format...")
            flow_invoice_data = self._convert_excel_json_to_flow_invoice_data(json_file_path, flows)
            
            if not flow_invoice_data:
                logger.error(f"❌ Chunk {chunk_idx}: Failed to convert Excel JSON to flow_invoice_data format")
                logger.info(f"🔄 Chunk {chunk_idx}: Falling back to API-based extraction...")
                return await self._process_api_chunk(chunk_start, chunk_end, flows, chunk_idx)
            
            # Use existing download logic to download XML/JSON files
            logger.info(f"📥 Chunk {chunk_idx}: Downloading XML/JSON files using existing logic...")
            chunk_results = await self.download_files_for_invoices(
                flow_invoice_data, 
                download_xml=True, 
                download_json_detail=True  # Enable JSON fallback
            )
            
            return chunk_results or []
            
        except Exception as e:
            logger.error(f"❌ Excel processing error for chunk {chunk_idx}: {e}")
            logger.info(f"🔄 Chunk {chunk_idx}: Falling back to API-based extraction...")
            return await self._process_api_chunk(chunk_start, chunk_end, flows, chunk_idx)
    
    async def _process_api_chunk(
        self, 
        chunk_start: date, 
        chunk_end: date, 
        flows: List[InvoiceFlow], 
        chunk_idx: int
    ) -> List[Dict[str, Any]]:
        """Process a single monthly chunk using API calls"""
        
        logger.info(f"🔗 API mode - Chunk {chunk_idx}: {chunk_start} to {chunk_end}")
        
        try:
            # Use the existing separated methods
            flow_invoice_data = await self.get_invoice_listings_only(chunk_start, chunk_end, flows)
            
            if not flow_invoice_data:
                logger.error(f"❌ Chunk {chunk_idx}: No invoice listings retrieved")
                return []
            
            # Download files for all listed invoices
            chunk_results = await self.download_files_for_invoices(
                flow_invoice_data, 
                download_xml=True, 
                download_json_detail=True  # Enable JSON fallback
            )
            
            return chunk_results or []
            
        except Exception as e:
            logger.error(f"❌ API processing error for chunk {chunk_idx}: {e}")
            return []
    
    async def _crawl_api_fallback(
        self, start_date: date, end_date: date, flows: List[InvoiceFlow]
    ) -> List[Dict[str, Any]]:
        """Original API-based crawling method - used as fallback"""
        
        logger.info(f"🔗 Using API-based extraction for {len(flows)} flows")
        
        # Use the existing separated methods
        flow_invoice_data = await self.get_invoice_listings_only(start_date, end_date, flows)
        
        if not flow_invoice_data:
            logger.error("❌ No invoice listings retrieved")
            return []
        
        # Download files for all listed invoices
        all_invoice_data = await self.download_files_for_invoices(
            flow_invoice_data, 
            download_xml=True, 
            download_json_detail=True  # Enable JSON fallback
        )
        
        return all_invoice_data

    async def _upload_xmls_to_gcs(
        self, gcs_bucket_name: str, task_id: str
    ) -> Optional[str]:
        """Upload all XML files to GCS in a dedicated folder for this run"""

        if not storage:
            logger.warning("Google Cloud Storage not available - skipping GCS upload")
            return None

        if not gcs_bucket_name:
            logger.warning("No GCS bucket name provided - skipping GCS upload")
            return None

        try:
            # Create unique GCS folder path for this extraction run
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            gcs_folder_path = f"gdt-extract-direct-api/{task_id or timestamp}/xml"

            # Initialize GCS client - simplified to avoid timeout issues
            client = storage.Client()
            bucket = client.bucket(gcs_bucket_name)

            uploaded_files = []
            xml_files = []
            error_files = []
            json_detail_files = []

            # Find all files in extraction folder - XML files, error stubs, AND JSON detail files
            for root, _, files in os.walk(self.extraction_folder):
                for file in files:
                    if file.lower().endswith(".xml"):
                        local_file_path = os.path.join(root, file)
                        xml_files.append((local_file_path, file, "xml"))
                    elif file.lower().endswith("_xml_failed.json"):
                        # Include error stub files for invoices with failed XML downloads
                        local_file_path = os.path.join(root, file)
                        error_files.append((local_file_path, file, "error_stub"))
                    elif file.lower().endswith("_detail.json"):
                        # Include JSON detail files for invoices with successful JSON fallback
                        local_file_path = os.path.join(root, file)
                        json_detail_files.append((local_file_path, file, "json_detail"))

            all_files = xml_files + error_files + json_detail_files
            logger.info(f"Found {len(xml_files)} XML files, {len(error_files)} error stub files, and {len(json_detail_files)} JSON detail files to upload to GCS")

            if len(all_files) == 0:
                logger.warning("No files found in extraction folder for GCS upload")
                return None

            # Upload each file (XML files + error stubs) - simplified to avoid timeout issues
            for local_file_path, filename, file_type in all_files:
                try:
                    # Verify file exists and has content
                    if not os.path.exists(local_file_path):
                        logger.warning(f"File does not exist: {local_file_path}")
                        continue
                        
                    file_size = os.path.getsize(local_file_path)
                    if file_size == 0:
                        logger.warning(f"Skipping empty file: {filename}")
                        continue

                    # Create GCS blob path
                    blob_path = f"{gcs_folder_path}/{filename}"

                    # Upload file with timeout handling
                    blob = bucket.blob(blob_path)
                    blob.upload_from_filename(local_file_path, timeout=60)  # 60 second timeout

                    uploaded_files.append(blob_path)
                    
                    file_type_labels = {
                        "xml": "XML file",
                        "error_stub": "error stub", 
                        "json_detail": "JSON detail"
                    }
                    file_type_label = file_type_labels.get(file_type, "file")
                    logger.info(f"✅ Uploaded {file_type_label}: {filename} to gs://{gcs_bucket_name}/{blob_path}")

                except Exception as e:
                    logger.error(f"❌ Failed to upload {filename}: {e}")
                    # Continue with other files instead of failing completely

            if uploaded_files:
                gcs_folder_url = f"gs://{gcs_bucket_name}/{gcs_folder_path}"
                xml_count = len(xml_files)
                stub_count = len(error_files)
                json_count = len(json_detail_files)
                logger.success(
                    f"🚀 Successfully uploaded {len(uploaded_files)} files to GCS folder: {gcs_folder_url}"
                )
                logger.info(f"   • {xml_count} XML files + {stub_count} error stubs + {json_count} JSON details = {len(uploaded_files)} total files")
                
                # Return both folder URL and list of uploaded files
                return {
                    "folder_url": gcs_folder_url,
                    "uploaded_files": [f"gs://{gcs_bucket_name}/{file_path}" for file_path in uploaded_files]
                }
            else:
                logger.warning("No files were uploaded to GCS")
                return None

        except Exception as e:
            logger.error(f"Error uploading files to GCS: {e}")
            return None

    async def _download_invoice_xml_with_retry(
        self,
        invoice_json: Dict[str, Any],
        flow_name: str,
        base_endpoint_url: str,
        max_retries: int = 3,
    ) -> Optional[str]:
        """Download XML file with retry logic for failed downloads"""

        invoice_code = invoice_json.get("khhdon", "unknown")
        invoice_number = invoice_json.get("shdon", "unknown")

        for attempt in range(max_retries):
            try:
                logger.info(
                    f"Downloading XML {invoice_code}-{invoice_number} (attempt {attempt + 1}/{max_retries})"
                )

                xml_path = await self._download_invoice_xml(
                    invoice_json, flow_name, base_endpoint_url
                )

                if xml_path:
                    if attempt > 0:
                        logger.success(
                            f"✅ Successfully downloaded {invoice_code}-{invoice_number} on retry attempt {attempt + 1}"
                        )
                    return xml_path

                # If first attempt fails, wait before retry
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 2  # Exponential backoff: 2s, 4s, 6s
                    logger.info(
                        f"⏳ Waiting {wait_time}s before retry {attempt + 2}..."
                    )
                    await asyncio.sleep(wait_time)

            except Exception as e:
                logger.error(
                    f"❌ Attempt {attempt + 1} failed for {invoice_code}-{invoice_number}: {e}"
                )
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 2
                    logger.info(
                        f"⏳ Waiting {wait_time}s before retry {attempt + 2}..."
                    )
                    await asyncio.sleep(wait_time)

        logger.error(
            f"🔴 Failed to download XML for {invoice_code}-{invoice_number} after {max_retries} attempts"
        )
        
        # Create error stub file so this invoice still gets processed by import service
        try:
            flow_type_short = {
                "ban_ra_dien_tu": "sold_electronic",
                "ban_ra_may_tinh_tien": "sold_sco", 
                "mua_vao_dien_tu": "purchase_electronic",
                "mua_vao_may_tinh_tien": "purchase_sco",
            }.get(flow_name, flow_name)
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            error_stub_filename = f"{invoice_code}_{invoice_number}_{flow_type_short}_{timestamp}_xml_failed.json"
            error_stub_path = os.path.join(self.extraction_folder, error_stub_filename)
            
            # Create error stub with invoice details
            error_stub_data = {
                "error": "XML download failed after 3 retries",
                "invoice_code": invoice_code,
                "invoice_number": invoice_number,
                "flow_name": flow_name,
                "timestamp": timestamp,
                "invoice_data": invoice_json,
                "xml_download_status": "failed",
                "file_type": "error_stub"
            }
            
            with open(error_stub_path, "w", encoding="utf-8") as f:
                json.dump(error_stub_data, f, ensure_ascii=False, indent=2)
            
            logger.info(f"📝 Created error stub file: {error_stub_filename}")
            return error_stub_path  # Return stub path so invoice still gets processed
            
        except Exception as stub_error:
            logger.error(f"❌ Failed to create error stub file: {stub_error}")
            return None

    @rate_limit_gdt_requests("self")
    async def download_invoice_json_detail(
        self, invoice_json: Dict[str, Any], flow_name: str
    ) -> Optional[str]:
        """Download detailed JSON data for a specific invoice using the detail API"""
        
        try:
            # Extract required parameters from invoice JSON
            nbmst = invoice_json.get("nbmst", "")  # Mã số thuế người bán
            khhdon = invoice_json.get("khhdon", "")  # Ký hiệu hóa đơn
            shdon = str(invoice_json.get("shdon", ""))  # Số hóa đơn
            khmshdon = invoice_json.get("khmshdon", 1)  # Kiểu mã số hóa đơn

            if not all([nbmst, khhdon, shdon]):
                logger.warning(
                    f"Missing required parameters for JSON detail download: nbmst={nbmst}, khhdon={khhdon}, shdon={shdon}"
                )
                return None

            # Build detail API URL
            detail_url = "https://hoadondientu.gdt.gov.vn:30000/query/invoices/detail"
            
            # Build parameters for JSON detail
            params = {
                "nbmst": nbmst,
                "khhdon": khhdon,
                "shdon": shdon,
                "khmshdon": khmshdon,
            }

            # Build full URL with parameters
            param_string = "&".join([f"{k}={v}" for k, v in params.items()])
            full_detail_url = f"{detail_url}?{param_string}"

            logger.info(f"Downloading JSON detail: {khhdon}-{shdon} from {detail_url}")

            async with httpx.AsyncClient(
                cookies=self.session_cookies or {},
                headers=self.session_headers or {},
                timeout=30.0,
                verify=False,  # nosec B501 # Skip SSL verification for government portal
            ) as client:
                response = await client.get(full_detail_url)

                if response.status_code == 200:
                    try:
                        detail_data = response.json()
                        
                        # Create filename with meaningful information
                        flow_type_short = {
                            "ban_ra_dien_tu": "sold_electronic",
                            "ban_ra_may_tinh_tien": "sold_sco",
                            "mua_vao_dien_tu": "purchase_electronic",
                            "mua_vao_may_tinh_tien": "purchase_sco",
                        }.get(flow_name, flow_name)

                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        json_detail_filename = f"{khhdon}_{shdon}_{flow_type_short}_{timestamp}_detail.json"
                        json_detail_path = os.path.join(self.extraction_folder, json_detail_filename)

                        # Save detailed JSON content
                        with open(json_detail_path, "w", encoding="utf-8") as f:
                            json.dump(detail_data, f, ensure_ascii=False, indent=2)

                        logger.success(f"✅ Downloaded JSON detail: {json_detail_filename}")
                        return json_detail_path

                    except json.JSONDecodeError as e:
                        logger.error(f"❌ Failed to parse JSON detail response for {khhdon}-{shdon}: {e}")
                        return None
                else:
                    logger.error(
                        f"❌ Failed to download JSON detail for {khhdon}-{shdon}: {response.status_code}"
                    )
                    logger.error(f"Response: {response.text[:500]}")
                    return None

        except Exception as e:
            logger.error(
                f"Error downloading JSON detail for invoice {invoice_json.get('khhdon', 'unknown')}-{invoice_json.get('shdon', 'unknown')}: {e}"
            )
            return None

    @rate_limit_gdt_requests("self")
    async def _download_invoice_xml(
        self, invoice_json: Dict[str, Any], flow_name: str, base_endpoint_url: str
    ) -> Optional[str]:
        """Download XML file for a specific invoice"""

        try:
            # Extract required parameters from invoice JSON
            nbmst = invoice_json.get("nbmst", "")  # Mã số thuế người bán
            khhdon = invoice_json.get("khhdon", "")  # Ký hiệu hóa đơn
            shdon = str(invoice_json.get("shdon", ""))  # Số hóa đơn
            khmshdon = invoice_json.get("khmshdon", 1)  # Kiểu mã số hóa đơn

            if not all([nbmst, khhdon, shdon]):
                logger.warning(
                    f"Missing required parameters for XML download: nbmst={nbmst}, khhdon={khhdon}, shdon={shdon}"
                )
                return None

            # Build export-xml URL based on the flow type
            if "sco-query" in base_endpoint_url:
                # Cash register invoices use sco-query/invoices/export-xml
                export_url = "https://hoadondientu.gdt.gov.vn:30000/sco-query/invoices/export-xml"
            else:
                # Electronic invoices use query/invoices/export-xml
                export_url = (
                    "https://hoadondientu.gdt.gov.vn:30000/query/invoices/export-xml"
                )

            # Build parameters for XML export
            params = {
                "nbmst": nbmst,
                "khhdon": khhdon,
                "shdon": shdon,
                "khmshdon": khmshdon,
            }

            # Build full URL with parameters
            param_string = "&".join([f"{k}={v}" for k, v in params.items()])
            full_export_url = f"{export_url}?{param_string}"

            logger.info(f"Downloading XML: {khhdon}-{shdon} from {export_url}")

            async with httpx.AsyncClient(
                cookies=self.session_cookies or {},
                headers=self.session_headers or {},
                timeout=30.0,
                verify=False,  # nosec B501 # Skip SSL verification for government portal
            ) as client:
                response = await client.get(full_export_url)

                if response.status_code == 200:
                    # Create filename with meaningful information
                    flow_type_short = {
                        "ban_ra_dien_tu": "sold_electronic",
                        "ban_ra_may_tinh_tien": "sold_sco",
                        "mua_vao_dien_tu": "purchase_electronic",
                        "mua_vao_may_tinh_tien": "purchase_sco",
                    }.get(flow_name, flow_name)

                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

                    # Check if response is a ZIP file
                    content_type = response.headers.get("content-type", "")
                    if "zip" in content_type.lower() or response.content.startswith(
                        b"PK"
                    ):
                        # Handle ZIP file extraction
                        logger.info(
                            f"Received ZIP file for {khhdon}-{shdon}, extracting..."
                        )

                        try:
                            # Save ZIP to temporary file
                            with tempfile.NamedTemporaryFile(
                                delete=False, suffix=".zip"
                            ) as temp_zip:
                                temp_zip.write(response.content)
                                temp_zip_path = temp_zip.name

                            # Extract ZIP file
                            with zipfile.ZipFile(temp_zip_path, "r") as zip_ref:
                                # List all files in the ZIP
                                zip_files = zip_ref.namelist()
                                logger.info(f"ZIP contains files: {zip_files}")

                                xml_file_path = None
                                for file_name in zip_files:
                                    if file_name.lower().endswith(".xml"):
                                        # Extract XML file
                                        xml_content = zip_ref.read(file_name)

                                        # Create final XML filename
                                        xml_filename = f"{khhdon}_{shdon}_{flow_type_short}_{timestamp}.xml"
                                        xml_file_path = os.path.join(
                                            self.extraction_folder, xml_filename
                                        )

                                        # Save extracted XML content
                                        with open(xml_file_path, "wb") as f:
                                            f.write(xml_content)

                                        logger.success(
                                            f"✅ Extracted XML from ZIP: {xml_filename}"
                                        )
                                        break

                                # Clean up temporary ZIP file
                                os.unlink(temp_zip_path)

                                if xml_file_path:
                                    return xml_file_path
                                else:
                                    logger.warning(
                                        f"No XML file found in ZIP for {khhdon}-{shdon}"
                                    )
                                    return None

                        except zipfile.BadZipFile:
                            logger.error(
                                f"Invalid ZIP file received for {khhdon}-{shdon}"
                            )
                            return None
                        except Exception as e:
                            logger.error(
                                f"Error extracting ZIP for {khhdon}-{shdon}: {e}"
                            )
                            return None

                    else:
                        # Handle direct XML response (fallback)
                        xml_filename = (
                            f"{khhdon}_{shdon}_{flow_type_short}_{timestamp}.xml"
                        )
                        xml_file_path = os.path.join(
                            self.extraction_folder, xml_filename
                        )

                        # Save XML content directly
                        with open(xml_file_path, "wb") as f:
                            f.write(response.content)

                        logger.success(f"✅ Downloaded XML: {xml_filename}")
                        return xml_file_path

                else:
                    logger.error(
                        f"❌ Failed to download XML for {khhdon}-{shdon}: {response.status_code}"
                    )
                    logger.error(f"Response: {response.text[:500]}")
                    return None

        except Exception as e:
            logger.error(
                f"Error downloading XML for invoice {invoice_json.get('khhdon', 'unknown')}-{invoice_json.get('shdon', 'unknown')}: {e}"
            )
            return None

    async def _process_api_invoice(
        self,
        invoice_json: Dict[str, Any],
        flow_name: str,
        index: int,
        xml_file_path: Optional[str] = None,
        json_detail_path: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Process a single invoice from API response"""

        try:
            # Extract key information
            invoice_code = invoice_json.get("khhdon", "")  # Ký hiệu hóa đơn
            invoice_number = str(invoice_json.get("shdon", ""))  # Số hóa đơn
            invoice_id = invoice_json.get("id", "")  # Invoice ID

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

            # Save individual invoice JSON
            if invoice_code and invoice_number:
                json_filename = f"invoice_{invoice_code}_{invoice_number}_{flow_name}_{timestamp}_{index}.json"
            else:
                json_filename = f"invoice_{index}_{flow_name}_{timestamp}.json"

            json_file_path = os.path.join(self.extraction_folder, json_filename)

            with open(json_file_path, "w", encoding="utf-8") as f:
                json.dump(invoice_json, f, ensure_ascii=False, indent=2)

            # Create invoice record
            invoice_record = {
                "page_number": 1,  # API doesn't have pages in traditional sense
                "row_number": index,
                "global_row_number": index,
                "invoice_code": str(invoice_code) if invoice_code else "",
                "invoice_number": str(invoice_number) if invoice_number else "",
                "invoice_id": str(invoice_id) if invoice_id else "",
                "timestamp": timestamp,
                "source_file": json_filename,
                "local_path": json_file_path,
                "xml_file_path": xml_file_path,  # Add XML file path (could be error stub if XML failed)
                "json_detail_path": json_detail_path,  # Add JSON detail file path
                "xml_download_status": "success" if xml_file_path and xml_file_path.endswith('.xml') else "failed",  # Track XML download status
                "json_detail_status": "success" if json_detail_path else "not_attempted",  # Track JSON detail download status
                "download_method": "xml" if xml_file_path and xml_file_path.endswith('.xml') else ("json_detail" if json_detail_path else "failed"),
                "flow_type": flow_name,
                "data": invoice_json,  # Store the full JSON data
                "extraction_method": "direct_api",  # Mark as direct API extraction
                "processing_status": "completed",  # Mark as successfully processed
                "invoice_details": {
                    json_filename: {
                        "KHHDon": invoice_code,  # Ký hiệu
                        "SHDon": invoice_number,  # Số
                        "InvoiceId": invoice_id,
                        "XMLFile": xml_file_path,  # Include XML file path (could be error stub)
                        "JSONDetailFile": json_detail_path,  # Include JSON detail file path
                        "XMLStatus": "success" if xml_file_path and xml_file_path.endswith('.xml') else "failed",
                        "JSONDetailStatus": "success" if json_detail_path else "not_attempted",
                        "full_data": invoice_json,
                    }
                },
                # Add additional fields from the JSON - convert all to strings for Pydantic validation
                "nbmst": str(invoice_json.get("nbmst", "")) if invoice_json.get("nbmst") else "",
                "nbten": str(invoice_json.get("nbten", "")) if invoice_json.get("nbten") else "",
                "nmten": str(invoice_json.get("nmten", "")) if invoice_json.get("nmten") else "",
                "tdlap": str(invoice_json.get("tdlap", "")) if invoice_json.get("tdlap") else "",
                "tgtttbso": str(invoice_json.get("tgtttbso", "")) if invoice_json.get("tgtttbso") is not None else "",
                "tgtcthue": str(invoice_json.get("tgtcthue", "")) if invoice_json.get("tgtcthue") is not None else "",
                "tgtthue": str(invoice_json.get("tgtthue", "")) if invoice_json.get("tgtthue") is not None else "",
                "tthai": str(invoice_json.get("tthai", "")) if invoice_json.get("tthai") is not None else "",
            }

            return invoice_record

        except Exception as e:
            logger.error(f"Error processing API invoice {index}: {e}")
            return None

    async def download_excel_export(
        self, 
        start_date: date, 
        end_date: date,
        invoice_type: str = "purchase",
        ttxly: Optional[int] = 5,
        max_retries: int = 3,
        split_by_month: bool = True
    ) -> Optional[str]:
        """
        Download Excel export file from GDT portal with automatic monthly chunking
        
        Args:
            start_date: Start date for the export
            end_date: End date for the export  
            invoice_type: Type of invoices - "purchase" or "sold"
            ttxly: Processing status value (5, 6, or 8)
            max_retries: Maximum number of retry attempts
            split_by_month: Whether to split date range by month (default: True)
            
        Returns:
            Path to downloaded Excel file or None if failed
            Note: If multiple months, returns path to the last downloaded file
        """
        
        # Check if we need to split by month
        if split_by_month:
            date_chunks = self._split_date_range_by_month(start_date, end_date)
            
            if len(date_chunks) > 1:
                logger.info(f"📅 Splitting date range into {len(date_chunks)} monthly chunks for Excel export")
                
                all_file_paths = []
                for chunk_idx, (chunk_start, chunk_end) in enumerate(date_chunks, 1):
                    logger.info(f"🔄 Processing Excel export chunk {chunk_idx}/{len(date_chunks)}: {chunk_start} to {chunk_end}")
                    
                    # Download Excel for this chunk
                    file_path = await self._download_excel_export_single_month(
                        chunk_start, chunk_end, invoice_type, ttxly, max_retries, chunk_idx
                    )
                    
                    if file_path:
                        all_file_paths.append(file_path)
                        logger.success(f"✅ Chunk {chunk_idx} Excel downloaded: {os.path.basename(file_path)}")
                    else:
                        logger.warning(f"⚠️ Chunk {chunk_idx} Excel download failed")
                    
                    # Add delay between chunks to avoid rate limiting
                    if chunk_idx < len(date_chunks):
                        delay_seconds = 3.0
                        logger.info(f"⏳ Waiting {delay_seconds} seconds before next chunk...")
                        await asyncio.sleep(delay_seconds)
                
                logger.success(f"🎉 Downloaded {len(all_file_paths)} Excel files from {len(date_chunks)} monthly chunks")
                
                # Return the last downloaded file path (or could return all_file_paths if needed)
                return all_file_paths[-1] if all_file_paths else None
            else:
                # Single month, use original logic
                return await self._download_excel_export_single_month(
                    start_date, end_date, invoice_type, ttxly, max_retries, 1
                )
        else:
            # No splitting, use original logic
            return await self._download_excel_export_single_month(
                start_date, end_date, invoice_type, ttxly, max_retries, 1
            )
    
    async def _download_excel_export_single_month(
        self, 
        start_date: date, 
        end_date: date,
        invoice_type: str,
        ttxly: Optional[int],
        max_retries: int,
        chunk_idx: int
    ) -> Optional[str]:
        """
        Download Excel export for a single month (original logic)
        
        Args:
            start_date: Start date for the export
            end_date: End date for the export  
            invoice_type: Type of invoices - "purchase" or "sold"
            ttxly: Processing status value (5, 6, or 8)
            max_retries: Maximum number of retry attempts
            chunk_idx: Chunk index for logging
            
        Returns:
            Path to downloaded Excel file or None if failed
        """
        try:
            # Format dates for the API
            start_date_str = start_date.strftime("%d/%m/%Y")
            end_date_str = end_date.strftime("%d/%m/%Y")
            
            # Build search parameters
            search_params = f"tdlap=ge={start_date_str}T00:00:00;tdlap=le={end_date_str}T23:59:59"
            if ttxly is not None:
                search_params += f";ttxly=={ttxly}"
            
            # Determine the correct export URL based on invoice type
            if invoice_type == "purchase":
                export_url = "https://hoadondientu.gdt.gov.vn:30000/query/invoices/export-excel-sold"
                # Build full URL with parameters
                full_url = f"{export_url}?sort=tdlap:desc,khmshdon:asc,shdon:desc&search={search_params}&type={invoice_type}"
            else:
                # https://hoadondientu.gdt.gov.vn:30000/sco-query/invoices/export-excel?sort=tdlap:desc,khmshdon:asc,shdon:desc&search=tdlap=ge=16/08/2025T00:00:00;tdlap=le=15/09/2025T23:59:59
                export_url = "https://hoadondientu.gdt.gov.vn:30000/sco-query/invoices/export-excel"
                # Build full URL with parameters
                full_url = f"{export_url}?sort=tdlap:desc,khmshdon:asc,shdon:desc&search={search_params}"
            
            logger.info(f"📊 Chunk {chunk_idx}: Downloading Excel export for {invoice_type} invoices")
            logger.info(f"📅 Date range: {start_date_str} to {end_date_str}")
            logger.info(f"🔗 URL: {full_url}")
            
            # Retry logic for download
            for attempt in range(max_retries):
                try:
                    if attempt > 0:
                        wait_time = 2 ** attempt  # Exponential backoff
                        logger.info(f"⏳ Retry attempt {attempt + 1}/{max_retries} after {wait_time}s...")
                        await asyncio.sleep(wait_time)
                    
                    async with httpx.AsyncClient(
                        cookies=self.session_cookies or {},
                        headers=self.session_headers or {},
                        timeout=60.0,  # Longer timeout for Excel export
                        verify=False,  # nosec B501 # Skip SSL verification for government portal
                    ) as client:
                        logger.info(f"🔄 Sending Excel export request (attempt {attempt + 1}/{max_retries})...")
                        response = await client.get(full_url)
                        
                        if response.status_code == 200:
                            # Check if we got actual Excel content
                            content_type = response.headers.get("content-type", "")
                            content_disposition = response.headers.get("content-disposition", "")
                            
                            # Excel files typically have these content types
                            excel_types = [
                                "application/vnd.ms-excel",
                                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                "application/octet-stream"
                            ]
                            
                            is_excel = any(t in content_type.lower() for t in excel_types) or \
                                      "excel" in content_disposition.lower() or \
                                      response.content.startswith(b"PK")  # XLSX files are ZIP archives
                            
                            if not is_excel and len(response.content) < 1000:
                                # Might be an error message
                                logger.warning(f"Response might not be Excel. Content-Type: {content_type}")
                                logger.warning(f"Response preview: {response.content[:500]}")
                            
                            # Generate filename
                            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                            ttxly_suffix = f"_ttxly{ttxly}" if ttxly is not None else ""
                            filename = f"gdt_export_{invoice_type}_{start_date_str.replace('/', '')}_{end_date_str.replace('/', '')}{ttxly_suffix}_{timestamp}.xlsx"
                            file_path = os.path.join(self.extraction_folder, filename)
                            
                            # Save the Excel file
                            with open(file_path, "wb") as f:
                                f.write(response.content)
                            
                            file_size_mb = len(response.content) / (1024 * 1024)
                            logger.success(f"✅ Excel file downloaded successfully: {filename}")
                            logger.info(f"📁 File size: {file_size_mb:.2f} MB")
                            logger.info(f"📍 Saved to: {file_path}")
                            
                            return file_path
                            
                        elif response.status_code == 401:
                            logger.error(f"❌ Authentication failed (401). Bearer token may be expired.")
                            if attempt == max_retries - 1:
                                return None
                            # Try to re-authenticate on next attempt
                            continue
                            
                        elif response.status_code == 429:
                            logger.error(f"❌ Rate limited (429) on attempt {attempt + 1}")
                            if attempt < max_retries - 1:
                                # Longer wait for rate limiting
                                wait_time = 10 * (attempt + 1)  # 10s, 20s, 30s
                                logger.info(f"⏳ Rate limit recovery: Waiting {wait_time}s before retry...")
                                await asyncio.sleep(wait_time)
                                continue
                            return None
                            
                        elif response.status_code == 500:
                            logger.error(f"❌ Server error (500) on attempt {attempt + 1}")
                            if attempt == max_retries - 1:
                                # Save error response for debugging
                                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                                error_filename = f"excel_export_error_{invoice_type}_{timestamp}_{response.status_code}.txt"
                                error_path = os.path.join(self.extraction_folder, error_filename)
                                with open(error_path, "w", encoding="utf-8") as f:
                                    f.write(f"URL: {full_url}\n")
                                    f.write(f"Status: {response.status_code}\n")
                                    f.write(f"Response: {response.text}\n")
                                logger.error(f"Error details saved to: {error_filename}")
                                return None
                            continue
                            
                        else:
                            logger.error(f"❌ Unexpected status code: {response.status_code}")
                            logger.error(f"Response: {response.text[:500]}")
                            if attempt == max_retries - 1:
                                return None
                            continue
                            
                except httpx.TimeoutException:
                    logger.error(f"⏱️ Request timeout on attempt {attempt + 1}")
                    if attempt == max_retries - 1:
                        return None
                    continue
                    
                except Exception as e:
                    logger.error(f"❌ Error during download attempt {attempt + 1}: {e}")
                    if attempt == max_retries - 1:
                        return None
                    continue
            
            logger.error(f"❌ Failed to download Excel export after {max_retries} attempts")
            return None
            
        except Exception as e:
            logger.error(f"❌ Unexpected error in _download_excel_export_single_month: {e}")
            return None

    @langfuse_span_async(name="download_excel_bulk_export")
    async def download_excel_bulk_export(
        self,
        start_date: date,
        end_date: date,
        invoice_types: List[str] = ["purchase", "sold"],
        ttxly_values: List[int] = [5, 6, 8],  # Always use all three processing statuses
        delay_between_downloads: float = 2.0
    ) -> Dict[str, List[str]]:
        """
        Download multiple Excel export files for different invoice types and processing statuses
        Automatically handles monthly chunking for date ranges > 1 month
        
        Args:
            start_date: Start date for the export
            end_date: End date for the export
            invoice_types: List of invoice types to download ("purchase", "sold")
            ttxly_values: List of processing status values to download (5, 6, 8, or None)
            delay_between_downloads: Delay in seconds between downloads to respect server
            
        Returns:
            Dictionary mapping invoice type to list of downloaded file paths
        """
        downloaded_files = {}
        
        # Split date range into monthly chunks
        date_chunks = self._split_date_range_by_month(start_date, end_date)
        
        logger.info(f"📊 Starting bulk Excel export download")
        logger.info(f"📅 Date range: {start_date} to {end_date}")
        
        if len(date_chunks) > 1:
            logger.info(f"📅 Split into {len(date_chunks)} monthly chunks to comply with GDT limits")
        
        logger.info(f"📋 Invoice types: {invoice_types}")
        logger.info(f"🔢 Processing statuses (ttxly): {ttxly_values}")
        
        total_downloads = len(date_chunks) * len(invoice_types) * len(ttxly_values)
        current_download = 0
        
        # Initialize the dictionary
        for invoice_type in invoice_types:
            downloaded_files[invoice_type] = []
        
        # Process each monthly chunk sequentially
        for chunk_idx, (chunk_start, chunk_end) in enumerate(date_chunks, 1):
            if len(date_chunks) > 1:
                logger.info(f"🔄 Processing chunk {chunk_idx}/{len(date_chunks)}: {chunk_start} to {chunk_end}")
            
            for invoice_type in invoice_types:
                logger.info(f"➡️ Processing invoice type: {invoice_type}")
                
                for ttxly in ttxly_values:
                    current_download += 1
                    logger.info(f"📥 Download {current_download}/{total_downloads}: {invoice_type} with ttxly={ttxly} (chunk {chunk_idx})")
                    
                    # Add delay between downloads to respect the server and avoid 429 errors
                    if current_download > 1:
                        # Increase delay for sequential processing to avoid rate limits
                        actual_delay = max(delay_between_downloads, 3.0)  # Minimum 3 seconds
                        logger.info(f"⏳ Waiting {actual_delay}s before next download to avoid rate limiting...")
                        await asyncio.sleep(actual_delay)
                    
                    # Download the Excel file for this chunk (with split_by_month=False since we already split)
                    file_path = await self._download_excel_export_single_month(
                        start_date=chunk_start,
                        end_date=chunk_end,
                        invoice_type=invoice_type,
                        ttxly=ttxly,
                        max_retries=10,
                        chunk_idx=chunk_idx
                    )
                    
                    if file_path:
                        downloaded_files[invoice_type].append(file_path)
                        logger.success(f"✅ Downloaded: {os.path.basename(file_path)}")
                    else:
                        logger.warning(f"⚠️ Failed to download {invoice_type} with ttxly={ttxly} for chunk {chunk_idx}")
                    
                    # Extra delay after errors
                    if not file_path and current_download < total_downloads:
                        logger.info(f"⏳ Error recovery: Waiting 5s before next download...")
                        await asyncio.sleep(5.0)
            
            # Add delay between monthly chunks
            if chunk_idx < len(date_chunks):
                logger.info(f"⏳ Completed chunk {chunk_idx}, waiting 5s before next month...")
                await asyncio.sleep(5.0)
        
        # Summary
        total_success = sum(len(files) for files in downloaded_files.values())
        logger.info(f"📊 Bulk download completed: {total_success}/{total_downloads} files downloaded")
        
        for invoice_type, files in downloaded_files.items():
            if files:
                logger.success(f"✅ {invoice_type}: {len(files)} files downloaded across {len(date_chunks)} months")
                for file_path in files:
                    logger.info(f"   📁 {os.path.basename(file_path)}")
            else:
                logger.warning(f"⚠️ {invoice_type}: No files downloaded")
        
        return downloaded_files

    @langfuse_span_async(name="combine_excel_to_json")
    def combine_excel_to_json(
        self,
        excel_file_paths: List[str],
        output_json_path: Optional[str] = None
    ) -> Optional[str]:
        """
        Combine multiple Excel files into a single JSON file
        
        Args:
            excel_file_paths: List of paths to Excel files
            output_json_path: Optional output path for JSON file
            
        Returns:
            Path to the combined JSON file or None if failed
        """
        try:
            import pandas as pd
            from datetime import datetime
            
            if not excel_file_paths:
                logger.warning("No Excel files provided to combine")
                return None
            
            logger.info(f"📊 Starting to combine {len(excel_file_paths)} Excel files into JSON")
            
            all_data = []
            total_rows = 0
            
            # Process each Excel file
            for idx, file_path in enumerate(excel_file_paths, 1):
                if not os.path.exists(file_path):
                    logger.warning(f"File not found: {file_path}")
                    continue
                
                try:
                    logger.info(f"📖 Reading Excel file {idx}/{len(excel_file_paths)}: {os.path.basename(file_path)}")
                    
                    # Detect invoice type from filename
                    filename = os.path.basename(file_path).lower()
                    if filename.startswith('gdt_export_purchase'):
                        detected_invoice_type = 'purchase'
                    elif filename.startswith('gdt_export_sold'):
                        detected_invoice_type = 'sales'
                    else:
                        # Fallback detection for other patterns
                        if 'purchase' in filename:
                            detected_invoice_type = 'purchase'
                        elif 'sold' in filename:
                            detected_invoice_type = 'sales'
                        else:
                            detected_invoice_type = 'unknown'

                    logger.info(f"📊 Detected invoice type: '{detected_invoice_type}' from filename: {os.path.basename(file_path)}")
                    
                    # First, read the file without headers to find the actual header row
                    df_raw = pd.read_excel(file_path, engine='openpyxl', header=None, dtype=str)
                    
                    # Find the header row (typically row 5 for GDT exports)
                    header_row = None
                    for idx_row, row in df_raw.iterrows():
                        # Check if this row contains column headers
                        row_str = ' '.join([str(v) for v in row if pd.notna(v)])
                        # Look for Vietnamese header keywords
                        if any(keyword in row_str.lower() for keyword in ['stt', 'ký hiệu', 'số hóa đơn', 'ngày lập', 'mst', 'tên người']):
                            header_row = idx_row
                            logger.debug(f"Found header row at index {header_row}")
                            break
                    
                    # Read Excel file with proper header
                    if header_row is not None:
                        df = pd.read_excel(file_path, engine='openpyxl', header=header_row, dtype=str)
                    else:
                        # Fallback to default if no header found
                        df = pd.read_excel(file_path, engine='openpyxl', dtype=str)
                    
                    # Clean column names - remove 'Unnamed' columns
                    df = df.loc[:, ~df.columns.str.contains('^Unnamed')]
                    
                    # Remove rows that are completely empty
                    df = df.dropna(how='all')
                    
                    # Log column names for debugging
                    logger.debug(f"Columns in {os.path.basename(file_path)}: {df.columns.tolist()}")
                    
                    # Standardize column names to match expected format
                    column_mapping = {
                        'STT': 'stt',
                        'Ký hiệu mẫu số': 'ky_hieu_mau_so',
                        'Ký hiệu hóa đơn': 'ky_hieu_hoa_don',
                        'Số hóa đơn': 'so_hoa_don',
                        'Ngày lập': 'ngay_lap',
                        'MST người bán/MST người xuất hàng': 'mst_nguoi_ban',
                        'Tên người bán/Tên người xuất hàng': 'ten_nguoi_ban',
                        'Địa chỉ người bán': 'dia_chi_nguoi_ban',
                        'MST người mua/MST người nhận hàng': 'mst_nguoi_mua',
                        'Tên người mua/Tên người nhận hàng': 'ten_nguoi_mua',
                        'Tổng tiền chưa thuế': 'tong_tien_chua_thue',
                        'Tổng tiền thuế': 'tong_tien_thue',
                        'Tổng tiền chiết khấu thương mại': 'tong_tien_chiet_khau',
                        'Tổng tiền phí': 'tong_tien_phi',
                        'Tổng tiền thanh toán': 'tong_tien_thanh_toan',
                        'Đơn vị tiền tệ': 'don_vi_tien_te',
                        'Tỷ giá': 'ty_gia',
                        'Trạng thái hóa đơn': 'trang_thai_hoa_don',
                        'Kết quả kiểm tra hóa đơn': 'ket_qua_kiem_tra',
                        'Tình trạng xử lý': 'tinh_trang_xu_ly',
                        'Loại hóa đơn': 'loai_hoa_don'
                    }

                    # Rename columns if they match
                    df = df.rename(columns=column_mapping)
                    
                    # Convert DataFrame to list of dictionaries
                    records = df.to_dict('records')
                                        
                    # Add metadata to each record
                    for record in records:
                        # Clean up the record - convert NaN to None
                        cleaned_record = {}
                        for key, value in record.items():
                            if pd.isna(value):
                                cleaned_record[key] = None
                            elif isinstance(value, pd.Timestamp):
                                # Convert Timestamp to string
                                cleaned_record[key] = value.strftime("%d/%m/%Y")
                            elif isinstance(value, (pd.Timedelta, pd.Period)):
                                cleaned_record[key] = str(value)
                            else:
                                cleaned_record[key] = str(value) if value is not None else None
                        
                        # Add source file information
                        cleaned_record['_source_file'] = os.path.basename(file_path)
                        
                        # Add invoice type detected from filename
                        cleaned_record['loai_hoa_don'] = detected_invoice_type
                        
                        # Only add records that have meaningful data
                        if any(v is not None and str(v).strip() for k, v in cleaned_record.items() if not k.startswith('_') and k != 'loai_hoa_don'):
                            all_data.append(cleaned_record)
                    
                    rows_added = len([r for r in records if any(pd.notna(v) for v in r.values())])
                    total_rows += rows_added
                    logger.success(f"✅ Added {rows_added} rows from {os.path.basename(file_path)} (invoice_type: {detected_invoice_type})")
                    
                except Exception as e:
                    logger.error(f"❌ Error reading Excel file {file_path}: {e}")
                    import traceback
                    logger.debug(traceback.format_exc())
                    continue
            # Generate output filename if not provided
            if not output_json_path:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                output_json_path = os.path.join(
                    self.extraction_folder,
                    f"combined_invoices_{timestamp}.json"
                )
                
            if not all_data:
                logger.warning("No data extracted from Excel files")

            # Log summary by invoice type
            invoice_type_summary = {}
            for record in all_data:
                inv_type = record.get('loai_hoa_don', 'unknown')
                invoice_type_summary[inv_type] = invoice_type_summary.get(inv_type, 0) + 1
            
            logger.info(f"📊 Invoice type summary:")
            for inv_type, count in invoice_type_summary.items():
                logger.info(f"  • {inv_type}: {count} invoices")
            
            # Create the combined JSON structure
            combined_data = {
                "metadata": {
                    "total_records": len(all_data),
                    "source_files": len(excel_file_paths),
                    "created_at": datetime.now().isoformat(),
                    "source_file_list": [os.path.basename(f) for f in excel_file_paths if os.path.exists(f)],
                    "invoice_type_summary": invoice_type_summary
                },
                "invoices": all_data
            }
            
            # Write to JSON file
            with open(output_json_path, 'w', encoding='utf-8') as f:
                json.dump(combined_data, f, ensure_ascii=False, indent=2, default=str)
            
            file_size_mb = os.path.getsize(output_json_path) / (1024 * 1024)
            logger.success(f"✅ Successfully combined {len(all_data)} records into JSON")
            logger.info(f"📁 Output file: {output_json_path}")
            logger.info(f"📊 File size: {file_size_mb:.2f} MB")
            
            return output_json_path
            
        except ImportError:
            logger.error("❌ pandas is required for Excel processing. Install with: pip install pandas openpyxl")
            return None
        except Exception as e:
            logger.error(f"❌ Error combining Excel files to JSON: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return None

    @langfuse_trace_async(
        name="download_and_combine_excel_export",
        metadata={"component": "excel_processing", "operation": "download_and_combine_excel"}
    )
    async def download_and_combine_excel_export(
        self,
        start_date: date,
        end_date: date,
        invoice_types: List[str] = ["purchase", "sold"],
        ttxly_values: List[int] = [5, 6, 8],
        combine_to_json: bool = True,
        delay_between_downloads: float = 2.0
    ) -> Dict[str, Any]:
        """
        Download Excel files and optionally combine them into a single JSON file
        Automatically handles monthly chunking for date ranges > 1 month
        
        Args:
            start_date: Start date for the export
            end_date: End date for the export
            invoice_types: List of invoice types to download
            ttxly_values: List of processing status values
            combine_to_json: Whether to combine Excel files into JSON
            delay_between_downloads: Delay between downloads
            
        Returns:
            Dictionary with download results and optional JSON file path
        """
        try:
            # Calculate expected chunks and files
            date_chunks = self._split_date_range_by_month(start_date, end_date)
            expected_files = len(date_chunks) * len(invoice_types) * len(ttxly_values)
            
            logger.info(f"📊 Starting Excel download and combine process")
            logger.info(f"📅 Date range: {start_date} to {end_date}")
            
            if len(date_chunks) > 1:
                logger.info(f"📅 Will process {len(date_chunks)} monthly chunks sequentially")
            
            logger.info(f"📋 Expected files: {expected_files} ({len(invoice_types)} types × {len(ttxly_values)} statuses × {len(date_chunks)} months)")
            
            # First, download all Excel files (with automatic monthly chunking)
            download_results = await self.download_excel_bulk_export(
                start_date=start_date,
                end_date=end_date,
                invoice_types=invoice_types,
                ttxly_values=ttxly_values,
                delay_between_downloads=delay_between_downloads
            )
            
            # Collect all downloaded file paths
            all_excel_files = []
            for invoice_type, file_paths in download_results.items():
                all_excel_files.extend(file_paths)
            
            logger.info(f"📁 Downloaded {len(all_excel_files)} out of {expected_files} expected Excel files")
            
            result = {
                "excel_files": download_results,
                "total_excel_files": len(all_excel_files),
                "expected_files": expected_files,
                "monthly_chunks": len(date_chunks),
                "json_file": None
            }
            
            # Combine to JSON if requested
            if combine_to_json and all_excel_files:
                logger.info(f"📋 Combining {len(all_excel_files)} Excel files into JSON...")
                # Don't use await - combine_excel_to_json is not async
                json_path = self.combine_excel_to_json(all_excel_files)
                
                if json_path:
                    result["json_file"] = json_path
                    logger.success(f"✅ Excel files successfully combined into: {json_path}")
                else:
                    logger.warning("⚠️ Failed to combine Excel files into JSON")
            elif combine_to_json and not all_excel_files:
                logger.warning("⚠️ No Excel files to combine into JSON")
            
            return result
            
        except Exception as e:
            logger.error(f"❌ Error in download_and_combine_excel_export: {e}")
            return {
                "excel_files": {},
                "total_excel_files": 0,
                "expected_files": 0,
                "monthly_chunks": 0,
                "json_file": None,
                "error": str(e)
            }


@langfuse_trace_async(name="test_direct_api_crawler")
async def test_direct_api_crawler():
    """Test function for the direct API crawler"""
    from datetime import timedelta

    # Mock page and extraction folder for testing
    class MockPage:
        def __init__(self):
            self.context = MockContext()

        async def evaluate(self, script):
            return "Mozilla/5.0 (Test User Agent)"

    class MockContext:
        async def cookies(self):
            # Mock cookies that would be extracted from a real session
            return [
                {
                    "name": "JSESSIONID",
                    "value": "test-session-id",
                    "domain": "hoadondientu.gdt.gov.vn",
                },
                {
                    "name": "auth-token",
                    "value": "test-auth-token",
                    "domain": "hoadondientu.gdt.gov.vn",
                },
            ]

    # Test the crawler
    extraction_folder = "/tmp/test_direct_api"  # nosec B108 # Test directory only
    os.makedirs(extraction_folder, exist_ok=True)

    page = MockPage()
    crawler = DirectAPICrawler(page, extraction_folder)

    # Test date range
    end_date = date.today()
    start_date = end_date - timedelta(days=30)

    # Test flows
    flows = [InvoiceFlow.BAN_RA_DIEN_TU, InvoiceFlow.MUA_VAO_DIEN_TU]

    # This would normally be called after successful login
    results = await crawler.crawl_all_flows_direct(start_date, end_date, flows)

    print(f"Direct API crawler test completed: {len(results)} invoices")
    return results


if __name__ == "__main__":
    # Run test
    asyncio.run(test_direct_api_crawler())
