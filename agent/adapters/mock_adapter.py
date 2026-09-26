"""
Mock Bank Adapter & Local Mock Web Portal Server
------------------------------------------------
1. Runs a local HTTP mock server (127.0.0.1:8888) simulating a full bank portal (Login, CAPTCHA, OTP, Date Picker, PDF Statement Download).
2. Implements MockBankAdapter demonstrating full local agent lifecycle:
   QUEUED → LAUNCHING_BROWSER → LOGGING_IN → AWAITING_INPUT → NAVIGATING → DOWNLOADING → UPLOADING → PARSING → IMPORTING → SUCCESS.
"""
import os
import asyncio
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from typing import Optional
from playwright.async_api import Page

from agent.adapters.base_adapter import AgentBaseBankAdapter

logger = logging.getLogger(__name__)

# Sample CSV bank statement content for mock bank download
MOCK_STATEMENT_CSV = """Date,Description,Debit,Credit,Balance
2026-08-01,SALARY CREDIT KREDO TECH,0.00,150000.00,350000.00
2026-08-05,OFFICE SUPPLIES AMAZON,2450.00,0.00,347550.00
2026-08-10,ELECTRICITY BILL BESCOM,4200.00,0.00,343350.00
2026-08-15,DIVIDEND RECEIVED TCS LTD,0.00,8500.00,351850.00
"""

# Simple Base64 PNG image for CAPTCHA testing
TINY_CAPTCHA_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAAAXNSR0IArs4c6QAAAARnQU1BAACxjwv8YQUAAAAJcEhZcwAADsMAAA7DAcdvqGQAAAAhSURBVHhe7cExAQAAAMKg9U9tDQ8gAAAAAAAAAAAAAD4aW8AAAX174nQAAAAASUVORK5CYII="

MOCK_LOGIN_HTML = f"""<!DOCTYPE html>
<html>
<head><title>Mock Bank Portal</title></head>
<body style="font-family:sans-serif; padding:2rem; background:#f1f5f9;">
    <h2>Mock Bank NetBanking Login</h2>
    <form action="/login_submit" method="POST">
        <p><label>Username: <input type="text" id="txtUser" name="user" required></label></p>
        <p><label>Password: <input type="password" id="txtPass" name="pass" required></label></p>
        <p>
            <label>Visual CAPTCHA:</label><br/>
            <img id="captchaImg" src="data:image/png;base64,{TINY_CAPTCHA_PNG_B64}" style="border:1px solid #ccc; margin:5px 0;"/><br/>
            <input type="text" id="txtCaptcha" name="captcha" placeholder="Enter CAPTCHA (mock123)" required/>
        </p>
        <p><button type="submit" id="btnLogin">Login</button></p>
    </form>
</body>
</html>
"""

MOCK_OTP_HTML = """<!DOCTYPE html>
<html>
<head><title>Mock Bank - 2FA OTP</title></head>
<body style="font-family:sans-serif; padding:2rem; background:#f1f5f9;">
    <h2>Two-Factor Authentication (OTP)</h2>
    <form action="/otp_submit" method="POST">
        <p><label>Enter 6-digit OTP sent to mobile: <input type="text" id="txtOtp" name="otp" required></label></p>
        <p><button type="submit" id="btnOtp">Verify OTP</button></p>
    </form>
</body>
</html>
"""

MOCK_STATEMENTS_HTML = """<!DOCTYPE html>
<html>
<head><title>Mock Bank - Account Statements</title></head>
<body style="font-family:sans-serif; padding:2rem; background:#f1f5f9;">
    <h2>Download Account Statement</h2>
    <form action="/download_statement" method="GET">
        <p><label>From Date: <input type="date" id="fromDate" name="from" value="2026-08-01"></label></p>
        <p><label>To Date: <input type="date" id="toDate" name="to" value="2026-08-31"></label></p>
        <p><button type="submit" id="btnDownload">Download Statement (CSV)</button></p>
    </form>
</body>
</html>
"""


class MockBankHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress console log clutter

    def do_GET(self):
        if self.path.startswith("/download_statement"):
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Disposition", 'attachment; filename="mock_statement.csv"')
            self.end_headers()
            self.wfile.write(MOCK_STATEMENT_CSV.encode("utf-8"))
        elif self.path == "/statements":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(MOCK_STATEMENTS_HTML.encode("utf-8"))
        elif self.path == "/otp":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(MOCK_OTP_HTML.encode("utf-8"))
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(MOCK_LOGIN_HTML.encode("utf-8"))

    def do_POST(self):
        if self.path == "/login_submit":
            self.send_response(302)
            self.send_header("Location", "/otp")
            self.end_headers()
        elif self.path == "/otp_submit":
            self.send_response(302)
            self.send_header("Location", "/statements")
            self.end_headers()


class MockBankServer:
    """Helper to run Mock Bank HTTP server on background thread."""
    def __init__(self, port: int = 8888):
        self.port = port
        self.server = HTTPServer(("127.0.0.1", port), MockBankHTTPHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        logger.info(f"[MockBankServer] Server started at http://127.0.0.1:{self.port}")

    def stop(self):
        self.server.shutdown()
        logger.info("[MockBankServer] Server stopped.")


class MockBankAdapter(AgentBaseBankAdapter):
    bank_display_name = "Mock Bank"
    implementation_status = "live_verified"
    unattended_capable = False  # Requires interactive CAPTCHA & OTP ask()

    CONFIG = {
        "login_url": "http://127.0.0.1:8888/",
        "username_field": "#txtUser",
        "password_field": "#txtPass",
        "captcha_field": "#txtCaptcha",
        "captcha_img": "#captchaImg",
        "login_btn": "#btnLogin",
        "otp_field": "#txtOtp",
        "otp_submit_btn": "#btnOtp",
        "from_date_field": "#fromDate",
        "to_date_field": "#toDate",
        "download_btn": "#btnDownload",
    }

    async def login(self, page: Page, credentials: dict) -> None:
        cfg = self.CONFIG
        logger.info("[MockBankAdapter] Navigating to Mock Bank portal")
        await page.goto(cfg["login_url"], wait_until="networkidle")
        await self.human_delay(page)

        # Fill Username & Password
        await page.locator(cfg["username_field"]).fill(credentials.get("username", "testuser"))
        await page.locator(cfg["password_field"]).fill(credentials.get("password", "testpass"))

        # Capture CAPTCHA image bytes and ask local interaction system
        captcha_img_elem = page.locator(cfg["captcha_img"])
        captcha_bytes = await captcha_img_elem.screenshot()

        # Call local ask() interface (suspends Playwright, renders prompt on local GUI)
        captcha_answer = await self.ask(
            "captcha",
            {
                "title": "Mock Bank Visual CAPTCHA",
                "message": "Enter visual CAPTCHA text shown below:",
                "image_bytes": captcha_bytes,
            }
        )
        if not captcha_answer:
            captcha_answer = "mock123"

        await page.locator(cfg["captcha_field"]).fill(captcha_answer)
        await self.human_delay(page)
        await page.locator(cfg["login_btn"]).click()
        await page.wait_for_load_state("networkidle")

        # Handle 2FA OTP prompt if navigated to OTP page
        if await page.locator(cfg["otp_field"]).count() > 0:
            otp_val = credentials.get("otp")
            if not otp_val:
                # Ask local user for 6-digit OTP code via ask() interface
                otp_val = await self.ask(
                    "otp",
                    {
                        "title": "Mock Bank 2FA OTP Code",
                        "message": "Enter 6-digit OTP code sent to your registered mobile:",
                    }
                )
                if not otp_val:
                    otp_val = "123456"

            await page.locator(cfg["otp_field"]).fill(otp_val)
            await self.human_delay(page)
            await page.locator(cfg["otp_submit_btn"]).click()
            await page.wait_for_load_state("networkidle")

        logger.info("[MockBankAdapter] Login completed successfully")

    async def navigate_to_statements(self, page: Page, params: dict) -> None:
        logger.info("[MockBankAdapter] Reached account statement page")

    async def download_statement(self, page: Page, date_range: dict) -> str:
        cfg = self.CONFIG
        temp_dir = os.path.join(os.getenv("LOCALAPPDATA", os.path.expanduser("~")), "KredoAgent", "temp")
        os.makedirs(temp_dir, exist_ok=True)

        logger.info(f"[MockBankAdapter] Downloading statement range {date_range.get('start')} to {date_range.get('end')}")

        async with page.expect_download() as dl_info:
            await page.locator(cfg["download_btn"]).click()
        download = await dl_info.value

        dest_path = os.path.join(temp_dir, download.suggested_filename or "mock_statement.csv")
        await download.save_as(dest_path)
        logger.info(f"[MockBankAdapter] Downloaded statement saved to {dest_path}")
        return os.path.abspath(dest_path)
