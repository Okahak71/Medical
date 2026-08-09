import base64
import json
import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from bs4 import BeautifulSoup
from groq import AsyncGroq

# Initialize FastAPI App
app = FastAPI(title="Florida Medical License Verification Service")

# Initialize Groq Client
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "your-groq-api-key-here")
groq_client = AsyncGroq(api_key=GROQ_API_KEY)


# --- Input & Output Data Schemas ---
class VerificationRequest(BaseModel):
    full_name: str = ""
    state: str
    license_number: str


class VerificationResponse(BaseModel):
    status: str
    expiration_date: str
    screenshot_base64: str


# --- HTML Cleaner Function ---
def clean_html_content(html_raw: str) -> str:
    """Strips out script tags, inline styles, and metadata to save tokens for Groq."""
    soup = BeautifulSoup(html_raw, "html.parser")
    for element in soup(["script", "style", "svg", "nav", "footer", "header", "noscript"]):
        element.decompose()
    return soup.get_text(separator="\n", strip=True)


# --- API Endpoint ---
@app.post("/verify", response_model=VerificationResponse)
async def verify_license(request: VerificationRequest):
    # Standardize inputs
    clean_license = request.license_number.strip().upper()
    target_url = "https://mqa-internet.doh.state.fl.us/MQASearchServices/HealthCareProviders"

    playwright = None
    browser = None
    context = None

    try:
        # 1. Start Async Playwright & Launch Browser
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )

        # Use a realistic User-Agent to prevent basic headless browser blocks
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        page.set_default_timeout(25000)

        # 2. Navigate to Florida MQA Search Portal
        await page.goto(target_url, wait_until="domcontentloaded")

        # 3. Enter License Number and Submit Search
        await page.fill("#LicenseNumber", clean_license)
        
        # Click search and wait for the results page or table to load
        await page.click("#btnSearch")
        await page.wait_for_load_state("domcontentloaded")
        
        # Short delay to ensure dynamic result rendering completes
        await page.wait_for_timeout(2000)

        # 4. Take Screenshot and convert to Base64
        screenshot_bytes = await page.screenshot(full_page=True)
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

        # 5. Extract and Clean Page Content for Groq
        raw_html = await page.content()
        cleaned_text = clean_html_content(raw_html)

        # 6. Groq AI Extraction
        prompt = f"""
        You are an expert compliance data extractor. Analyze the extracted text from the Florida Department of Health license verification portal for license '{clean_license}':

        ---
        {cleaned_text[:12000]}
        ---

        INSTRUCTIONS:
        1. Extract the license 'status' (e.g., "CLEAR/ACTIVE", "DELINQUENT", "EXPIRED", "REVOKED", "NOT_FOUND").
        2. Extract the 'expiration_date' (e.g., "01/31/2026").
        3. CRITICAL RULE FOR EXPIRATION DATE:
           - If the status is NOT active (e.g. Expired, Revoked, Delinquent, Null and Void, or Not Found), set 'expiration_date' strictly to "-".
           - Only return a valid date if the license status is actively practicing/clear.

        Return ONLY a JSON object formatted as follows:
        {{
            "status": "<EXTRACTED_STATUS>",
            "expiration_date": "<DATE_OR_HYPHEN>"
        }}
        """

        completion = await groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": "You are a precise JSON extractor. Output valid JSON only, without markdown formatting or introductory text."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.0
        )

        ai_response = json.loads(completion.choices[0].message.content)

        status_result = ai_response.get("status", "UNKNOWN")
        expiration_result = ai_response.get("expiration_date", "-")

        # Backup check: If status isn't active/clear, enforce expiration date as "-"
        if "CLEAR" not in status_result.upper() and "ACTIVE" not in status_result.upper():
            expiration_result = "-"

        return VerificationResponse(
            status=status_result,
            expiration_date=expiration_result,
            screenshot_base64=screenshot_b64
        )

    except PlaywrightTimeoutError:
        raise HTTPException(status_code=504, detail="Timeout while loading Florida state verification portal.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Verification failed: {str(e)}")
    finally:
        # Clean up browser resources
        if context:
            await context.close()
        if browser:
            await browser.close()
        if playwright:
            await playwright.stop()
