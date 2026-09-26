import os
import fitz
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

def detect_file_format(file_content: bytes, filename: str) -> str:
    """
    Stage 1: Automated File Type & PDF Scanned/Digital Detector.
    Returns one of: 'digital_pdf', 'scanned_pdf', 'excel', 'csv', 'image', 'docx', 'unknown'
    """
    ext = filename.lower().split('.')[-1] if '.' in filename else ''

    if ext == 'pdf':
        try:
            doc = fitz.open(stream=file_content, filetype="pdf")
            total_text = sum(len(page.get_text()) for page in doc)
            if total_text < 50:
                return "scanned_pdf"
            return "digital_pdf"
        except Exception as e:
            logger.warning(f"PDF detection fallback to scanned_pdf for {filename}: {e}")
            return "scanned_pdf"

    elif ext in ['xlsx', 'xls']:
        return "excel"
    elif ext == 'csv':
        return "csv"
    elif ext in ['png', 'jpg', 'jpeg']:
        return "image"
    elif ext in ['docx', 'doc']:
        return "docx"

    return "unknown"

class FileRouter:
    """
    Stage 1: Modular File Router Dispatcher.
    """
    def route(self, file_content: bytes, filename: str) -> Dict[str, Any]:
        fmt = detect_file_format(file_content, filename)
        logger.info(f"FileRouter: '{filename}' detected as format '{fmt}'.")
        return {
            "filename": filename,
            "detected_format": fmt,
            "size_bytes": len(file_content)
        }
