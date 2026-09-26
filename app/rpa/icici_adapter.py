"""
ICICI Bank Retail Net-Banking Adapter
--------------------------------------
Portal : https://www.icicibank.com/  →  "Login" → Personal Net Banking
NOT the corporate/CIB portal.

Login flow (verified shape, selectors must be confirmed live):
  1. Click "Login" on homepage → redirected to retail login page
  2. Enter User ID
  3. Click "Login" / "Continue"
  4. Virtual Keyboard password entry (ICICI uses a JS on-screen keypad)
  5. Click "Login"
  6. OTP prompt (registered mobile) → pause/resume via shared OTP step

Statement flow:
  My Accounts → Account Statement → select account → set date range
  → choose format (PDF / Excel) → Download

Bot-detection notes:
  ICICI runs Imperva / PerimeterX and detects headless Chrome.
  We run headful + stealth args. If blocked we raise AntiBot immediately.

DEBUG MODE:
  Pass debug=True to the runner to enable step screenshots & slow-motion.
"""
import asyncio
import logging
import os

from playwright.async_api import Page

from app.rpa.base_adapter import BaseBankAdapter, OTPRequired, AntiBot
from app.rpa.helpers import (
    DebugHelper, VirtualKeyboard, StatementExporter,
    human_delay, check_for_bot_block,
)

logger = logging.getLogger(__name__)


class ICICIAdapter(BaseBankAdapter):
    bank_display_name = "ICICI Bank"
    bank_key = "icici"

    # ── Selectors (verify live before use — ICICI updates DOM periodically) ──
    _LOGIN_URL = "https://www.icicibank.com/retail/auth/loginPage.do"
    _ALT_LOGIN_URL = "https://infinity.icicibank.com/corp/AuthenticationController"

    _SEL = {
        # Step 1 – User ID
        "userid_field":     "#userId, #user-id, input[name='userId'], input[placeholder*='User ID']",
        "userid_submit":    "#loginSubmitBtn, button[type='submit'], input[value='LOGIN']",

        # Step 2 – Virtual Keyboard password
        "vk_container":     "#softKeyboard, #virtualKeyboard, table.keyboard-table, div#keyPad",
        "vk_key":           "#softKeyboard td, #virtualKeyboard td, div.key, span.key-btn",
        "password_input":   "#password, input[type='password'], input[name='IMAGECAPTCHA']",

        # Step 3 – OTP
        "otp_field":        "#otpNum, input[name='otp'], input[placeholder*='OTP']",
        "otp_submit":       "#validateOtpBtn, button[type='submit']",

        # Post-login indicator
        "dashboard":        "#accountSummary, .account-summary, #myAccountsLink",

        # Anti-bot
        "anti_bot":         "text=Access Denied, text=Robot Check, text=Please verify",

        # Statement navigation
        "accounts_menu":    "#myAccountsLink, a[href*='accounts'], text=My Accounts",
        "stmt_link":        "a[href*='statement'], text=Account Statement",
        "account_select":   "select#accountNo, select[name='accountNo']",
        "from_date":        "#fromDate, input[name='fromDate']",
        "to_date":          "#toDate, input[name='toDate']",
        "format_select":    "select#downloadType, select[name='downloadType']",
        "download_btn":     "#downloadStatement, button[value='Download'], input[value='Download']",
    }

    def __init__(self, debug_mode: bool = False, job_id: str = ""):
        self.debug = DebugHelper(debug_mode, job_id)
        self._vk = VirtualKeyboard(
            vk_container=self._SEL["vk_container"],
            vk_key=self._SEL["vk_key"],
            normal_input=self._SEL["password_input"],
        )
        self._exporter = StatementExporter(
            from_date_sel=self._SEL["from_date"],
            to_date_sel=self._SEL["to_date"],
            format_sel=self._SEL["format_select"],
            format_value="PDF",
            download_btn=self._SEL["download_btn"],
            date_format="%d/%m/%Y",
            max_range_days=180,
        )

    async def login(self, page: Page, credentials: dict) -> None:
        logger.info("[ICICI] Navigating to retail login URL")
        await page.goto(self._LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        await self.debug.step(page, "login_page_loaded")
        await check_for_bot_block(page, self.debug)

        # ── Step 1: User ID ──────────────────────────────────────────────────
        uid_loc = page.locator(self._SEL["userid_field"]).first
        await uid_loc.wait_for(state="visible", timeout=15_000)
        await uid_loc.click()
        await human_delay(300, 600)
        await uid_loc.fill(credentials.get("username", ""))
        await self.debug.step(page, "userid_entered")
        await human_delay(400, 800)

        await page.locator(self._SEL["userid_submit"]).first.click()
        await human_delay(1000, 2000)
        await check_for_bot_block(page, self.debug)
        await self.debug.step(page, "after_userid_submit")

        # ── Step 2: Password (VK or plain) ──────────────────────────────────
        await self._vk.enter_password(page, credentials.get("password", ""), self.debug)
        await self.debug.step(page, "password_entered")
        await human_delay(400, 700)

        # Click the main login button
        try:
            await page.locator(self._SEL["userid_submit"]).first.click()
        except Exception:
            await page.keyboard.press("Enter")
        await human_delay(1500, 3000)
        await check_for_bot_block(page, self.debug)

        # ── Step 3: OTP ──────────────────────────────────────────────────────
        otp_loc = page.locator(self._SEL["otp_field"])
        if await otp_loc.count() > 0 and await otp_loc.first.is_visible():
            otp_val = credentials.get("otp", "")
            if not otp_val:
                await self.debug.step(page, "otp_prompt_detected")
                raise OTPRequired()
            await otp_loc.first.fill(otp_val)
            await human_delay(300, 600)
            await page.locator(self._SEL["otp_submit"]).first.click()
            await human_delay(2000, 3000)
            await check_for_bot_block(page, self.debug)
            await self.debug.step(page, "otp_submitted")

        # ── Confirm login ────────────────────────────────────────────────────
        try:
            await page.wait_for_selector(self._SEL["dashboard"], timeout=15_000)
        except Exception:
            await self.debug.error_screenshot(page, "login_failed")
            raise AntiBot(
                "ICICI login did not reach the dashboard page. "
                "The bank may have changed its login flow or is blocking automation. "
                "A screenshot has been saved for debugging."
            )
        logger.info("[ICICI] Login successful")

    async def navigate_to_statements(self, page: Page, params: dict) -> None:
        await self.debug.step(page, "nav_accounts_start")
        logger.info("[ICICI] Navigating to Account Statement")
        await page.locator(self._SEL["accounts_menu"]).first.click()
        await human_delay(800, 1500)
        await page.locator(self._SEL["stmt_link"]).first.click()
        await human_delay(800, 1500)
        await self.debug.step(page, "statement_page_loaded")

        # Select account if dropdown exists
        acc_sel = page.locator(self._SEL["account_select"])
        if await acc_sel.count() > 0:
            acct = params.get("account_number", "")
            if acct:
                try:
                    await acc_sel.first.select_option(value=acct)
                except Exception:
                    await acc_sel.first.select_option(index=0)
            else:
                await acc_sel.first.select_option(index=0)
            await human_delay(400, 700)

    async def download_statement(self, page: Page, date_range: dict) -> str:
        upload_dir = os.path.join(os.getenv("UPLOAD_DIR", "uploads"), "rpa")
        return await self._exporter.export(page, date_range, upload_dir, self.debug)
