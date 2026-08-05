import base64
import logging
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# Initialize API
app = FastAPI(title="License Verification API")
logging.basicConfig(level=logging.INFO)

# Define the expected JSON payload format
class VerificationRequest(BaseModel):
    state: str
    license_number: str

# Health check endpoint for UptimeRobot (Keeps the server awake)
@app.get("/health")
async def health_check():
    return {"status": "awake"}

# Main Scraping Endpoint
@app.post("/verify")
async def verify_license(req: VerificationRequest):
    async with async_playwright() as p:
        # 1. Launch Headless Browser (Configured for Linux Cloud Servers)
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled", 
                "--no-sandbox", 
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage"
            ]
        )
        
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        
        page = await context.new_page()

        try:
            # 2. Navigate to the state board URL
            # Note: Replace this with the actual URL you are targeting.
            # For testing, we are injecting a mock HTML response directly.
            mock_html = f"""
            <!DOCTYPE html>
            <html>
            <body>
                <h2>Verification for {req.license_number} ({req.state})</h2>
                <table id="resultsTable">
                    <tr><th>Status</th><th>Expiry</th></tr>
                    <tr><td class="status-active">ACTIVE</td><td>2027-10-31</td></tr>
                </table>
            </body>
            </html>
            """
            
            # Simulate navigating to the site and loading the data
            await page.set_content(mock_html)
            
            # 3. Extract the Data
            await page.wait_for_selector("#resultsTable")
            status_text = await page.locator(".status-active").inner_text()
            
            # 4. Capture Screenshot to memory (NOT to disk)
            screenshot_bytes = await page.screenshot(full_page=True)
            screenshot_base64 = base64.b64encode(screenshot_bytes).decode('utf-8')
            
            return {
                "success": True,
                "state": req.state,
                "license_number": req.license_number,
                "status": status_text.strip(),
                "expiration_date": "2027-10-31",
                "screenshot_base64": screenshot_base64
            }

        except PlaywrightTimeoutError:
            raise HTTPException(status_code=504, detail="Timeout while waiting for state board website.")
        except Exception as e:
            logging.error(f"Execution Error: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            # Always close the browser to prevent memory leaks on your free server
            await browser.close()