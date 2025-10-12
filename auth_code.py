"""
Combined API GDT Authentication Service
Handles both CAPTCHA solving and authentication in a single service
"""

import httpx
import asyncio
import tempfile
import shutil
import os
import base64
from typing import Optional, Tuple
from loguru import logger

from app.core.gdt_invoices_integration_workflow.token_cache import token_cache
from app.core.config import settings


class APIGDTAuthService:
    """Combined service for CAPTCHA solving and GDT portal authentication"""
    
    def __init__(self):
        self.captcha_url = "https://hoadondientu.gdt.gov.vn:30000/captcha"
        self.login_url = "https://hoadondientu.gdt.gov.vn:30000/security-taxpayer/authenticate"
        self.session_headers = {}
        self.session_cookies = {}
        
    async def authenticate_with_cache(
        self, 
        username: str, 
        password: str,
        force_refresh: bool = False
    ) -> Tuple[bool, Optional[str]]:
        """
        Authenticate with cached token support
        
        Args:
            username: User login username
            password: User login password  
            force_refresh: Force fresh authentication even if cached token exists
            
        Returns:
            Tuple of (success, bearer_token)
        """
        try:
            # Step 1: Check for cached token (unless force refresh)
            if not force_refresh:
                cached_token = token_cache.get_token(username, password)
                if cached_token:
                    logger.info("✅ Using cached authentication token")
                    await self._setup_session_with_token(cached_token)
                    
                    # Validate cached token
                    if await self._validate_token():
                        return True, cached_token
                    else:
                        logger.warning("🔄 Cached token invalid, performing fresh authentication")
                        token_cache.invalidate_token(username, password)
            
            # Step 2: Perform fresh authentication
            logger.info("🔄 Performing fresh authentication")
            success, token = await self._perform_fresh_authentication(username, password)
            
            if success:
                # Cache the new token (no expiration timeout)
                token_cache.store_token(username, password, token)
                logger.success("✅ Fresh authentication successful, token cached until invalidated")
                return True, token
            else:
                logger.error("❌ Fresh authentication failed")
                return False, None
                
        except Exception as e:
            logger.error(f"❌ Authentication error: {e}")
            return False, None
    
    async def _perform_fresh_authentication(
        self, 
        username: str, 
        password: str,
        max_retries: int = 3
    ) -> Tuple[bool, Optional[str]]:
        """Perform fresh authentication with CAPTCHA solving"""
        
        for attempt in range(max_retries):
            logger.info(f"🔄 Authentication attempt {attempt + 1}/{max_retries}")
            
            try:
                # Step 1: Fetch and solve CAPTCHA
                captcha_success, captcha_key, captcha_code = await self._fetch_and_solve_captcha()
                if not captcha_success:
                    logger.warning("❌ CAPTCHA fetch/solve failed")
                    continue
                
                # Step 2: Authenticate with credentials and CAPTCHA
                auth_success, token = await self._authenticate_with_credentials(
                    username, password, captcha_code, captcha_key
                )
                
                logger.info(f"Authentication response token/message: {token}")

                if "đăng nhập hoặc mật" in (token or ""):
                    logger.warning("❌ Credentials incorrect, aborting further attempts")
                    return False, None
                
                if "đã bị khoá" in (token or ""):
                    logger.warning("❌ Account locked, aborting further attempts")
                    return False, None

                if auth_success:
                    logger.success("✅ Authentication successful")
                    return True, token
                else:
                    logger.warning(f"❌ Authentication failed on attempt {attempt + 1}")
                    
                # Wait before retry
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
                    
            except Exception as e:
                logger.error(f"Authentication attempt {attempt + 1} error: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
        
        logger.error("❌ All authentication attempts failed")
        return False, None
    
    async def _fetch_and_solve_captcha(self) -> Tuple[bool, Optional[str], Optional[str]]:
        """Fetch CAPTCHA and solve it using AI"""
        try:
            # Step 1: Fetch CAPTCHA
            logger.info("🔤 Fetching CAPTCHA from direct API")
            
            async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
                response = await client.get(self.captcha_url)
                
                if response.status_code != 200:
                    logger.error(f"❌ CAPTCHA fetch failed: {response.status_code}")
                    return False, None, None
                
                try:
                    captcha_data = response.json()
                    captcha_key = captcha_data.get("key")
                    svg_data = captcha_data.get("content")  # SVG content (not base64)
                    
                    if not captcha_key or not svg_data:
                        logger.error("❌ Invalid CAPTCHA response format")
                        return False, None, None
                        
                    logger.success(f"✅ CAPTCHA fetched successfully, key: {captcha_key[:10]}...")
                    
                except Exception as e:
                    logger.error(f"❌ Failed to parse CAPTCHA response: {e}")
                    return False, None, None
            
            # Step 2: Solve CAPTCHA using AI
            temp_dir = tempfile.mkdtemp()
            try:
                captcha_code = await self._solve_captcha_with_ai(svg_data, temp_dir)
                if captcha_code:
                    logger.success(f"🤖 CAPTCHA solved: {captcha_code}")
                    return True, captcha_key, captcha_code
                else:
                    logger.error("❌ CAPTCHA solving failed")
                    return False, None, None
                    
            finally:
                # Clean up temp directory
                try:
                    shutil.rmtree(temp_dir)
                except:
                    pass
                    
        except Exception as e:
            logger.error(f"❌ CAPTCHA fetch/solve error: {e}")
            return False, None, None
    
    async def _solve_captcha_with_ai(self, svg_data: str, temp_dir: str) -> Optional[str]:
        """Solve CAPTCHA using AI Vision (Gemini)"""
        try:
            # Step 1: Convert SVG to PNG
            captcha_image_path = await self._convert_svg_to_png(svg_data, temp_dir)
            if not captcha_image_path:
                logger.error("❌ Failed to convert CAPTCHA SVG to PNG")
                return None
            
            # Step 2: Use AI to solve CAPTCHA using new Google Genai client
            from google import genai
            from PIL import Image
            
            # Setup AI client
            try:
                # Try GCP_PROJECT_ID first, then fall back to GCS_PROJECT_ID
                project_id = getattr(settings, 'GCP_PROJECT_ID', None) or getattr(settings, 'GCS_PROJECT_ID', None)
                location = getattr(settings, 'GCP_REGION', 'asia-southeast1')

                if getattr(settings, 'CAPTCHA_USE_VERTEX', False) and project_id:
                    ai_client = genai.Client(vertexai=True, project=project_id, location=location)
                    logger.info(f"Using Gemini with Vertex AI for CAPTCHA solving (project: {project_id}, location: {location})")
                elif getattr(settings, 'GEMINI_API_KEY', None):
                    ai_client = genai.Client(api_key=settings.GEMINI_API_KEY)
                    logger.info("Using Gemini API for CAPTCHA solving")
                else:
                    logger.error("❌ No Gemini credentials configured")
                    return None
            except Exception as e:
                logger.error(f"❌ Failed to setup Gemini client: {e}")
                return None
            
            # Load image
            image = Image.open(captcha_image_path)
            
            # CAPTCHA solving prompt
            prompt = (
                "This is a CAPTCHA image from a Vietnamese government website. "
                "Please read and return ONLY the code (usually 5-7 characters, mix of letters and numbers). "
                "The code contains mostly lowercase letters and numbers. "
                "Common characters include: a-z, A-Z, 0-9. "
                "Do not return any explanation, just the code."
            )
            
            # Use Gemini to solve CAPTCHA
            model_name = getattr(settings, 'CAPTCHA_MODEL', 'gemini-2.5-flash')
            logger.info(f"🤖 Solving CAPTCHA with {model_name}")
            
            # Run in thread to avoid blocking
            def generate_content():
                return ai_client.models.generate_content(
                    model=model_name,
                    contents=[image, prompt]
                )
            
            response = await asyncio.to_thread(generate_content)
            
            if hasattr(response, 'text') and response.text:
                captcha_code = response.text.strip().split("\n")[0]
                
                # Basic validation - should be 5-7 alphanumeric characters
                if captcha_code and len(captcha_code) >= 5 and captcha_code.isalnum():
                    return captcha_code
                else:
                    logger.warning(f"🤖 Invalid CAPTCHA format: '{captcha_code}'")
                    return None
            else:
                logger.error("❌ Empty response from AI")
                return None
                
        except Exception as e:
            logger.error(f"❌ AI CAPTCHA solving error: {e}")
            return None
    
    async def _convert_svg_to_png(self, svg_data: str, temp_dir: str) -> Optional[str]:
        """Convert SVG data to PNG image"""
        try:
            # Handle both raw SVG and base64 SVG
            if svg_data.startswith('data:image/svg+xml;base64,'):
                svg_data = svg_data.replace('data:image/svg+xml;base64,', '')
                svg_bytes = base64.b64decode(svg_data)
            else:
                # Raw SVG content
                svg_bytes = svg_data.encode('utf-8')
            
            # Convert SVG to PNG using cairosvg if available
            try:
                import cairosvg
                from PIL import Image
                import io
                
                # Convert SVG to PNG with white background
                png_bytes = cairosvg.svg2png(
                    bytestring=svg_bytes,
                    background_color='white'  # Ensure white background for better CAPTCHA recognition
                )
                
                # Further process with PIL to ensure white background and optimize
                img = Image.open(io.BytesIO(png_bytes))
                
                # Convert to RGB mode with white background if needed
                if img.mode in ('RGBA', 'LA', 'P'):
                    # Create white background
                    white_bg = Image.new('RGB', img.size, 'white')
                    if img.mode == 'P':
                        img = img.convert('RGBA')
                    white_bg.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                    img = white_bg
                
                # Save optimized PNG image with white background
                png_path = os.path.join(temp_dir, "captcha.png")
                img.save(png_path, "PNG", optimize=True)
                
                logger.debug(f"CAPTCHA SVG converted to PNG with white background: {png_path}")
                return png_path
                
            except ImportError:
                logger.warning("cairosvg not available, using fallback method")
                
                # Fallback: Save SVG directly and try to convert with PIL
                svg_path = os.path.join(temp_dir, "captcha.svg")
                with open(svg_path, "wb") as f:
                    f.write(svg_bytes)
                
                # Try to convert with Pillow (limited SVG support)
                try:
                    from PIL import Image
                    img = Image.open(svg_path)
                    
                    # Ensure white background for fallback method too
                    if img.mode in ('RGBA', 'LA', 'P'):
                        # Create white background
                        white_bg = Image.new('RGB', img.size, 'white')
                        if img.mode == 'P':
                            img = img.convert('RGBA')
                        white_bg.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                        img = white_bg
                    elif img.mode != 'RGB':
                        # Convert other modes to RGB with white background
                        white_bg = Image.new('RGB', img.size, 'white')
                        white_bg.paste(img)
                        img = white_bg
                    
                    png_path = os.path.join(temp_dir, "captcha.png")
                    img.save(png_path, "PNG", optimize=True)
                    logger.debug(f"CAPTCHA SVG converted to PNG with white background (fallback): {png_path}")
                    return png_path
                except Exception as e:
                    logger.error(f"❌ Failed to convert SVG with PIL: {e}")
                    return None
                    
        except Exception as e:
            logger.error(f"❌ SVG conversion error: {e}")
            return None
    
    async def _authenticate_with_credentials(
        self,
        username: str,
        password: str,
        captcha_code: str,
        captcha_key: str
    ) -> Tuple[bool, Optional[str]]:
        """Authenticate using credentials and CAPTCHA"""
        
        try:
            # Login payload
            login_data = {
                "username": username,
                "password": password,
                "cvalue": captcha_code,
                "ckey": captcha_key
            }
            
            logger.info(f"🔐 Authenticating user: {username}")
            logger.info(f"🔤 Using CAPTCHA: {captcha_code}")
            
            async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
                response = await client.post(self.login_url, json=login_data)
                
                if response.status_code == 200:
                    try:
                        auth_data = response.json()
                        
                        # Extract Bearer token
                        token = auth_data.get("token")
                        if token:
                            bearer_token = f"Bearer {token}"
                            logger.success(f"✅ Authentication successful, token: {bearer_token[:20]}...")
                            
                            # Setup session with new token
                            await self._setup_session_with_token(bearer_token)
                            return True, bearer_token
                        elif auth_data.get("message"):
                            logger.error(f"❌ Authentication failed: {auth_data.get('message')}")
                            return False, auth_data.get("message")
                        else:
                            logger.error("❌ No token in response")
                            return False, None
                            
                    except Exception as e:
                        logger.error(f"❌ Failed to parse auth response: {e}")
                        return False, None
                else:
                    auth_error = response.json()
                    logger.error(f"❌ Authentication failed: {response.status_code}")
                    logger.error(f"Response: {response.text[:200]}")
                    return False, auth_error.get("message", f"HTTP {response.status_code}")
                    
        except Exception as e:
            logger.error(f"❌ Authentication request error: {e}")
            return False, None
    
    async def _setup_session_with_token(self, bearer_token: str):
        """Setup session headers and cookies with Bearer token"""
        self.session_headers = {
            "Authorization": bearer_token,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        }
        
        # Extract session cookies if needed
        self.session_cookies = {}
        logger.debug("Session setup complete with Bearer token")
    
    async def _validate_token(self) -> bool:
        """Validate cached token by making a test API call using correct format"""
        try:
            from datetime import date, timedelta
            
            # Use correct API format as shown in DirectAPICrawler.make_api_request
            test_date = date.today()
            yesterday = test_date - timedelta(days=1)
            
            # Format dates correctly (dd/mm/yyyy format)
            start_date_str = yesterday.strftime("%d/%m/%Y")
            end_date_str = test_date.strftime("%d/%m/%Y")
            
            # Build search parameters in correct format
            search_params = f"tdlap=ge={start_date_str}T00:00:00;tdlap=le={end_date_str}T23:59:59"
            
            # Build full URL with correct parameters
            test_url = "https://hoadondientu.gdt.gov.vn:30000/query/invoices/sold"
            full_url = f"{test_url}?sort=tdlap:desc,khmshdon:asc,shdon:desc&size=1&search={search_params}"
            
            # Add correct headers for validation
            validation_headers = self.session_headers.copy() if self.session_headers else {}
            validation_headers.update({
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "vi",
                "Connection": "keep-alive",
                "Host": "hoadondientu.gdt.gov.vn:30000",
                "Origin": "https://hoadondientu.gdt.gov.vn",
                "Referer": "https://hoadondientu.gdt.gov.vn/",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-site",
                "End-Point": "/tra-cuu/tra-cuu-hoa-don",
            })

            logger.info("🔍 Validating cached token with test API call")
            logger.info(f"🔗 Validation URL: {full_url}")
            logger.info(f"🔍 Validating token with header: {validation_headers}")
            
            async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
                response = await client.get(full_url, headers=validation_headers)
                logger.info(f"Validation response status: {response}")
                if response.status_code == 200:
                    logger.debug("✅ Token validation successful")
                    return True
                elif response.status_code in [401, 403]:
                    logger.debug("❌ Token validation failed - unauthorized")
                    return False
                else:
                    logger.warning(f"Token validation unclear: {response.status_code}")
                    logger.debug(f"Response: {response.text[:200]}")
                    return False
                    
        except Exception as e:
            logger.warning(f"Token validation error: {e}")
            return False
    
    def get_session_info(self) -> dict:
        """Get current session information"""
        return {
            "headers": self.session_headers,
            "cookies": self.session_cookies,
            "has_bearer_token": "Authorization" in self.session_headers
        }
