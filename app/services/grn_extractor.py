"""
app/services/grn_extractor.py

Wraps your existing read_pdf.py / read_image_content.py scripts.
Runs extraction in a thread pool so it doesn't block the async event loop.
"""

import asyncio
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}


def _extract_sync(file_path: str) -> dict:
    """
    Synchronous extraction — called in a thread pool.
    Mirrors the logic from your original read_salesReceipt.py.
    """
    path   = Path(file_path)
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        from app.services.read_pdf import extract_grn
        logger.info("Extracting GRN from PDF: %s", path.name)
        return extract_grn(str(path))

    if suffix in IMAGE_EXTENSIONS:
        from app.services.read_image_content import extract_grn_from_image
        logger.info("Extracting GRN from image via OCR: %s", path.name)
        return extract_grn_from_image(str(path))

    raise ValueError(f"Unsupported file type for extraction: {suffix}")


async def extract_grn(file_path: str) -> dict:
    """
    Async wrapper — runs the blocking extraction in a thread pool
    so the FastAPI event loop is never blocked.
    """
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, _extract_sync, file_path)
        logger.info("Extraction succeeded for: %s  items=%d", file_path, len(result.get("items", [])))
        return result
    except Exception as exc:
        logger.error("Extraction failed for %s: %s", file_path, exc)
        raise