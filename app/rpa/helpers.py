"""
RPA Shared Helpers
------------------
Reusable utilities shared across all bank adapters.

Modules:
  - VirtualKeyboard   : click on-screen keyboard keys in order
  - SecureAccessStep  : handle multi-step login (userId -> image/phrase page -> password)
  - StatementExporter : set date range, choose format, trigger download
  - DebugHelper       : screenshot at each step, slow-motion logging

All helpers are async and accept a `Page` + optional context.
"""
import asyncio
import logging
import os
import random
import re
from datetime import datetime
from typing import Optional, List

from playwright.async_api import Page, Locator

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────
_SCREENSHOT_DIR = os.path.join(os.getenv("UPLOAD_DIR", "uploads"), "rpa_screens")
_DEBUG_SLOWMO_MS = 600   # extra delay per action in debug mode


# ──────────────────────────────────────────────────────────────────────────────
# Human-like randomised delay
# ──────────────────────────────────────────────────────────────────────────────
async def human_delay(min_ms: int = 350, max_ms: int = 950) -> None:
    """Randomised human-like pause to reduce bot fingerprint."""
    await asyncio.sleep(random.randint(min_ms, max_ms) / 1000)


async def human_type(page: Page, selector: str, text: str, delay_ms: int = 80) -> None:
    """Type character-by-character with per-keystroke jitter (less bot-like)."""
    await page.locator(selector).click()
    for ch in text:
        await page.keyboard.type(ch, delay=delay_ms + random.randint(-20, 40))


# ──────────────────────────────────────────────────────────────────────────────
# Debug Helper
# ──────────────────────────────────────────────────────────────────────────────
class DebugHelper:
    """
    Wrap key actions with screenshots and verbose logging when debug_mode=True.
    Pass an instance into each adapter.
    """

    def __init__(self, debug_mode: bool = False, job_id: str = ""):
        self.debug_mode = debug_mode
        self.job_id = job_id
        self._step = 0
        os.makedirs(_SCREENSHOT_DIR, exist_ok=True)

    async def step(self, page: Page, label: str) -> str:
        """Take a screenshot (in debug mode) and log the step. Returns screenshot path."""
        self._step += 1
        path = ""
        if self.debug_mode:
            fname = f"rpa_{self.job_id}_step{self._step:02d}_{label.replace(' ', '_')[:40]}.png"
            path = os.path.join(_SCREENSHOT_DIR, fname)
            try:
                await page.screenshot(path=path, full_page=True)
                logger.info(f"[DEBUG step {self._step}] {label} — screenshot: {path}")
            except Exception as e:
                logger.warning(f"[DEBUG] Screenshot failed: {e}")
            await asyncio.sleep(_SLOWMO / 1000)
        else:
            logger.debug(f"[step {self._step}] {label}")
        return path

    async def error_screenshot(self, page: Page, label: str = "error") -> str:
        """Always take a screenshot on errors, regardless of debug_mode."""
        os.makedirs(_SCREENSHOT_DIR, exist_ok=True)
        fname = f"rpa_{self.job_id}_ERROR_{label[:40]}.png"
        path = os.path.join(_SCREENSHOT_DIR, fname)
        try:
            await page.screenshot(path=path, full_page=True)
            logger.error(f"[RPA ERROR] screenshot saved: {path}")
        except Exception:
            pass
        return path


_SLOWMO = _DEBUG_SLOWMO_MS


# ──────────────────────────────────────────────────────────────────────────────
# Virtual Keyboard Helper
# ──────────────────────────────────────────────────────────────────────────────
class VirtualKeyboard:
    """
    Detect and interact with on-screen software keyboards used by Indian banks
    to defeat keyloggers.

    Strategy:
    1. Try normal keyboard input first.
    2. If a virtual keyboard container is detected, find clickable key elements
       whose text matches each character of the password, click them in sequence.
    3. Fall back gracefully: if a character is missing from the virtual keys
       (e.g. keyboard uses images), log a warning and skip.

    Typical virtual-keyboard selectors (varies by bank):
      - ICICI : table.vkbd td, div.vkey
      - HDFC  : table#VirtualKeyboard td
      - Canara: span.keypad-btn, li.vk_key

    Pass bank-specific selectors via `vk_container` and `vk_key`.
    """

    def __init__(
        self,
        vk_container: str = "",          # CSS selector for the VK root element
        vk_key: str = "",                # CSS selector for individual key elements
        normal_input: str = "",          # CSS selector for plain-text fallback input
    ):
        self.vk_container = vk_container
        self.vk_key = vk_key
        self.normal_input = normal_input

    async def is_present(self, page: Page) -> bool:
        """Return True if the virtual keyboard container is visible."""
        if not self.vk_container:
            return False
        try:
            loc = page.locator(self.vk_container)
            return await loc.is_visible()
        except Exception:
            return False

    async def enter_password(self, page: Page, password: str, debug: DebugHelper = None) -> bool:
        """
        Enter `password` using either the virtual keyboard or a plain input.
        Returns True if VK was used, False if plain input was used.
        """
        if await self.is_present(page):
            logger.info("[VirtualKeyboard] Virtual keyboard detected — clicking keys")
            if debug:
                await debug.step(page, "VirtualKeyboard_detected")
            await self._click_vk_keys(page, password)
            return True
        elif self.normal_input:
            logger.info("[VirtualKeyboard] No VK — using plain input")
            await human_type(page, self.normal_input, password)
            return False
        else:
            logger.warning("[VirtualKeyboard] Neither VK nor plain input selector configured")
            return False

    async def _click_vk_keys(self, page: Page, password: str) -> None:
        """Click each key of the password on the virtual keyboard."""
        keys = page.locator(self.vk_key)
        key_count = await keys.count()
        if key_count == 0:
            logger.error("[VirtualKeyboard] No key elements found — falling back to clipboard paste")
            await page.evaluate(
                f"navigator.clipboard.writeText('{password}')"
            )
            return

        # Build a map: character → list[locator index]
        char_map: dict[str, list[int]] = {}
        for i in range(key_count):
            key_el = keys.nth(i)
            try:
                txt = (await key_el.inner_text()).strip()
                if txt:
                    char_map.setdefault(txt, []).append(i)
            except Exception:
                pass

        for ch in password:
            indices = char_map.get(ch, [])
            if not indices:
                logger.warning(f"[VirtualKeyboard] Key '{ch}' not found on virtual keyboard — skipping")
                continue
            idx = random.choice(indices)   # some banks randomise key positions
            await keys.nth(idx).click()
            await asyncio.sleep(random.randint(80, 200) / 1000)


# ──────────────────────────────────────────────────────────────────────────────
# Secure Access / Anti-phishing phrase step
# ──────────────────────────────────────────────────────────────────────────────
class SecureAccessStep:
    """
    Handle the intermediate "we show you your personal image/phrase" page that
    some banks display between entering the User ID and the password step.

    Flow:
      1. User enters ID → Continue.
      2. Bank shows personalised image + phrase to prove it's the real site.
      3. User sees the expected phrase → clicks "Yes, proceed" / "Continue".
      4. Password field appears.

    The helper just logs the displayed phrase and clicks the continue button —
    the human verifying the phrase is out of scope (they do it visually).
    """

    def __init__(
        self,
        phrase_selector: str = "",        # element that displays the phrase/image alt
        continue_btn: str = "",           # button to click after phrase is shown
    ):
        self.phrase_selector = phrase_selector
        self.continue_btn = continue_btn

    async def is_present(self, page: Page) -> bool:
        if not self.phrase_selector:
            return False
        try:
            return await page.locator(self.phrase_selector).is_visible()
        except Exception:
            return False

    async def handle(self, page: Page, debug: DebugHelper = None) -> None:
        if not await self.is_present(page):
            return
        try:
            phrase = await page.locator(self.phrase_selector).inner_text()
            logger.info(f"[SecureAccess] Displayed phrase/image alt: '{phrase.strip()}'")
        except Exception:
            logger.info("[SecureAccess] Phrase element found but could not read text")
        if debug:
            await debug.step(page, "SecureAccess_phrase_shown")
        await human_delay(600, 1200)
        if self.continue_btn:
            try:
                await page.locator(self.continue_btn).click()
                logger.info("[SecureAccess] Clicked continue button past phrase page")
            except Exception as e:
                logger.warning(f"[SecureAccess] Continue button click failed: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# Statement Export Helper
# ──────────────────────────────────────────────────────────────────────────────
class StatementExporter:
    """
    Generic helper for the common statement-download flow:
      1. Fill date range (from / to) using date pickers or text inputs.
      2. Optionally select export format (PDF / Excel / CSV).
      3. Click the download trigger and await the Playwright download event.
      4. Save to the upload directory.
      5. If PDF is password-protected, rename with _NEEDS_PASSWORD suffix
         and return the path — never fail the run.

    Each bank subclass configures the selectors; the logic is shared.
    """

    def __init__(
        self,
        from_date_sel: str = "",
        to_date_sel: str = "",
        format_sel: str = "",            # optional — select/radio for format
        format_value: str = "PDF",       # value to choose (bank-specific)
        download_btn: str = "",
        date_format: str = "%d/%m/%Y",   # how the bank expects dates
        max_range_days: int = 180,       # bank-imposed max date range
    ):
        self.from_date_sel = from_date_sel
        self.to_date_sel = to_date_sel
        self.format_sel = format_sel
        self.format_value = format_value
        self.download_btn = download_btn
        self.date_format = date_format
        self.max_range_days = max_range_days

    def _fmt(self, date_str: str) -> str:
        """Convert YYYY-MM-DD → bank's expected format."""
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime(self.date_format)

    async def export(
        self,
        page: Page,
        date_range: dict,
        upload_dir: str,
        debug: DebugHelper = None,
    ) -> str:
        """
        Fill the date fields, select format, trigger download.
        Returns absolute path of the saved file.
        """
        from_str = self._fmt(date_range["start"])
        to_str = self._fmt(date_range["end"])

        logger.info(f"[StatementExporter] Setting range {from_str} → {to_str}")

        if self.from_date_sel:
            await page.locator(self.from_date_sel).triple_click()
            await page.keyboard.type(from_str)
            await human_delay(200, 500)

        if self.to_date_sel:
            await page.locator(self.to_date_sel).triple_click()
            await page.keyboard.type(to_str)
            await human_delay(200, 500)

        if self.format_sel and self.format_value:
            try:
                await page.locator(self.format_sel).select_option(self.format_value)
                await human_delay(200, 400)
            except Exception:
                logger.warning(f"[StatementExporter] Could not select format '{self.format_value}'")

        if debug:
            await debug.step(page, "before_download_click")

        os.makedirs(upload_dir, exist_ok=True)
        async with page.expect_download(timeout=60_000) as dl_info:
            await page.locator(self.download_btn).click()
        download = await dl_info.value

        suggested = download.suggested_filename or f"statement_{date_range['start']}.pdf"
        dest = os.path.join(upload_dir, suggested)
        await download.save_as(dest)

        # Detect password-protected PDF
        dest = await _mark_if_encrypted(dest)
        logger.info(f"[StatementExporter] Saved to {dest}")
        return os.path.abspath(dest)


async def _mark_if_encrypted(path: str) -> str:
    """
    Quick check if a PDF has an owner / user password.
    Renames to *_NEEDS_PASSWORD.pdf and returns the new path.
    Does NOT raise — the pipeline will flag it downstream.
    """
    if not path.lower().endswith(".pdf"):
        return path
    try:
        with open(path, "rb") as f:
            header = f.read(2048)
        # PyPDF / pdfplumber will fail later; we just detect the /Encrypt marker
        if b"/Encrypt" in header:
            new_path = path.replace(".pdf", "_NEEDS_PASSWORD.pdf")
            os.rename(path, new_path)
            logger.warning(f"[StatementExporter] Password-protected PDF detected → {new_path}")
            return new_path
    except Exception:
        pass
    return path


# ──────────────────────────────────────────────────────────────────────────────
# Bot-detection guard
# ──────────────────────────────────────────────────────────────────────────────
_BOT_INDICATORS = [
    "access denied",
    "robot",
    "captcha",
    "unusual traffic",
    "please verify",
    "too many requests",
    "automated",
    "scraping",
]


async def check_for_bot_block(page: Page, debug: DebugHelper = None) -> None:
    """
    Check page title/body for common anti-bot indicators.
    Raises AntiBot if detected so the runner can surface a clean error + screenshot.
    """
    from app.rpa.base_adapter import AntiBot
    try:
        content = (await page.title()).lower() + " " + (await page.inner_text("body"))[:500].lower()
    except Exception:
        return
    for phrase in _BOT_INDICATORS:
        if phrase in content:
            if debug:
                await debug.error_screenshot(page, "bot_block_detected")
            raise AntiBot(
                f"Bank portal blocked automation (bot-detection triggered: '{phrase}'). "
                "Try again later or switch to headful mode with a fresh browser profile."
            )
