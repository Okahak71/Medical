import asyncio
from playwright.async_api import async_playwright

async def run_test():
    async with async_playwright() as p:
        # Launch browser visually so you can see it working
        browser = await p.chromium.launch(headless=False, slow_mo=500)
        page = await browser.new_page(viewport={'width': 1280, 'height': 800})

        print("1. Navigating to Florida Medical/Nursing Portal...")
        await page.goto("https://mqa-internet.doh.state.fl.us/MQASearchServices/HealthCareProviders")

        print("2. Selecting License Type...")
        # Select "Registered Nurse" from dropdown
        await page.select_option("#ContentPlaceHolder1_ddlProfession", label="Registered Nurse")

        print("3. Entering Test License Number...")
        # Enter a test license number field
        await page.fill("#ContentPlaceHolder1_txtLicenseNumber", "99999")

        print("4. Clicking Search...")
        await page.click("#ContentPlaceHolder1_btnSubmit")

        print("5. Waiting for Results Page...")
        await page.wait_for_load_state("networkidle")

        print("6. Capturing Screenshot Proof...")
        await page.screenshot(path="florida_test_proof.png", full_page=True)

        print("SUCCESS! Verification complete. Check 'florida_test_proof.png' in your folder.")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run_test())
