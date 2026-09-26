import cv2
import numpy as np
import logging

logger = logging.getLogger(__name__)

def preprocess_document_image(image_bytes: bytes) -> np.ndarray:
    """
    Offline OpenCV Image Preprocessing Pipeline:
    1. Decode raw bytes
    2. Convert to Grayscale
    3. Automated Deskewing via minAreaRect
    4. Contrast Enhancement (CLAHE)
    """
    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

    if img is None:
        logger.error("Failed to decode image bytes with OpenCV.")
        return None

    # Step 1: Grayscale
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # Step 2: Automated Deskew
    try:
        coords = np.column_stack(np.where(gray < 250))
        if coords.size > 0:
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
        logger.warning(f"Deskew skipped: {e}")

    # Step 3: Contrast Enhancement (CLAHE)
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)

    return enhanced
