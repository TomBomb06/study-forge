"""Text extraction: PDF (pypdf), image OCR (tesseract), plain text."""

from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader
from pypdf.errors import PdfReadError

MIN_USABLE_CHARS = 40


class ExtractionError(Exception):
    """User-facing extraction failure with a clear message."""


def extract_text(path: str, ext: str) -> str:
    if ext == ".pdf":
        text = _extract_pdf(path)
    elif ext in (".png", ".jpg", ".jpeg"):
        text = _extract_image(path)
    elif ext == ".txt":
        text = _extract_txt(path)
    else:
        raise ExtractionError(f"Unsupported file type: {ext}")

    text = text.strip()
    if len(text) < MIN_USABLE_CHARS:
        raise ExtractionError(
            "We couldn't find enough readable text in this file. "
            "If it's a scanned PDF, try uploading the pages as photos instead; "
            "if it's a photo, try better lighting or a straighter angle."
        )
    return text


def _extract_pdf(path: str) -> str:
    try:
        reader = PdfReader(path)
    except PdfReadError:
        raise ExtractionError("This PDF appears to be corrupt or unreadable.")
    if reader.is_encrypted:
        raise ExtractionError("This PDF is password-protected. Remove the password and re-upload.")
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    return "\n\n".join(pages)


def _extract_image(path: str) -> str:
    import pytesseract

    try:
        with Image.open(path) as img:
            img.load()
            if img.mode not in ("L", "RGB"):
                img = img.convert("RGB")
            return pytesseract.image_to_string(img)
    except UnidentifiedImageError:
        raise ExtractionError("This image file appears to be corrupt or unreadable.")
    except pytesseract.TesseractNotFoundError:
        raise ExtractionError(
            "OCR is not available on this server (tesseract not installed)."
        )


def _extract_txt(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()
