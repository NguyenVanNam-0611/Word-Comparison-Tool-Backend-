# api/routes/images.py
# FIX: Cache-Control max-age đọc từ MAX_AGE_HOURS của image_store
# thay vì hardcode 43200 — đảm bảo browser cache không dài hơn file tồn tại.

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from src.services.utils.image_store import get_image_path, TMP_DIR, MAX_AGE_HOURS

router = APIRouter(prefix="/images", tags=["images"])

_SAFE_FILENAME_RE = re.compile(
    r"^[a-f0-9]{64}\.(png|jpg|jpeg|gif|webp|bmp)$",
    re.IGNORECASE,
)

_EXT_TO_MIME: dict[str, str] = {
    "png":  "image/png",
    "jpg":  "image/jpeg",
    "jpeg": "image/jpeg",
    "gif":  "image/gif",
    "webp": "image/webp",
    "bmp":  "image/bmp",
}

# FIX: tính từ MAX_AGE_HOURS — không hardcode
_CACHE_HEADER = f"public, max-age={MAX_AGE_HOURS * 3600}, immutable"


@router.get("/{filename}")
async def serve_image(filename: str) -> FileResponse:
    if not _SAFE_FILENAME_RE.match(filename):
        raise HTTPException(status_code=400, detail="Invalid image filename")

    file_path = TMP_DIR / filename

    try:
        resolved = file_path.resolve()
        if not str(resolved).startswith(str(TMP_DIR.resolve())):
            raise HTTPException(status_code=400, detail="Invalid path")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")

    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(status_code=404, detail="Image not found")

    ext        = filename.rsplit(".", 1)[-1].lower()
    media_type = _EXT_TO_MIME.get(ext, "image/png")

    return FileResponse(
        path=resolved,
        media_type=media_type,
        headers={"Cache-Control": _CACHE_HEADER},
    )