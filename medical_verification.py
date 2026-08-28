import base64
import json
import logging
import os
import re
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from PIL import Image
from groq import AsyncGroq

logger = logging.getLogger("uvicorn.error")

app = FastAPI(title="Florida License Verification Service via Groq Vision")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

groq_client = AsyncGroq(api_key=GROQ_API_KEY)

SERVICE_API_KEY = os.getenv("SERVICE_API_KEY", "")


def check_api_key(x_api_key: str = Header(default=None)):
    if SERVICE_API_KEY and x_api_key != SERVICE_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


# --- Input & Output Data Schemas for n8n ---
class VerificationRequest(BaseModel):
    full_name: str = ""
    state: str = ""
    license_number: str


class VerificationResponse(BaseModel):
    verdict_status: str
    expiration_date: str
    screenshot: str


def clean_json_response(raw_response: str) -> dict:
    """Safely extracts JSON from AI text without throwing char 0 decode errors."""
    if not raw_response or not raw_response.strip():
        return {"verdict_status": "NOT FOUND", "expiration_date": "-"}

    match = re.search(r"\{.*\}", raw_response, re.DOTALL)
    if not match:
        return {"verdict_status": "PARSE_ERROR", "expiration_date": "-"}
        
    json_str = match.group(0)
    
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        return {"verdict_status": "PARSE_ERROR", "expiration_date": "-"}


# --- API Endpoint ---
@app.post("/verify", response_model=VerificationResponse)
async def verify_license(request: VerificationRequest, x_api_key: str = Header(default=None)):
    check_api_key(x_api_key)
    clean_license = request.license_number.strip().upper()
    target_url = "https://mqa-internet.doh.state.fl.us/MQASearchServices/HealthCareProviders"

    playwright = None
    browser = None
    context = None

    try:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )

        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        page.set_default_timeout(25000)

        await page.goto(target_url, wait_until="domcontentloaded")
        await page.fill("#SearchDto_LicenseNumber", clean_license)
        async with page.expect_navigation(wait_until="domcontentloaded"):
            await page.click("input[type='submit'][value='Search']")

        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(2500)

        screenshot_bytes = await page.screenshot(clip={"x" : 0, "y" : 100, "height" : 500, "width" : 1000})
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

        completion = await groq_client.chat.completions.create(
            model="qwen/qwen3.6-27b",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"""
                            Analyze this screenshot of a license verification search result for license '{clean_license}':

                            TASK:
                            
                            1. Extract the 'verdict_status' (e.g., "CLEAR/ACTIVE", "DELINQUENT", "EXPIRED", "REVOKED", "NOT FOUND").
                            2. Extract the 'expiration_date' (e.g., "01/31/2026").
                            3. RULE FOR EXPIRATION DATE:
                               - If the status is NOT active/clear (e.g., Expired, Revoked, Delinquent, Null and Void, or Not Found), set 'expiration_date' strictly to "-".
                               - Only return a valid date if the license status is explicitly active or clear.
                            4. If a "NOT FOUND" page appears, set 'verdict_status' = "NOT FOUND" and 'expiration_date' = "-"

                            Return ONLY a JSON object:
                            {{
                                "verdict_status": "<EXTRACTED_STATUS>",
                                "expiration_date": "<DATE_OR_HYPHEN>"
                            }}
                            """
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{screenshot_b64}"
                            }
                        }
                    ]
                }
            ],
            temperature=0.0,
            response_format={"type": "json_object"}
        )

        raw_ai_text = completion.choices[0].message.content
        ai_data = clean_json_response(raw_ai_text)

        status_result = ai_data.get("verdict_status", "NOT FOUND")
        expiration_result = ai_data.get("expiration_date", "-")

        if status_result == "NOT FOUND":
            crop_corr = (342, 75, 599, 232)
            screenshot_bytes = screenshot_bytes.crop(crop_corr)
        
        return VerificationResponse(
            verdict_status=status_result,
            expiration_date=expiration_result,
            screenshot=screenshot_b64
        )

    except PlaywrightTimeoutError:
        raise HTTPException(
            status_code=504, 
            detail="Timeout waiting for Florida state verification website to respond."
        )
    except Exception as e:
        raise HTTPException(
            status_code=500, 
            detail=f"Verification execution failed: {str(e)}"
        )
    finally:
        # Resource cleanup
        if context:
            await context.close()
        if browser:
            await browser.close()
        if playwright:
            await playwright.stop()
