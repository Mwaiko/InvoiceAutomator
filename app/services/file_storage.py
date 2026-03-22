"""
app/services/file_storage.py

Abstracts file I/O behind a simple interface.
Currently: local filesystem.
Later: swap to S3 by implementing the same interface.
"""

import os
import shutil
import uuid
from pathlib import Path

from fastapi import UploadFile

from app.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def detect_file_type(filename: str) -> str:
    """Returns 'pdf' or 'image'."""
    suffix = Path(filename).suffix.lower()
    return "pdf" if suffix == ".pdf" else "image"


def validate_extension(filename: str) -> None:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        from app.core.exceptions import ValidationError
        raise ValidationError(f"Unsupported file type: {suffix}. Allowed: {ALLOWED_EXTENSIONS}")


async def save_grn_upload(upload: UploadFile) -> tuple[str, str]:
    """
    Saves an uploaded GRN file to local storage.

    Returns:
        (storage_path, file_type)
    """
    validate_extension(upload.filename or "")

    folder = settings.grn_storage_path
    _ensure_dir(folder)

    # Unique filename to prevent collisions
    suffix   = Path(upload.filename or "file").suffix.lower()
    filename = f"{uuid.uuid4()}{suffix}"
    dest     = os.path.join(folder, filename)

    with open(dest, "wb") as f:
        shutil.copyfileobj(upload.file, f)

    file_type = detect_file_type(filename)
    logger.info("Saved GRN upload: %s (%s)", dest, file_type)
    return dest, file_type


def get_grn_file_path(storage_path: str) -> str:
    """Returns the absolute path for a stored GRN file."""
    return os.path.abspath(storage_path)


def delete_file(storage_path: str) -> None:
    """Soft-delete is preferred — only call this for cleanup of failed uploads."""
    try:
        os.remove(storage_path)
        logger.info("Deleted file: %s", storage_path)
    except FileNotFoundError:
        pass