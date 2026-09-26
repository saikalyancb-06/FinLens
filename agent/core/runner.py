"""
KredoAgent Core Local Job Runner & State Machine
------------------------------------------------
1. Long-polls server for queued metadata jobs (GET /rpa/agent/pending-jobs).
2. Collects credentials locally inside KredoAgent.exe (Zero server transmission).
3. Drives local Playwright Chromium instance through state transitions:
   QUEUED → LAUNCHING_BROWSER → LOGGING_IN → AWAITING_INPUT → NAVIGATING → DOWNLOADING → UPLOADING → PARSING → IMPORTING → SUCCESS.
4. Uploads downloaded statement file payload to server (POST /files/upload).
5. Clears memory store and ephemeral files on completion or failure.
"""
import os
import sys
import asyncio
import logging
from typing import Optional, Dict, Any

# When running as PyInstaller frozen EXE, direct Playwright to bundled Chromium browser folder
if getattr(sys, 'frozen', False):
    bundle_dir = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    bundled_browsers = os.path.join(bundle_dir, "ms-playwright")
    if os.path.exists(bundled_browsers):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = bundled_browsers

from playwright.async_api import async_playwright

from agent.core.interaction import AgentInteractionSystem
from agent.core.uploader import EphemeralMemoryStore, ServerUploadClient
from agent.adapters.mock_adapter import MockBankAdapter
from agent.adapters.base_adapter import AgentBaseBankAdapter

logger = logging.getLogger(__name__)

# Registry of local adapters inside KredoAgent.exe
AGENT_ADAPTERS: Dict[str, type] = {
    "mock_bank": MockBankAdapter,
}

try:
    from app.rpa.sbi_adapter import SBIAdapter
    AGENT_ADAPTERS["sbi"] = SBIAdapter
except Exception:
    pass


class LocalJobRunner:
    """
    Main job loop and state machine driver running inside KredoAgent.exe.
    """
    def __init__(self, server_url: str, access_token: Optional[str] = None, headless: bool = True):
        self.server_url = server_url.rstrip("/")
        self.access_token = access_token
        self.headless = headless
        self.memory_store = EphemeralMemoryStore()
        self.interaction_system = AgentInteractionSystem(headless_cli_mode=headless)
        self.uploader = ServerUploadClient(server_url, access_token)

    def _update_server_status(self, job_id: str, status: str, error_message: Optional[str] = None, statement_id: Optional[str] = None):
        import json
        import urllib.request
        url = f"{self.server_url}/rpa/agent/update-status"
        payload = {
            "job_id": job_id,
            "status": status,
            "error_message": error_message,
            "statement_id": statement_id
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        if self.access_token:
            req.add_header("Authorization", f"Bearer {self.access_token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                pass
        except Exception as e:
            logger.error(f"[LocalJobRunner] Status update failed: {e}")

    async def execute_job(self, job_metadata: Dict[str, Any], local_credentials: Dict[str, Any]) -> str:
        job_id = job_metadata["job_id"]
        bank_name = job_metadata.get("bank_name", "mock_bank").lower()
        start_date = job_metadata.get("start_date", "2026-08-01")
        end_date = job_metadata.get("end_date", "2026-08-31")

        logger.info(f"[LocalJobRunner] Starting job {job_id} for bank={bank_name}")
        self.memory_store.set(job_id, local_credentials)

        adapter_cls = AGENT_ADAPTERS.get(bank_name, MockBankAdapter)
        adapter: AgentBaseBankAdapter = adapter_cls(
            interaction_system=self.interaction_system,
            job_id=job_id
        )

        downloaded_file_path: Optional[str] = None
        playwright_obj = None
        browser = None

        try:
            # 1. LAUNCHING_BROWSER
            self._update_server_status(job_id, "launching_browser")
            playwright_obj = await async_playwright().start()
            browser = await playwright_obj.chromium.launch(headless=self.headless)
            context = await browser.new_context(accept_downloads=True)
            page = await context.new_page()

            # 2. LOGGING_IN
            self._update_server_status(job_id, "logging_in")
            await adapter.login(page, local_credentials)

            # 3. NAVIGATING
            self._update_server_status(job_id, "navigating")
            await adapter.navigate_to_statements(page, {})

            # 4. DOWNLOADING
            self._update_server_status(job_id, "downloading")
            downloaded_file_path = await adapter.download_statement(page, {"start": start_date, "end": end_date})

            # Close browser context
            await context.close()
            await browser.close()
            await playwright_obj.stop()
            browser = None
            playwright_obj = None

            # 5. UPLOADING
            self._update_server_status(job_id, "uploading")
            upload_res = self.uploader.upload_statement(downloaded_file_path)

            statement_id = upload_res.get("statement_id") or upload_res.get("file_id")

            # 6. PARSING & IMPORTING (Triggered server side by /files/upload)
            self._update_server_status(job_id, "parsing")
            await asyncio.sleep(0.5)
            self._update_server_status(job_id, "importing")
            await asyncio.sleep(0.5)

            # 7. SUCCESS
            self._update_server_status(job_id, "success", statement_id=str(statement_id) if statement_id else None)
            logger.info(f"[LocalJobRunner] Job {job_id} finished successfully! Statement ID: {statement_id}")
            return str(statement_id)

        except Exception as e:
            logger.error(f"[LocalJobRunner] Job {job_id} failed: {e}", exc_info=True)
            self._update_server_status(job_id, "failed", error_message=str(e))
            raise e

        finally:
            # Emergency Cleanup & Ephemeral Memory Wipe
            if browser:
                await browser.close()
            if playwright_obj:
                await playwright_obj.stop()
            if downloaded_file_path and os.path.exists(downloaded_file_path):
                try:
                    os.remove(downloaded_file_path)
                except Exception:
                    pass
            self.memory_store.clear_job(job_id)
