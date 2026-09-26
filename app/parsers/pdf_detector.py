import re
import fitz  # PyMuPDF
import pdfplumber

def is_digital_pdf(file_path: str) -> bool:
    """
    Detects whether a PDF file is digital (contains searchable vector text) 
    or scanned (image-based).
    Returns True if digital, False if scanned.
    """
    total_text_length = 0
    total_pages = 0

    try:
        doc = fitz.open(file_path)
        total_pages = len(doc)
        
        for page in doc:
            text = page.get_text()
            if text:
                total_text_length += len(text.strip())
                
        doc.close()
    except Exception:
        # Fallback to pdfplumber if fitz encounters issues
        try:
            with pdfplumber.open(file_path) as pdf:
                total_pages = len(pdf.pages)
                for page in pdf.pages:
                    text = page.extract_text()
                    if text:
                        total_text_length += len(text.strip())
        except Exception:
            return False

    if total_pages == 0:
        return False

    # Calculate average text length per page
    avg_text_per_page = total_text_length / total_pages

    # If average text count per page is greater than threshold, it's a digital PDF
    return avg_text_per_page > 50
