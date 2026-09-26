import logging
from typing import List, Dict, Any, Tuple
import fitz

from financial_parser.parsers.pdf_parser import parse_digital_pdf
from financial_parser.parsers.excel_parser import parse_excel_document
from financial_parser.parsers.csv_parser import parse_csv_document

logger = logging.getLogger(__name__)

def detect_document_type(file_content: bytes, filename: str) -> str:
    ext = filename.lower().split('.')[-1] if '.' in filename else ''
    
    if ext == 'pdf':
        try:
            doc = fitz.open(stream=file_content, filetype="pdf")
            total_text = sum(len(p.get_text()) for p in doc)
            if total_text < 50:
                return "scanned_pdf"
            return "digital_pdf"
        except Exception:
            return "scanned_pdf"
            
    elif ext in ['xlsx', 'xls']:
        return "excel"
    elif ext == 'csv':
        return "csv"
    elif ext in ['png', 'jpg', 'jpeg']:
        return "image"
    elif ext in ['docx', 'doc']:
        return "docx"
    
    return "csv"

def route_and_extract(file_content: bytes, filename: str) -> Tuple[List[Dict[str, Any]], float]:
    doc_type = detect_document_type(file_content, filename)
    logger.info(f"Router detected document type '{doc_type}' for {filename}")

    if doc_type in ["digital_pdf", "scanned_pdf"]:
        return parse_digital_pdf(file_content, filename), 0.95
    elif doc_type == "excel":
        return parse_excel_document(file_content, filename)
    elif doc_type == "csv":
        return parse_csv_document(file_content, filename), 0.90
    else:
        return parse_csv_document(file_content, filename), 0.80
