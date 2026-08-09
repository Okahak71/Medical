import base64
import os
import json
from fastapi import FastAPI, HTTPException, Security, Depends
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from bs4 import BeautifulSoup
from groq import AsyncGroq

app = FastAPI(title="Medical Verification API with AI Parsing")

API_KEY = os.getenv("API_KEY", "your-secure-api-key")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "your-groq-api-key")

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)
groq_client = AsyncGroq(api_key=GROQ_API_KEY)

# Map states to their official search portals and selector configs
STATE_PORTALS = {
    "FL": {
        "url": "https://mqa-internet.doh.state.fl.us/MQASearchServices/HealthCareProviders",
        "input_selector": "#LicenseNumber",
        "submit_selector": "#btnSearch"
    },
    "TX": {
        "url": "https://35422.000webhostapp.com/", # Replace with actual TX Board URL
        "input_selector": "#LicenseNumber",
        "submit_selector": "#btnSearch"
    }
}

def validate_api_key(api_key: str = Security(api_key_header)):
    if api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized: Invalid API Key")
    return api_key

def extract_clean_text_from_html(html_content: str) -> str:
    soup = BeautifulSoup(html_content, "html.parser")
    for element in soup(["script", "style", "svg", "nav", "footer", "header", "noscript"]):
        element.decompose()
    return soup.get_text(separator="\n", strip=True)

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

@app.post("/verify", response_model=VerificationResponse)
async def verify_medical_license(request: VerificationRequest, api_key: str = Depends(validate_api_key)):
    clean_state = request.state.lstrip("=").strip().upper()
    clean_license = request.license_number.lstrip("=").strip()

    response_data = VerificationResponse(
        status="ERROR",
        state=clean_state,
        license_number=clean_license,
        message="An unknown error occurred."
    )

    if clean_state not in STATE_PORTALS:
        response_data.message = f"State '{clean_state}' is not supported yet."
        return response_data

    portal_info = STATE_PORTALS[clean_state]
    
    playwright = None
    browser = None
    context = None
    
    try:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        
        # Add realistic User-Agent to bypass basic bot filters
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        page.set_default_timeout(25000)
        
        # 1. Navigation
        await page.goto(portal_info["url"], wait_until="domcontentloaded")
        
        # 2. Form Interaction
        await page.fill(portal_info["input_selector"], clean_license)
        await page.click(portal_info["submit_selector"])
        
        # 3. Wait for DOM update instead of networkidle
        await page.wait_for_timeout(3000) 
        
        screenshot_bytes = await page.screenshot(full_page=False)
        b64_string = base64.b64encode(screenshot_bytes).decode("utf-8")
        
        raw_html = await page.content()
        cleaned_text = extract_clean_text_from_html(raw_html)
        
        # 4. AI Parsing
        prompt = f"""
        Extract verification details for license '{clean_license}' in {clean_state}:
        ---
        {cleaned_text[:12000]}
        ---
        Return ONLY JSON:
        {{
            "status": "CLEAR/ACTIVE | INACTIVE | NOT_FOUND",
            "expiration_date": "YYYY-MM-DD or N/A",
            "practitioner_name": "Full Name or N/A",
            "found": true/false
        }}
        """

        completion = await groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": "You output only valid JSON without markdown."},
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
        response_data.message = f"Timeout waiting for {clean_state} state website to respond."
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
