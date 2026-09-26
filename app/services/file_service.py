import os
import uuid
import hashlib
from typing import Tuple
from fastapi import UploadFile, HTTPException, status

from app.config import settings

ALLOWED_EXTENSIONS = {
    ".pdf",
    ".csv",
    ".xlsx",
    ".xls",
    ".zip"
}

# Read from settings so MAX_FILE_SIZE_BYTES in the environment actually takes
# effect. It was hardcoded here, which silently pinned the limit at 50 MB no
# matter what was configured — raising it in .env changed nothing and the
# rejection message still said 50MB.
MAX_FILE_SIZE_BYTES = settings.MAX_FILE_SIZE_BYTES
UPLOAD_DIR = os.path.abspath(settings.UPLOAD_DIR)

def validate_file(file: UploadFile) -> str:
    filename = file.filename or ""
    # Extract only the basename to prevent path traversal
    clean_filename = os.path.basename(filename)
    ext = os.path.splitext(clean_filename)[1].lower()
    
    # Prevent path traversal attacks by disallowing path separators or relative paths
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid filename: path traversal detected"
        )

    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file format '{ext}'. Allowed formats: PDF, Excel (.xlsx, .xls), CSV, ZIP"
        )
    return ext

def validate_file_content(ext: str, file_bytes: bytes):
    """Deep signature / content structure validation for financial statement formats."""
    if ext == ".pdf":
        if not file_bytes.startswith(b"%PDF-"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="INVALID_FILE_STRUCTURE: File header signature is not a valid PDF document."
            )
    elif ext in [".xlsx", ".zip"]:
        if not file_bytes.startswith(b"PK\x03\x04"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="INVALID_FILE_STRUCTURE: File header signature is not a valid XLSX/ZIP container."
            )
        if ext == ".xlsx":
            import zipfile, io
            try:
                with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
                    namelist = z.namelist()
                    if "[Content_Types].xml" not in namelist and "xl/workbook.xml" not in namelist:
                        raise ValueError("Missing XLSX workbook structure")
            except Exception:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="INVALID_FILE_STRUCTURE: File is not a valid XLSX spreadsheet package."
                )
    elif ext == ".csv":
        try:
            sample = file_bytes[:10240].decode("utf-8", errors="replace")
            # Ensure text format without binary NULL byte injections
            if "\x00" in sample:
                raise ValueError("Binary null bytes detected in CSV")
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="INVALID_FILE_STRUCTURE: File is not a valid CSV text document."
            )

def save_uploaded_file(file: UploadFile, user_id: str) -> Tuple[str, str, int, str]:
    ext = validate_file(file)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    
    file.file.seek(0)
    file_bytes = file.file.read()
    file_size = len(file_bytes)
    
    if file_size > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File size exceeds maximum threshold of {MAX_FILE_SIZE_BYTES // (1024 * 1024)}MB"
        )
        
    validate_file_content(ext, file_bytes)

    file_sha256 = hashlib.sha256(file_bytes).hexdigest()
    # Server-generated random filename preventing path traversal or execution risks
    unique_name = f"{user_id}_{uuid.uuid4().hex}{ext}"
    target_path = os.path.join(UPLOAD_DIR, unique_name)

    # Ensure path stays strictly inside UPLOAD_DIR
    real_target_path = os.path.abspath(target_path)
    if not real_target_path.startswith(UPLOAD_DIR):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path traversal attempt blocked"
        )

    with open(real_target_path, "wb") as f:
        f.write(file_bytes)
        
    return real_target_path, unique_name, file_size, file_sha256

