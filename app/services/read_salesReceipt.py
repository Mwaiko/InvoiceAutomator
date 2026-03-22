from pathlib import Path


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}


def read_Grn(invoice_file) -> dict:
    """
    Accept:
      - a file path string / Path (PDF or image)
      - a dict that is already a parsed GRN (pass-through)
      - a dict that is raw PaddleOCR output (has 'rec_texts' key)

    Returns a structured GRN dict.
    """

    # ── Already a parsed GRN dict ─────────────────────────────────────────────
    if isinstance(invoice_file, dict):
        # Raw PaddleOCR result passed in directly
        if "rec_texts" in invoice_file:
            from read_image_content import _parse_tokens
            tokens = [t.strip() for t in invoice_file["rec_texts"] if t.strip()]
            full_text = " ".join(tokens)
            return _parse_tokens(tokens, full_text)
        # Already a structured GRN
        return invoice_file

    # ── File path ─────────────────────────────────────────────────────────────
    file_path = Path(invoice_file)

    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        from read_pdf import extract_grn
        print(f"📄 Extracting GRN from PDF: {file_path.name}")
        return extract_grn(str(file_path))

    if suffix in IMAGE_EXTENSIONS:
        from read_image_content import extract_grn_from_image
        print(f"🖼️  Extracting GRN from image (OCR): {file_path.name}")
        return extract_grn_from_image(str(file_path))

    if suffix == ".txt":
        raise NotImplementedError("Plain-text GRN parsing is not yet implemented.")

    raise ValueError(f"Unsupported file type: {suffix}")