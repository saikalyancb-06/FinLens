"""
HDFC Bank Retail Net-Banking Adapter
--------------------------------------
Portal : https://netbanking.hdfcbank.com/netbanking/

Login flow (verified shape):
  1. Customer ID entered on home page
  2. Click "Continue"
  3. Secure Access Image/Phrase page — log phrase, click "Proceed"
  4. Password entry — HDFC offers a virtual keyboard (IPIN) OR plain input;
     detect and use whichever is visible
  5. Click "Login"
  6. OTP (registered mobile) → pause/resume

Statement flow:
  Accounts → Enquire → Account Statement
  → Select account → Set period → Choose format (PDF / XLS) → Download

HDFC limits statement range to ≤ 90 days per export; we respect that.

Bot-detection:
  HDFC uses Akamai Bot Manager. We run headful + stealth flags.
  If blocked, AntiBot is raised immediately with screenshot.
"""
import logging
import os

from playwright.async_api import Page

from app.rpa.base_adapter import BaseBankAdapter, OTPRequired, AntiBot
from app.rpa.helpers import (
    DebugHelper, VirtualKeyboard, SecureAccessStep,
    StatementExporter, human_delay, check_for_bot_block,
)

logger = logging.getLogger(__name__)


class HDFCAdapter(BaseBankAdapter):
    bank_display_name = "HDFC Bank"
    bank_key = "hdfc"

    _LOGIN_URL = "https://netbanking.hdfcbank.com/netbanking/"

    _SEL = {
        # Step 1 – Customer ID
        "cust_id_field":    "input[name='fldLoginUserId'], #userId, input[placeholder*='Customer']",
        "continue_btn":     "input[value='CONTINUE'], button.login-btn, #loginBtn",

        # Step 2 – Secure Access phrase
        "phrase_el":        "#hdfc_phrase, .secure-phrase, span.phrase-text, img.userimg + span",
        "phrase_continue":  "input[value='PROCEED'], button[value='Proceed'], #proceedBtn",

        # Step 3 – Password (VK or plain)
        "vk_container":     "#VirtualKeyboard, table.vkbd, div#vkeypad",
        "vk_key":           "#VirtualKeyboard td, table.vkbd td, div.vkey",
        "password_input":   "#fldPassword, input[name='fldPassword'], input[type='password']",
        "login_btn":        "input[value='LOGIN'], button[type='submit'], #loginsubmit",

        # Step 4 – OTP
        "otp_field":        "input[name='otpNumber'], #otpNum, input[placeholder*='OTP']",
        "otp_submit":       "input[value='SUBMIT'], button[value='Submit'], #otpsubmit",

        # Post-login
        "dashboard":        "#accounts-section, .account-summary, #menu-account",

        # Statement navigation
        "accounts_tab":     "a[href*='accounts'], #accountsMenu, text=Accounts",
        "enquire_link":     "a[href*='enquire'], text=Enquire",
        "stmt_link":        "a[href*='accountStatement'], text=Account Statement",
        "account_select":   "select[name='accountNo'], select#accountNo",
        "from_date":        "input[name='fromDate'], #fromDate",
        "to_date":          "input[name='toDate'], #toDate",
        "format_select":    "select[name='downloadAs'], select#downloadAs",
        "download_btn":     "input[value='DOWNLOAD'], button#downloadBtn",
    }

    def __init__(self, debug_mode: bool = False, job_id: str = ""):
        self.debug = DebugHelper(debug_mode, job_id)
        self._secure = SecureAccessStep(
            phrase_selector=self._SEL["phrase_el"],
            continue_btn=self._SEL["phrase_continue"],
        )
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
            max_range_days=90,    # HDFC caps at 90 days per export
        )

    async def login(self, page: Page, credentials: dict) -> None:
        logger.info("[HDFC] Navigating to NetBanking login")
        await page.goto(self._LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        await self.debug.step(page, "login_page_loaded")
        await check_for_bot_block(page, self.debug)

        # ── Step 1: Customer ID ──────────────────────────────────────────────
        cid_loc = page.locator(self._SEL["cust_id_field"]).first
        await cid_loc.wait_for(state="visible", timeout=15_000)
        await cid_loc.click()
        await human_delay(300, 600)
        await cid_loc.fill(credentials.get("username", ""))
        await self.debug.step(page, "customer_id_entered")
        await human_delay(500, 900)

        await page.locator(self._SEL["continue_btn"]).first.click()
        await human_delay(1500, 2500)
        await check_for_bot_block(page, self.debug)
        await self.debug.step(page, "after_continue")

        # ── Step 2: Secure Access phrase ─────────────────────────────────────
        await self._secure.handle(page, self.debug)
        await human_delay(600, 1200)

        # ── Step 3: Password (VK or plain) ──────────────────────────────────
        await self._vk.enter_password(page, credentials.get("password", ""), self.debug)
        await self.debug.step(page, "password_entered")
        await human_delay(400, 800)

        await page.locator(self._SEL["login_btn"]).first.click()
        await human_delay(2000, 3500)
        await check_for_bot_block(page, self.debug)

        # ── Step 4: OTP ──────────────────────────────────────────────────────
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
                "HDFC login did not reach the account dashboard. "
                "The Secure Access or password step may have failed, or the portal "
                "is blocking the automated session. Screenshot saved for debugging."
            )
        logger.info("[HDFC] Login successful")

    async def navigate_to_statements(self, page: Page, params: dict) -> None:
        await self.debug.step(page, "nav_accounts_start")
        logger.info("[HDFC] Navigating to Account Statement")

        await page.locator(self._SEL["accounts_tab"]).first.click()
        await human_delay(700, 1300)
        await page.locator(self._SEL["enquire_link"]).first.click()
        await human_delay(700, 1300)
        await page.locator(self._SEL["stmt_link"]).first.click()
        await human_delay(800, 1500)
        await self.debug.step(page, "statement_page_loaded")

        acc_sel = page.locator(self._SEL["account_select"])
        if await acc_sel.count() > 0:
            acct = params.get("account_number", "")
            try:
                if acct:
                    await acc_sel.first.select_option(value=acct)
                else:
                    await acc_sel.first.select_option(index=0)
            except Exception:
                pass
            await human_delay(400, 700)

    async def download_statement(self, page: Page, date_range: dict) -> str:
        upload_dir = os.path.join(os.getenv("UPLOAD_DIR", "uploads"), "rpa")
        return await self._exporter.export(page, date_range, upload_dir, self.debug)
