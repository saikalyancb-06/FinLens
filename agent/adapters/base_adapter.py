"""
Local Agent Base Adapter Architecture
--------------------------------------
Contract signature for bank adapters executing inside KredoAgent.exe.
Contains generic ask() interaction support for local CAPTCHAs, OTPs, and PDF Passwords.
"""
import asyncio
import logging
from typing import Optional, Dict, Any
from playwright.async_api import Page

logger = logging.getLogger(__name__)


class AgentOTPRequired(Exception):
    pass


class AgentAntiBot(Exception):
    pass


class AgentBaseBankAdapter:
    bank_display_name: str = "BaseBank"
    implementation_status: str = "built_from_capture"  # "not_implemented" | "built_from_capture" | "live_verified"
    unattended_capable: bool = False

    def __init__(self, interaction_system=None, job_id: str = ""):
        self.interaction_system = interaction_system
        self.job_id = job_id

    async def ask(self, interaction_type: str, prompt_data: Dict[str, Any]) -> str:
        """
        Generic ask() interface for local user interaction prompts.
        Directs inputs to local KredoAgent GUI window.
        """
        if not self.interaction_system:
            raise RuntimeError("No interaction system bound to adapter")
        return await self.interaction_system.ask(interaction_type, prompt_data)

    async def human_delay(self, page: Page, min_ms: int = 300, max_ms: int = 900) -> None:
        import random
        delay = random.randint(min_ms, max_ms) / 1000.0
        await asyncio.sleep(delay)

    async def login(self, page: Page, credentials: dict) -> None:
        raise NotImplementedError

    async def navigate_to_statements(self, page: Page, params: dict) -> None:
        raise NotImplementedError

    async def download_statement(self, page: Page, date_range: dict) -> str:
        raise NotImplementedError
