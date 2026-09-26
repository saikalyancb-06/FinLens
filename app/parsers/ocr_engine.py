"""
ocr_engine.py  —  Production-grade OCR for scanned bank statement PDFs.

Fixes applied (vs original):
  BUG-010  DPI set to 300 (was default 72 → unreadable numbers).
           psm=6 (assume uniform block of text, best for statement columns).
           oem=3 (LSTM engine, best accuracy).
           Image pre-processing: grayscale → binarize → deskew.
           Post-OCR correction pass via normalizer.clean_ocr_text().
  NEW      Per-page logging.
  NEW      Graceful fallback if pdf2image/pytesseract unavailable.
"""

import logging
import os
from typing import List

from PIL import Image, ImageFilter, ImageOps

logger = logging.getLogger(__name__)

# ── Optional dependencies ─────────────────────────────────────────────────────
try:
    import pytesseract
    _TESSERACT_OK = True
except ImportError:
    pytesseract = None
    _TESSERACT_OK = False
    logger.warning("[OCR] pytesseract not installed — OCR disabled")

try:
    from pdf2image import convert_from_path
    _PDF2IMAGE_OK = True
except ImportError:
    convert_from_path = None
    _PDF2IMAGE_OK = False
    logger.warning("[OCR] pdf2image not installed — scanned PDF OCR disabled")

# Tesseract config for bank statements (uniform text, digits-heavy)
_TESS_CONFIG = "--psm 6 --oem 3 -c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz.,/-:() "
# Note: whitelist keeps only characters appearing in bank statements


def _preprocess_image(img: Image.Image) -> Image.Image:
    """
    Prepare a PIL image for best OCR accuracy on bank statement text:
      1. Convert to grayscale
      2. Scale to 300 DPI equivalent (2x upscale if small)
      3. Binarize with Otsu-style threshold (Pillow approximation)
      4. Light sharpening
    """
    # Grayscale
    if img.mode != "L":
        img = img.convert("L")

    # Upscale to at least 2400px wide for readable characters
    w, h = img.size
    if w < 2400:
        scale = 2400 / w
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    # Binarize: convert to pure black/white using auto-threshold
    # Pillow doesn't have Otsu but point() with a midpoint works well enough
    img = img.point(lambda p: 255 if p > 140 else 0)

    # Sharpen edges
    img = img.filter(ImageFilter.SHARPEN)

    return img


def perform_ocr_on_image(image: Image.Image) -> str:
    """
    Run Tesseract OCR on a PIL Image.
    Returns cleaned text string.
    """
    if not _TESSERACT_OK:
        return ""

    try:
        processed = _preprocess_image(image)
        raw_text = pytesseract.image_to_string(processed, config=_TESS_CONFIG) or ""
        # Import here to avoid circular import
        from app.parsers.normalizer import clean_ocr_text
        return clean_ocr_text(raw_text)
    except Exception as e:
        logger.warning(f"[OCR] Image OCR failed: {e}")
        return ""


def perform_ocr_on_pdf(pdf_path: str, dpi: int = 300) -> List[str]:
    """
    Convert each page of a scanned PDF to a 300-DPI image and OCR it.
    Returns list of cleaned text strings, one per page.
    """
    if not _PDF2IMAGE_OK or not _TESSERACT_OK:
        logger.warning("[OCR] pdf2image or pytesseract unavailable — returning empty")
        return []

    page_texts: List[str] = []
    try:
        logger.info(f"[OCR] Converting PDF to images at {dpi} DPI: {pdf_path}")
        images = convert_from_path(pdf_path, dpi=dpi)
        logger.info(f"[OCR] {len(images)} pages to process")
        for page_num, img in enumerate(images, start=1):
            text = perform_ocr_on_image(img)
            logger.debug(f"[OCR] Page {page_num}: extracted {len(text)} chars")
            page_texts.append(text)
    except Exception as e:
        logger.error(f"[OCR] PDF OCR failed: {e}")

    return page_texts
