import fitz
import pdfplumber
import io
import logging
from typing import List, Dict, Any
from financial_parser.extraction.table_reconstruction import reconstruct_table_from_word_coords

logger = logging.getLogger(__name__)

def parse_digital_pdf(file_content: bytes, filename: str) -> List[Dict[str, Any]]:
    """
    Digital PDF Parser using PyMuPDF + pdfplumber word coordinate extraction.
    Never OCRs a digital PDF unless extraction fails completely.
    """
    words_list = []
    try:
        doc = fitz.open(stream=file_content, filetype="pdf")
        for page_idx, page in enumerate(doc):
            words = page.get_text("words")
            for w in words:
                words_list.append({
                    'page': page_idx + 1,
                    'x0': float(w[0]),
                    'y0': float(w[1]),
                    'x1': float(w[2]),
                    'y1': float(w[3]),
                    'text': str(w[4])
                })
    except Exception as e:
        logger.warning(f"PyMuPDF word extraction failed for {filename}: {e}")

    if not words_list:
        try:
            with pdfplumber.open(io.BytesIO(file_content)) as pdf:
                for page_idx, page in enumerate(pdf.pages):
                    words = page.extract_words()
                    for w in words:
                        words_list.append({
                            'page': page_idx + 1,
                            'x0': float(w['x0']),
                            'y0': float(w['top']),
                            'x1': float(w['x1']),
                            'y1': float(w['bottom']),
                            'text': str(w['text'])
                        })
        except Exception as e:
            logger.warning(f"pdfplumber word extraction failed for {filename}: {e}")

    return reconstruct_table_from_word_coords(words_list)
