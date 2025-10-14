"""GDT authentication activities - Real implementation."""

import os
import httpx
import cairosvg
from datetime import datetime, timedelta
from temporalio import activity
from temporalio.exceptions import ApplicationError
from google import genai
from app.config import settings

from temporal_app.models import GdtLoginRequest, GdtSession

# ============================================================================
# GDT Portal URLs
# ============================================================================
GDT_BASE_URL = "https://hoadondientu.gdt.gov.vn:30000"
GDT_CAPTCHA_URL = f"{GDT_BASE_URL}/captcha"
GDT_LOGIN_URL = f"{GDT_BASE_URL}/security-taxpayer/authenticate"
GDT_PORTAL_ORIGIN = "https://hoadondientu.gdt.gov.vn"

# ============================================================================
# Configuration
# ============================================================================
SESSION_EXPIRY_HOURS = 2
MAX_CAPTCHA_ATTEMPTS = 3


# ============================================================================
# Custom Exceptions (Following Temporal Patterns)
# ============================================================================
# ApplicationError with non_retryable flag tells Temporal whether to retry


class GDTAuthError(ApplicationError):
    """
    Retriable authentication error (CAPTCHA failures, network issues, rate limits).
    Temporal will automatically retry based on RetryPolicy.
    """

    def __init__(self, message: str):
        super().__init__(message, non_retryable=False)


class GDTInvalidCredentialsError(ApplicationError):
    """
    Non-retriable authentication error (wrong username/password, account locked).
    Temporal will NOT retry - workflow fails immediately.
    """

    def __init__(self, message: str):
        super().__init__(message, non_retryable=True)


@activity.defn
async def login_to_gdt(request: GdtLoginRequest) -> GdtSession:
    """
    Login to GDT portal with CAPTCHA solving.

    Flow (based on auth_code.py):
    1. Fetch CAPTCHA from /captcha endpoint
    2. Solve CAPTCHA (returns code for now, can integrate AI later)
    3. POST to /security-taxpayer/authenticate with username/password/captcha
    4. Extract bearer token from response
    5. Return GdtSession with auth credentials

    Args:
        request: GdtLoginRequest with company_id, username, password

    Returns:
        GdtSession: Session with bearer token and expiry

    Raises:
        GDTAuthError: If authentication fails
    """
    activity.logger.info(f"🔐 Logging in to GDT for company: {request.company_id}")

    # Step 1: Fetch CAPTCHA
    captcha_key, captcha_code = await _fetch_and_solve_captcha(activity)
    if not captcha_key or not captcha_code:
        activity.logger.warning("⚠️ Failed to fetch/solve CAPTCHA (will retry)")
        raise GDTAuthError("CAPTCHA fetch/solve failed - Temporal will retry")

    # Step 2: Authenticate with credentials + CAPTCHA
    activity.logger.info(f"🔤 Using CAPTCHA: {captcha_code}")

    login_payload = {
        "username": request.username,
        "password": request.password,
        "cvalue": captcha_code,  # CAPTCHA value
        "ckey": captcha_key,     # CAPTCHA key
    }

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
            response = await client.post(
                GDT_LOGIN_URL,
                json=login_payload,
                headers=headers,
            )

            # Handle rate limiting - let Temporal retry with exponential backoff
            if response.status_code == 429:
                activity.logger.warning("Rate limited (429) - Temporal will retry")
                # Let Temporal handle the retry with exponential backoff
                # No manual backoff needed - GDT will clear the rate limit
                raise GDTAuthError("Rate limit exceeded (429)")

            # Success
            if response.status_code == 200:
                auth_data = response.json()
                token = auth_data.get("token")

                if token:
                    # Check if token already has "Bearer " prefix
                    if token.startswith("Bearer "):
                        bearer_token = token
                        activity.logger.info(f"✅ Login successful (token already has Bearer prefix): {bearer_token[:30]}...")
                    else:
                        bearer_token = f"Bearer {token}"
                        activity.logger.info(f"✅ Login successful (added Bearer prefix): {bearer_token[:30]}...")

                    session = GdtSession(
                        company_id=request.company_id,
                        session_id=f"gdt_session_{request.company_id}_{int(datetime.now().timestamp())}",
                        access_token=bearer_token,
                        cookies={},  # GDT API uses bearer token, not cookies
                        expires_at=datetime.now() + timedelta(hours=SESSION_EXPIRY_HOURS),
                    )
                    return session

                # Check for error message in response
                message = auth_data.get("message", "")
                if "đăng nhập hoặc mật" in message or "password" in message.lower():
                    activity.logger.error(f"❌ Invalid credentials (non-retriable): {message}")
                    raise GDTInvalidCredentialsError(f"Invalid credentials: {message}")
                elif "đã bị khoá" in message or "locked" in message.lower():
                    activity.logger.error(f"❌ Account locked (non-retriable): {message}")
                    raise GDTInvalidCredentialsError(f"Account locked: {message}")
                elif "captcha" in message.lower():
                    activity.logger.warning(f"⚠️ CAPTCHA error (retriable): {message}")
                    raise GDTAuthError(f"CAPTCHA error: {message}")
                else:
                    activity.logger.error(f"No token in response: {auth_data}")
                    raise GDTAuthError("No token in auth response")

            # Other errors
            activity.logger.error(
                f"Login failed ({response.status_code}): {response.text[:200]}"
            )
            raise GDTAuthError(f"Login failed: HTTP {response.status_code}")

    except httpx.RequestError as e:
        activity.logger.error(f"Network error during login: {str(e)}")
        raise GDTAuthError(f"Network error: {str(e)}")


async def _fetch_and_solve_captcha(activity) -> tuple[str | None, str | None]:
    """
    Fetch CAPTCHA from GDT and solve it using Gemini AI.

    Returns:
        Tuple of (captcha_key, captcha_code) or (None, None) on failure
    """
    try:
        activity.logger.info("🔤 Fetching CAPTCHA from GDT")

        # Step 1: Fetch CAPTCHA
        async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
            response = await client.get(GDT_CAPTCHA_URL)

            if response.status_code != 200:
                activity.logger.error(f"CAPTCHA fetch failed: {response.status_code}")
                return None, None

            captcha_data = response.json()
            captcha_key = captcha_data.get("key")
            svg_content = captcha_data.get("content")

            if not captcha_key or not svg_content:
                activity.logger.error("Invalid CAPTCHA response format")
                return None, None

            activity.logger.info(f"✅ CAPTCHA fetched: {captcha_key[:10]}...")

        # Step 2: Solve CAPTCHA using Gemini
        captcha_code = await _solve_captcha_with_gemini(svg_content, activity)

        if not captcha_code:
            error_msg = "CAPTCHA solving failed - Gemini returned empty/None response"
            activity.logger.error(f"❌ {error_msg}")
            raise GDTAuthError(error_msg)

        activity.logger.info(f"✅ CAPTCHA solved: {captcha_code}")
        return captcha_key, captcha_code

    except httpx.RequestError as e:
        error_msg = f"Network error fetching CAPTCHA: {str(e)}"
        activity.logger.error(f"❌ {error_msg}")
        raise GDTAuthError(error_msg)
    except GDTAuthError:
        # Re-raise GDTAuthError as-is
        raise
    except Exception as e:
        error_msg = f"Unexpected error in CAPTCHA flow: {type(e).__name__}: {str(e)}"
        activity.logger.error(f"❌ {error_msg}")
        raise GDTAuthError(error_msg)


async def _solve_captcha_with_gemini(svg_content: str, activity) -> str | None:
    """
    Solve CAPTCHA using Google Gemini AI with enhanced image processing.

    Based on auth_code.py implementation with white background optimization.
    """
    try:
        # Convert SVG to PNG with white background (critical for better recognition)
        activity.logger.info("📸 Converting SVG CAPTCHA to PNG with white background...")
        png_data = cairosvg.svg2png(
            bytestring=svg_content.encode('utf-8'),
            background_color='white'  # Ensure white background for better CAPTCHA recognition
        )
        activity.logger.info(f"✅ PNG conversion successful ({len(png_data)} bytes)")

        # Initialize Gemini client from settings
        project_id = settings.GCP_PROJECT_ID or "finiziapp"
        region = settings.GCP_REGION
        model_name = settings.CAPTCHA_MODEL
        creds_path = settings.GOOGLE_APPLICATION_CREDENTIALS

        activity.logger.info(f"🤖 Initializing Gemini client:")
        activity.logger.info(f"   - Model: {model_name}")
        activity.logger.info(f"   - Project: {project_id}")
        activity.logger.info(f"   - Region: {region}")
        activity.logger.info(f"   - Credentials: {creds_path}")

        # Configure client using Vertex AI or direct Gemini API based on settings
        if settings.CAPTCHA_USE_VERTEX:
            client = genai.Client(
                vertexai=True,
                project=project_id,
                location=region,
            )
        else:
            client = genai.Client(api_key=settings.GEMINI_API_KEY)

        activity.logger.info("✅ Gemini client initialized successfully")

        # Enhanced prompt (matching auth_code.py)
        prompt = (
            "This is a CAPTCHA image from a Vietnamese government website. "
            "Please read and return ONLY the code (usually 5-7 characters, mix of letters and numbers). "
            "The code contains mostly lowercase letters and numbers. "
            "Common characters include: a-z, A-Z, 0-9. "
            "Do not return any explanation, just the code."
        )

        # Load and optimize PNG data as PIL Image (matching auth_code.py processing)
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(png_data))
        activity.logger.info("✅ PNG loaded as PIL Image")

        # Ensure white background and optimize image (critical step from auth_code.py)
        if img.mode in ('RGBA', 'LA', 'P'):
            # Create white background
            white_bg = Image.new('RGB', img.size, 'white')
            if img.mode == 'P':
                img = img.convert('RGBA')
            white_bg.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
            img = white_bg
            activity.logger.info("✅ Applied white background optimization")
        elif img.mode != 'RGB':
            # Convert other modes to RGB with white background
            white_bg = Image.new('RGB', img.size, 'white')
            white_bg.paste(img)
            img = white_bg
            activity.logger.info("✅ Converted to RGB with white background")

        # Call Gemini with optimized image
        activity.logger.info("🔮 Calling Gemini API to solve CAPTCHA...")

        # Run in thread to avoid blocking (as done in auth_code.py)
        import asyncio
        def generate_content():
            return client.models.generate_content(
                model=model_name,
                contents=[img, prompt]
            )

        response = await asyncio.to_thread(generate_content)
        activity.logger.info("✅ Gemini API call successful")

        # Extract and validate result (matching auth_code.py validation)
        if response and hasattr(response, 'text') and response.text:
            captcha_code = response.text.strip().split("\n")[0]  # Take first line only
            
            # Basic validation - should be 5-7 alphanumeric characters (from auth_code.py)
            if captcha_code and len(captcha_code) >= 5 and captcha_code.isalnum():
                activity.logger.info(f"🤖 Gemini solved CAPTCHA: '{captcha_code}' (length: {len(captcha_code)})")
                return captcha_code
            else:
                activity.logger.warning(f"🤖 Invalid CAPTCHA format: '{captcha_code}' (length: {len(captcha_code) if captcha_code else 0})")
                return None

        activity.logger.error(f"❌ Gemini returned empty response. Response object: {response}")
        return None

    except Exception as e:
        activity.logger.error(f"❌ Error solving CAPTCHA with Gemini:")
        activity.logger.error(f"   - Error Type: {type(e).__name__}")
        activity.logger.error(f"   - Error Message: {str(e)}")
        activity.logger.error(f"   - Full Error: {repr(e)}")
        import traceback
        activity.logger.error(f"   - Traceback:\n{traceback.format_exc()}")
        return None
