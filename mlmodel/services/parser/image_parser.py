import io
import logging
import cv2
import numpy as np
import pandas as pd
from PIL import Image

logger = logging.getLogger(__name__)

def preprocess_image(image_bytes: bytes) -> np.ndarray:
    """
    OpenCV Preprocessing Pipeline:
    1. Decode image
    2. Convert to Grayscale
    3. Deskew image using minAreaRect angle
    4. Contrast enhancement (CLAHE) & thresholding
    """
    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

    if img is None:
        logger.error("Failed to decode image bytes with OpenCV.")
        return None

    # Step 1: Grayscale
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # Step 2: Deskewing
    try:
        coords = np.column_stack(np.where(gray < 255))
        angle = cv2.minAreaRect(coords)[-1]
        if angle < -45:
            angle = -(90 + angle)
        else:
            angle = -angle
        
        if abs(angle) > 0.5 and abs(angle) < 45:
            (h, w) = gray.shape[:2]
            center = (w // 2, h // 2)
            M = cv2.getRotationMatrix2D(center, angle, 1.0)
            gray = cv2.warpAffine(gray, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    except Exception as e:
        logger.warning(f"Image deskew skipped: {e}")

    # Step 3: Contrast Enhancement (CLAHE)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)

    return enhanced

def parse_image_file(file_content: bytes, filename: str = "") -> pd.DataFrame:
    """
    Image Parser using OpenCV preprocessing + Tesseract OCR extraction.
    Returns transaction DataFrame.
    """
    logger.info(f"Starting Image OCR Parsing for {filename}")
    try:
        processed_img = preprocess_image(file_content)
        if processed_img is None:
            return pd.DataFrame()

        import pytesseract
        pil_img = Image.fromarray(processed_img)
        ocr_text = pytesseract.image_to_string(pil_img)

        from services.parser.pdf_parser import _parse_raw_text_lines
        return _parse_raw_text_lines(ocr_text)

    except Exception as e:
        logger.error(f"Image parsing error for {filename}: {e}")
        return pd.DataFrame()
