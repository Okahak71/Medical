import base64
import os
import json
from fastapi import FastAPI, HTTPException, Security, Depends
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from bs4 import BeautifulSoup
from groq import AsyncGroq

# 1. Initialize Application
app = FastAPI(title="Medical Verification API with AI Parsing")

# 2. Security Setup
API_KEY = os.getenv("API_KEY", "your-secure-api-key")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "your-groq-api-key")

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)
groq_client = AsyncGroq(api_key=GROQ_API_KEY)

def validate_api_key(api_key: str = Security(api_key_header)):
    if api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized: Invalid API Key")
    return api_key

# 3. Helpers
def extract_clean_text_from_html(html_content: str) -> str:
    """Strips useless scripts, styles, and tags to minimize token usage."""
    soup = BeautifulSoup(html_content, "html.parser")
    for element in soup(["script", "style", "svg", "nav", "footer", "header", "noscript"]):
        element.decompose()
    # Extract clean, readable text context
    return soup.get_text(separator="\n", strip=True)

# 4. Data Models
class VerificationRequest(BaseModel):
    state: str
    license_number: str

class VerificationResponse(BaseModel):
    status: str
    state: str
    license_number: str
    practitioner_name: str = "N/A"
    expiration_date: str = "N/A"
    message: str = "Success"
    screenshot_base64: str = ""

# 5. Core Endpoint
@app.post("/verify", response_model=VerificationResponse)
async def verify_medical_license(request: VerificationRequest, api_key: str = Depends(validate_api_key)):
    response_data = VerificationResponse(
        status="ERROR",
        state=request.state,
        license_number=request.license_number,
        message="An unknown error occurred."
    )
    
    playwright = None
    browser = None
    context = None
    
    try:
        clean_state = request.state.lstrip("=").strip()
        clean_license = request.license_number.lstrip("=").strip()

        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()
        page.set_default_timeout(20000)
        
        # --- 1. PLAYWRIGHT NAVIGATION ---
        # Example URL - Replace with the actual board URL
        target_url = "https://mqa-internet.doh.state.fl.us/MQASearchServices/HealthCareProviders" 
        await page.goto(target_url, wait_until="domcontentloaded")
        
        # Fill search inputs (Adjust simple input selectors as needed)
        await page.fill("#LicenseNumber", clean_license)
        await page.click("#btnSearch")
        
        # Wait for network idle or a basic container load
        await page.wait_for_load_state("networkidle")
        
        # Take full result page screenshot
        screenshot_bytes = await page.screenshot(full_page=False)
        b64_string = base64.b64encode(screenshot_bytes).decode("utf-8")
        
        # Get raw HTML content
        raw_html = await page.content()
        cleaned_text = extract_clean_text_from_html(raw_html)
        
        # --- 2. GROQ AI PARSING ---
        prompt = f"""
        You are an expert compliance data extractor. Analyze the following text extracted from a medical license verification search result page:

        ---
        {cleaned_text[:12000]} 
        ---

        Extract the verification details for license number '{clean_license}'.
        Return ONLY a JSON object with the following keys:
        - "status": The exact license status (e.g., "CLEAR/ACTIVE", "DELINQUENT/ACTIVE", "NOT_FOUND", etc.)
        - "expiration_date": The expiration date (YYYY-MM-DD or standard date format), or "N/A"
        - "practitioner_name": Full name of the practitioner, or "N/A"
        - "found": true if the record was found, false otherwise.
        """

        completion = await groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": "You output only valid JSON string objects without markdown formatting."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.0
        )
        
        ai_result = json.loads(completion.choices[0].message.content)
        
        if ai_result.get("found"):
            response_data.status = ai_result.get("status", "UNKNOWN")
            response_data.expiration_date = ai_result.get("expiration_date", "N/A")
            response_data.practitioner_name = ai_result.get("practitioner_name", "N/A")
            response_data.message = "Verification successful."
        else:
            response_data.status = "NOT_FOUND"
            response_data.message = "License record not found on state database."
            
        response_data.screenshot_base64 = b64_string

    except PlaywrightTimeoutError:
        response_data.message = "Timeout waiting for state website to respond."
    except Exception as e:
        response_data.message = f"Execution Exception: {str(e)}"
    finally:
        if context:
            await context.close()
        if browser:
            await browser.close()
        if playwright:
            await playwright.stop()
            
    return response_data
