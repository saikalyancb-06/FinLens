"""
KredoAgent Ephemeral In-Memory Store & Uploader Client
------------------------------------------------------
1. Manages ephemeral memory for local credentials, OTPs, and CAPTCHAs (zero server transmission).
2. Posts downloaded statement PDF/CSV to existing server /files/upload endpoint.
"""
import os
import logging
import json
import urllib.request
import urllib.error
import uuid
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class EphemeralMemoryStore:
    """
    Local in-memory store for active job credentials.
    Zero disk persistence. Cleared immediately upon job completion or cancellation.
    """
    def __init__(self):
        self._store: Dict[str, Dict[str, Any]] = {}

    def set(self, job_id: str, data: Dict[str, Any]) -> None:
        self._store[job_id] = dict(data)

    def get(self, job_id: str) -> Dict[str, Any]:
        return self._store.get(job_id, {})

    def clear_job(self, job_id: str) -> None:
        if job_id in self._store:
            self._store[job_id].clear()
            del self._store[job_id]

    def clear_all(self) -> None:
        for k in list(self._store.keys()):
            self._store[k].clear()
        self._store.clear()


class ServerUploadClient:
    """
    Posts statement file downloaded by Playwright to existing server /files/upload endpoint.
    Uses urllib.request (zero extra dependencies).
    """
    def __init__(self, server_base_url: str, access_token: Optional[str] = None):
        self.server_base_url = server_base_url.rstrip("/")
        self.access_token = access_token

    def upload_statement(self, file_path: str, pdf_password: Optional[str] = None) -> Dict[str, Any]:
        url = f"{self.server_base_url}/files/upload"

        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Statement file not found at {file_path}")

        filename = os.path.basename(file_path)
        boundary = f"----WebKitFormBoundary{uuid.uuid4().hex}"

        body = bytearray()
        # File field
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8"))
        body.extend(b"Content-Type: application/octet-stream\r\n\r\n")
        with open(file_path, "rb") as f:
            body.extend(f.read())
        body.extend(b"\r\n")

        # PDF password field
        if pdf_password:
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(b'Content-Disposition: form-data; name="pdf_password"\r\n\r\n')
            body.extend(pdf_password.encode("utf-8"))
            body.extend(b"\r\n")

        body.extend(f"--{boundary}--\r\n".encode("utf-8"))

        req = urllib.request.Request(url, data=bytes(body), method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        if self.access_token:
            req.add_header("Authorization", f"Bearer {self.access_token}")

        try:
            logger.info(f"[Agent Uploader] Uploading {filename} to {url}...")
            with urllib.request.urlopen(req, timeout=60) as response:
                res_bytes = response.read()
                res_data = json.loads(res_bytes.decode("utf-8"))
                logger.info(f"[Agent Uploader] Upload successful. Response: {res_data}")
                return res_data
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8") if e.fp else str(e)
            raise RuntimeError(f"Server upload failed with status {e.code}: {err_body}")
        except Exception as e:
            raise RuntimeError(f"Server upload error: {e}")

