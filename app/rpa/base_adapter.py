"""
BaseBankAdapter
---------------
Abstract base class all bank-specific RPA adapters must implement.

Adding a new bank = subclass this + register in runner.ADAPTERS dict.
No bank-specific logic belongs in the runner or routes.
"""
from abc import ABC, abstractmethod
from playwright.async_api import Page


class OTPRequired(Exception):
    """Raised by login() when the portal presents an OTP / 2FA prompt."""
    pass


class AntiBot(Exception):
    """Raised when the portal returns a CAPTCHA or bot-detection page."""
    pass


class BaseBankAdapter(ABC):

    # Human-readable name used in logs and the UI
    bank_display_name: str = "Bank"

    @abstractmethod
    async def login(self, page: Page, credentials: dict) -> None:
        """
        Navigate to the bank login URL and authenticate.

        credentials keys (all optional per bank):
            username, password, otp, account_number, dob

        Raises:
            OTPRequired – when a one-time-password prompt is detected.
            AntiBot    – when a bot-detection page is detected.
        """

    @abstractmethod
    async def navigate_to_statements(self, page: Page, params: dict) -> None:
        """
        Move from the post-login home page to the statement download page.

        params may carry bank-specific navigation hints (account_type, etc.).
        """

    @abstractmethod
    async def download_statement(self, page: Page, date_range: dict) -> str:
        """
        Set the desired date range, trigger the download, wait for it to finish.

        date_range keys: start (YYYY-MM-DD), end (YYYY-MM-DD)

        Returns:
            Absolute path to the saved file (PDF / CSV / XLSX).
        """

    async def handle_otp(self, page: Page, otp: str) -> None:
        """Optional OTP hook for adapters that separate OTP entry from login."""
        return None

    async def cleanup_logout(self, page: Page) -> None:
        """Optional cleanup hook for adapters that support a portal logout step."""
        return None

    # ------------------------------------------------------------------ helpers
    @staticmethod
    async def human_delay(page: Page, min_ms: int = 400, max_ms: int = 1200) -> None:
        """Introduce a randomised human-like pause to reduce bot fingerprint."""
        import random, asyncio
        delay = random.randint(min_ms, max_ms) / 1000
        await asyncio.sleep(delay)
