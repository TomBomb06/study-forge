"""Server-side upload validation + local file storage.

Local disk now; swap save_upload's write target for S3 later — the
validation logic stays identical.
"""

import os
import uuid

from fastapi import UploadFile

from .config import get_settings

# Magic-byte signatures — server-side type check, never trust the client.
_SIGNATURES: dict[str, list[bytes]] = {
    ".pdf": [b"%PDF"],
    ".png": [b"\x89PNG\r\n\x1a\n"],
    ".jpg": [b"\xff\xd8\xff"],
    ".jpeg": [b"\xff\xd8\xff"],
    ".txt": [],  # plain text has no signature; validated as UTF-8 below
    # Lecture audio/video (transcribed). Formats vary too much for a strict
    # magic-byte check, so these are validated by extension + size only.
    ".mp3": [],
    ".wav": [],
    ".m4a": [],
    ".mp4": [],
    ".mov": [],
    ".webm": [],
}

MEDIA_EXTENSIONS = {".mp3", ".wav", ".m4a", ".mp4", ".mov", ".webm"}
ALLOWED_EXTENSIONS = set(_SIGNATURES)
_CHUNK = 1024 * 1024


class UploadValidationError(Exception):
    """User-facing upload problem (bad type, too large, corrupt)."""


def _extension(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


def save_upload(upload: UploadFile, user_id: str) -> tuple[str, str]:
    """Validate and persist an upload. Returns (stored_path, extension)."""
    settings = get_settings()
    ext = _extension(upload.filename or "")
    if ext not in ALLOWED_EXTENSIONS:
        raise UploadValidationError(
            f"Unsupported file type '{ext or 'unknown'}'. "
            f"Supported: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )

    head = upload.file.read(16)
    signatures = _SIGNATURES[ext]
    if signatures and not any(head.startswith(sig) for sig in signatures):
        raise UploadValidationError(
            f"File does not look like a valid {ext} file."
        )
    if ext == ".txt":
        try:
            head.decode("utf-8")
        except UnicodeDecodeError:
            raise UploadValidationError("Text file is not valid UTF-8.")

    user_dir = os.path.join(settings.storage_dir, user_id)
    os.makedirs(user_dir, exist_ok=True)
    dest = os.path.join(user_dir, f"{uuid.uuid4().hex}{ext}")

    limit_mb = settings.max_media_mb if ext in MEDIA_EXTENSIONS else settings.max_upload_mb
    max_bytes = limit_mb * 1024 * 1024
    written = 0
    upload.file.seek(0)
    try:
        with open(dest, "wb") as f:
            while chunk := upload.file.read(_CHUNK):
                written += len(chunk)
                if written > max_bytes:
                    raise UploadValidationError(
                        f"File exceeds the {limit_mb} MB limit."
                    )
                f.write(chunk)
        if written == 0:
            raise UploadValidationError("Uploaded file is empty.")
    except UploadValidationError:
        if os.path.exists(dest):
            os.remove(dest)
        raise
    return dest, ext
