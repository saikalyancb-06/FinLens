"""
Canara Bank Retail Net-Banking Adapter
----------------------------------------
Portal: https://canarabank.com/ → Net Banking (post-Syndicate Bank merger)
The merged Canara Bank portal is now at: https://canarabank.com/net-banking
(Also accessible via: https://netbanking.canarabank.in/)

Login flow (post-merger, verified shape):
  1. Navigate to net-banking login page
  2. Enter User ID
  3. Enter Login Password (virtual keyboard may be present — detect and handle)
  4. Click Login
  5. OTP on registered mobile → pause/resume

Statement ("e-Pass Sheet") flow:
  Accounts → Account Statement / e-Pass Sheet
  → select account → set date range → export (PDF or Excel)

Canara may require a separate Transaction Password for statement download.
If a second password prompt appears, we treat it like a second OTP step
and surface it as OTPRequired so the user can supply it via the API.

Portal note:
  After the Syndicate merger the UI went through two redesigns (2021, 2023).
  All selectors below are parameterised — update CONFIG to match live DOM.
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


class CanaraAdapter(BaseBankAdapter):
    bank_display_name = "Canara Bank"
    bank_key = "canara"

    _LOGIN_URL = "https://netbanking.canarabank.in/Login.aspx"
    _ALT_LOGIN_URL = "https://canarabank.com/net-banking"

    _SEL = {
        # Step 1 – User ID
        "userid_field":       "#txtUserId, input[name='txtUserId'], input[placeholder*='User ID']",
        "loginpass_field":    "#txtLoginPassword, input[name='txtLoginPassword'], input[type='password']",

        # Virtual keyboard (if present)
        "vk_container":       "#divVirtualKeyboard, .vKeyboard, #virtualKeypad",
        "vk_key":             "#divVirtualKeyboard td, .vKeyboard span, #virtualKeypad li",

        # Login button
        "login_btn":          "#btnLogin, input[value='Login'], button[type='submit']",

        # OTP step (login)
        "login_otp_field":    "#txtOTP, input[name='OTP'], input[placeholder*='OTP']",
        "login_otp_submit":   "#btnSubmitOTP, input[value='Validate OTP'], button[type='submit']",

        # Post-login dashboard
        "dashboard":          "#divAccountSummary, .account-section, #mainContent",

        # Statement navigation
        "accounts_menu":      "#liAccounts, a[href*='Account'], text=Accounts",
        "stmt_link":          "a[href*='AccountStatement'], a[href*='ePassSheet'], text=Account Statement, text=e-Pass Sheet",

        # Statement form
        "account_select":     "select#ddlAccount, select[name='ddlAccount']",
        "from_date":          "#txtFromDate, input[name='FromDate']",
        "to_date":            "#txtToDate, input[name='ToDate']",
        "format_select":      "select#ddlFormat, select[name='Format']",
        "download_btn":       "#btnDownload, input[value='Download'], input[value='Submit']",

        # Optional: Transaction password prompt (for statement download)
        "txn_pwd_field":      "#txtTransPassword, input[name='TransPassword'], input[placeholder*='Transaction']",
        "txn_pwd_submit":     "#btnTxnSubmit, input[value='Submit'], button[type='submit']",

        # Anti-bot
        "anti_bot":           "text=Access Denied, text=Blocked, text=Robot Check",
    }

    def __init__(self, debug_mode: bool = False, job_id: str = ""):
        self.debug = DebugHelper(debug_mode, job_id)
        self._vk = VirtualKeyboard(
            vk_container=self._SEL["vk_container"],
            vk_key=self._SEL["vk_key"],
            normal_input=self._SEL["loginpass_field"],
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
        self._credentials = dict(credentials)
        logger.info("[Canara] Navigating to net-banking login")
        try:
            await page.goto(self._LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        except Exception:
            logger.warning("[Canara] Primary URL failed, trying alternate")
            await page.goto(self._ALT_LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)

        await self.debug.step(page, "login_page_loaded")
        await check_for_bot_block(page, self.debug)

        # ── Step 1: User ID ──────────────────────────────────────────────────
        uid_loc = page.locator(self._SEL["userid_field"]).first
        await uid_loc.wait_for(state="visible", timeout=15_000)
        await uid_loc.click()
        await human_delay(300, 600)
        await uid_loc.fill(credentials.get("username", ""))
        await self.debug.step(page, "userid_entered")
        await human_delay(500, 900)

        # ── Step 2: Password (VK or plain) ──────────────────────────────────
        await self._vk.enter_password(page, credentials.get("password", ""), self.debug)
        await self.debug.step(page, "password_entered")
        await human_delay(400, 800)

        await page.locator(self._SEL["login_btn"]).first.click()
        await human_delay(1500, 3000)
        await check_for_bot_block(page, self.debug)
        await self.debug.step(page, "after_login_click")

        # ── Step 3: OTP ──────────────────────────────────────────────────────
        otp_loc = page.locator(self._SEL["login_otp_field"])
        if await otp_loc.count() > 0 and await otp_loc.first.is_visible():
            otp_val = credentials.get("otp", "")
            if not otp_val:
                await self.debug.step(page, "otp_prompt_detected")
                raise OTPRequired()
            await otp_loc.first.fill(otp_val)
            await human_delay(300, 600)
            await page.locator(self._SEL["login_otp_submit"]).first.click()
            await human_delay(2000, 3000)
            await check_for_bot_block(page, self.debug)
            await self.debug.step(page, "otp_submitted")

        # ── Confirm login ────────────────────────────────────────────────────
        try:
            await page.wait_for_selector(self._SEL["dashboard"], timeout=15_000)
        except Exception:
            await self.debug.error_screenshot(page, "login_failed")
            raise AntiBot(
                "Canara Bank login did not reach the account dashboard. "
                "The portal may have changed its layout (post-merger), or the "
                "automation is being blocked. Screenshot saved for selector debugging."
            )
        logger.info("[Canara] Login successful")

    async def navigate_to_statements(self, page: Page, params: dict) -> None:
        await self.debug.step(page, "nav_accounts_start")
        logger.info("[Canara] Navigating to e-Pass Sheet / Account Statement")

        await page.locator(self._SEL["accounts_menu"]).first.click()
        await human_delay(800, 1500)
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

        # Handle optional Transaction Password prompt (Canara sometimes requires it
        # before showing the statement form — treat as a second OTP step)
        txn_pwd_loc = page.locator(self._SEL["txn_pwd_field"])
        if await txn_pwd_loc.count() > 0 and await txn_pwd_loc.first.is_visible():
            txn_pwd = getattr(self, "_credentials", {}).get("transaction_password", "")
            if not txn_pwd:
                logger.info("[Canara] Transaction password prompt detected")
                raise OTPRequired()   # UI will show OTP modal; user submits txn_pwd as "OTP"
            await txn_pwd_loc.first.fill(txn_pwd)
            await human_delay(300, 600)
            await page.locator(self._SEL["txn_pwd_submit"]).first.click()
            await human_delay(1000, 2000)

        return await self._exporter.export(page, date_range, upload_dir, self.debug)
