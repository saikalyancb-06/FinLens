import logging
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

class BaseEmailService:
    """Abstract base class for email statement providers."""
    
    async def list_messages(self, query: str = "") -> List[Dict[str, Any]]:
        raise NotImplementedError("Subclasses must implement list_messages")

    async def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        raise NotImplementedError("Subclasses must implement download_attachment")

class OutlookService(BaseEmailService):
    """Microsoft Outlook / MS Graph API statement provider (Phase 2 extensible interface)."""
    
    def __init__(self, access_token: str):
        self.access_token = access_token

    async def list_messages(self, query: str = "") -> List[Dict[str, Any]]:
        logger.info("[Outlook Service] Outlook provider interface ready for MS Graph API integration.")
        return []

    async def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        raise NotImplementedError("Outlook attachment download will be available in Phase 2.")
