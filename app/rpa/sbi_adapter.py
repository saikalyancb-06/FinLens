"""
ExampleBankAdapter
------------------
Placeholder concrete adapter.  All selectors / URLs are parameterised via
class-level CONFIG so replacing this with a real bank adapter is trivial:

    1. Subclass BaseBankAdapter  (or copy this file and rename).
    2. Fill in CONFIG with real selectors.
    3. Register in runner.ADAPTERS.

This adapter implements the SBI (State Bank of India) retail net-banking
flow as a concrete starting point.  Selectors are illustrative; update them
to match the live portal after testing with DEBUG_SCREENSHOT=True.
"""
import os
import asyncio
import logging
from playwright.async_api import Page

from app.rpa.base_adapter import BaseBankAdapter, OTPRequired, AntiBot

logger = logging.getLogger(__name__)


class SBIAdapter(BaseBankAdapter):
    bank_display_name = "SBI"

    def __init__(self, debug_mode: bool = False, job_id: str = ""):
        self.debug_mode = debug_mode
        self.job_id = job_id


    CONFIG = {
        "login_url": "https://www.onlinesbi.sbi/",
        "personal_banking_btn": "text=Personal Banking",
        "continue_btn": "text=CONTINUE",
        "username_field": "#txtUsername",
        "password_field": "#txtPassword",
        "login_btn": "#btnLogin",
        "otp_field": "#txtOTP",
        "otp_submit_btn": "#btnOTP",
        "statement_menu": "text=Account Statement",
        "from_date_field": "#txtFromDate",
        "to_date_field": "#txtToDate",
        "view_btn": "text=View",
        "download_pdf_btn": "text=Download PDF",
        "anti_bot_indicator": "text=Access Denied",
    }

    async def login(self, page: Page, credentials: dict) -> None:
        cfg = self.CONFIG
        logger.info("[SBIAdapter] Navigating to login URL")
        await page.goto(cfg["login_url"], wait_until="networkidle")
        await self.human_delay(page)

        # Check for anti-bot page
        if await page.locator(cfg["anti_bot_indicator"]).count() > 0:
            raise AntiBot("SBI portal returned Access Denied")

        # Click Personal Banking -> Continue
        await page.locator(cfg["personal_banking_btn"]).first.click()
        await self.human_delay(page)

        try:
            await page.locator(cfg["continue_btn"]).first.click()
            await self.human_delay(page)
        except Exception:
            pass  # Some portal versions skip this step

        # Fill credentials
        await page.locator(cfg["username_field"]).fill(credentials.get("username", ""))
        await self.human_delay(page, 200, 600)
        await page.locator(cfg["password_field"]).fill(credentials.get("password", ""))
        await self.human_delay(page, 300, 800)
        await page.locator(cfg["login_btn"]).click()

        # Wait briefly and detect OTP prompt
        await asyncio.sleep(2)
        if await page.locator(cfg["otp_field"]).count() > 0:
            otp_val = credentials.get("otp", "")
            if not otp_val:
                logger.info("[SBIAdapter] OTP prompt detected – raising OTPRequired")
                raise OTPRequired()
            # OTP already supplied (second call after user submitted via API)
            await page.locator(cfg["otp_field"]).fill(otp_val)
            await self.human_delay(page)
            await page.locator(cfg["otp_submit_btn"]).click()
            await asyncio.sleep(2)

        logger.info("[SBIAdapter] Login completed")

    async def navigate_to_statements(self, page: Page, params: dict) -> None:
        cfg = self.CONFIG
        logger.info("[SBIAdapter] Navigating to statements page")
        await page.locator(cfg["statement_menu"]).first.click()
        await self.human_delay(page, 800, 1500)
        logger.info("[SBIAdapter] Reached statements page")

    async def download_statement(self, page: Page, date_range: dict) -> str:
        cfg = self.CONFIG
        upload_dir = os.getenv("UPLOAD_DIR", "uploads")
        os.makedirs(upload_dir, exist_ok=True)

        logger.info(f"[SBIAdapter] Setting date range {date_range['start']} → {date_range['end']}")
        await page.locator(cfg["from_date_field"]).fill(date_range["start"])
        await self.human_delay(page, 200, 500)
        await page.locator(cfg["to_date_field"]).fill(date_range["end"])
        await self.human_delay(page)
        await page.locator(cfg["view_btn"]).click()
        await self.human_delay(page, 1000, 2000)

        # Intercept download
        async with page.expect_download() as dl_info:
            await page.locator(cfg["download_pdf_btn"]).click()
        download = await dl_info.value

        dest = os.path.join(upload_dir, download.suggested_filename or f"sbi_statement_{date_range['start']}.pdf")
        await download.save_as(dest)
        logger.info(f"[SBIAdapter] Statement saved to {dest}")
        return os.path.abspath(dest)
