from typing import List, Dict, Any
from PIL import Image

from app.parsers.base import BaseParser
from app.parsers.ocr_engine import perform_ocr_on_image
from app.parsers.pdf_parser import PDFParser

class ImageParser(BaseParser):
    def parse(self, file_path: str) -> List[Dict[str, Any]]:
        try:
            image = Image.open(file_path)
            ocr_text = perform_ocr_on_image(image)
            
            if not ocr_text.strip():
                return []

            # Reuse PDFParser's text parsing pipeline logic
            pdf_parser = PDFParser()
            return pdf_parser._process_raw_text(ocr_text)
        except Exception as e:
            print(f"[ImageParser Error] Failed to parse image {file_path}: {e}")
            return []
