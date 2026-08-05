import base64
import os
from fastapi import FastAPI, HTTPException, Security, Depends
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# 1. Initialize FastAPI Application
app = FastAPI(title="Medical Verification API")

# 2. Security Setup: Require an X-API-Key header
# It defaults to 'your-secure-api-key' but will use the environment variable if set on Render.
API_KEY = os.getenv("API_KEY", "your-secure-api-key")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)

def validate_api_key(api_key: str = Security(api_key_header)):
    if api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized: Invalid API Key")
    return api_key

# 3. Define Input and Output Data Models
class VerificationRequest(BaseModel):
    state: str
    license_number: str

class VerificationResponse(BaseModel):
    status: str
    state: str
    license_number: str
    expiration_date: str = "N/A"
    message: str = "Success"
    screenshot_base64: str = ""

# 4. Core Endpoint Logic
@app.post("/verify", response_model=VerificationResponse)
async def verify_medical_license(request: VerificationRequest, api_key: str = Depends(validate_api_key)):
    # Pre-fill a default failure response to prevent the API from crashing during an error
    response_data = VerificationResponse(
        status="ERROR",
        state=request.state,
        license_number=request.license_number,
        message="An unknown error occurred during execution."
    )
    
    playwright = None
    browser = None
    context = None
    
    try:
        # Start Playwright in an asynchronous context
        playwright = await async_playwright().start()
        
        # Chromium flags explicitly optimized for Render's strict memory limits
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-setuid-sandbox",
                "--single-process"
            ]
        )
        
        # Initialize an isolated browser context
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()
        
        # Set a hard 25-second timeout so requests do not hang your server
        page.set_default_timeout(25000)
        
        # --- SCRAPING LOGIC START ---
        # Note: Replace the target URL and CSS selectors with your actual target's details
        target_url = f"https://example-medical-board.com/verify?state={request.state}&license={request.license_number}"
        await page.goto(target_url)
        
        # Wait for the exact results container to become visible in the DOM
        results_locator = page.locator("#verification-results")
        await results_locator.wait_for(state="visible")
        
        # Screenshot strictly the results element to minimize Base64 payload size
        screenshot_bytes = await results_locator.screenshot()
        b64_string = base64.b64encode(screenshot_bytes).decode("utf-8")
        # --- SCRAPING LOGIC END ---
        
        # Populate the success data
        response_data.status = "ACTIVE"
        response_data.message = "Verification successful."
        response_data.screenshot_base64 = b64_string

    except PlaywrightTimeoutError:
        # Handle cases where the site takes too long or blocks the request
        response_data.message = "Timeout: The target website took too long to load or the specific element was not found."
    except Exception as e:
        # Catch unexpected errors cleanly
        response_data.message = f"Scraper Exception: {str(e)}"
    finally:
        # CRITICAL: Always release Playwright resources, regardless of success or failure
        if context:
            await context.close()
        if browser:
            await browser.close()
        if playwright:
            await playwright.stop()
            
    return response_data