"""
utils/image_store.py
~~~~~~~~~~~~~~~~~~~~
Quản lý lưu ảnh ra /tmp và serve qua static URL.

Thay thế pattern data_uri embed trong JSON:
    Cũ: content["data_uri"] = base64(blob)  → nặng, duplicate nhiều lần
    Mới: save_image(sha256, blob, mime)      → /tmp/docx_images/{sha256}.{ext}
         image_url(sha256, ext)              → "/images/{sha256}.{ext}"

Cleanup:
    Gọi cleanup_old_images() định kỳ (APScheduler 12h) hoặc thủ công.
    Không crash nếu file đã bị xóa bởi OS.

Thread-safe:
    save_image dùng write-then-rename để tránh partial file khi đọc đồng thời.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

# ── Config ────────────────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

TMP_DIR       = Path(os.environ.get("DOCX_IMAGE_TMP", str(_PROJECT_ROOT / "images")))
MAX_AGE_HOURS = int(os.environ.get("DOCX_IMAGE_MAX_AGE_HOURS", "12"))
URL_PREFIX    = "/images"

# Map MIME → extension
_MIME_TO_EXT: dict[str, str] = {
    "image/png":  "png",
    "image/jpeg": "jpg",
    "image/gif":  "gif",
    "image/webp": "webp",
    "image/bmp":  "bmp",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ext_for(mime: Optional[str]) -> str:
    """Trả extension từ mime, fallback về 'png'."""
    return _MIME_TO_EXT.get((mime or "").lower(), "png")


def _file_path(sha256: str, ext: str) -> Path:
    return TMP_DIR / f"{sha256}.{ext}"


# ── Public API ────────────────────────────────────────────────────────────────

def ensure_tmp_dir() -> None:
    """Tạo thư mục /tmp/docx_images nếu chưa có. Gọi một lần khi startup."""
    TMP_DIR.mkdir(parents=True, exist_ok=True)


def save_image(sha256: str, blob: bytes, mime: Optional[str] = None) -> str:
    """
    Lưu blob ra disk, trả về URL path để frontend dùng.
 
    FIX: tmp file dùng uuid4 thay PID — unique per-call, không bị
    collision khi cùng process gọi concurrent với cùng sha256.
 
    Dùng Path.replace() thay rename() — replace() hoạt động đúng
    trên cả Linux lẫn Windows (ghi đè dest nếu đã tồn tại, không raise).
    """
    import uuid
    ensure_tmp_dir()
    ext  = _ext_for(mime)
    dest = _file_path(sha256, ext)
 
    if dest.exists():
        dest.touch()
        return f"{URL_PREFIX}/{sha256}.{ext}"
 
    tmp_path = TMP_DIR / f"{sha256}.{ext}.{uuid.uuid4().hex}.tmp"
    try:
        tmp_path.write_bytes(blob)
        tmp_path.replace(dest)   # atomic trên Linux, ghi đè an toàn trên Windows
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
 
    return f"{URL_PREFIX}/{sha256}.{ext}"

def load_image(sha256: str) -> Optional[bytes]:
    """
    Đọc bytes ảnh đã lưu theo sha256.
 
    FIX: dùng get_image_path() đã có thay vì glob tự làm với _IMAGE_DIR sai.
    get_image_path() tự thử tất cả extension nếu không biết mime.
 
    Trả None nếu không tìm thấy hoặc đọc lỗi.
    """
    if not sha256:
        return None
 
    path = get_image_path(sha256)   # mime=None → thử tất cả ext
    if path is None:
        return None
 
    try:
        return path.read_bytes()
    except OSError:
        return None
 

def image_url(sha256: str, mime: Optional[str] = None) -> Optional[str]:
    """
    Trả URL nếu file đã tồn tại trên disk, None nếu chưa.

    Dùng để check trước khi gọi save_image khi chỉ có sha256.
    """
    ext  = _ext_for(mime)
    path = _file_path(sha256, ext)
    if path.exists():
        return f"{URL_PREFIX}/{sha256}.{ext}"
    return None


def get_image_path(sha256: str, mime: Optional[str] = None) -> Optional[Path]:
    """
    Trả Path object nếu file tồn tại — dùng trong route handler để serve.
    Thử tất cả extension nếu không biết mime.
    """
    if mime:
        p = _file_path(sha256, _ext_for(mime))
        return p if p.exists() else None

    # Thử tất cả ext đã biết
    for ext in _MIME_TO_EXT.values():
        p = _file_path(sha256, ext)
        if p.exists():
            return p
    return None


def cleanup_old_images(max_age_hours: int = MAX_AGE_HOURS) -> int:
    """
    Xóa file cũ hơn max_age_hours. Trả về số file đã xóa.
 
    Xóa luôn .tmp orphan (không có age limit) —
    file .tmp chỉ tồn tại trong thời gian atomic write,
    nếu còn sau cleanup cycle thì chắc chắn là orphan từ crash.
    """
    if not TMP_DIR.exists():
        return 0
 
    cutoff  = time.time() - max_age_hours * 3600
    deleted = 0
 
    for f in TMP_DIR.iterdir():
        if not f.is_file():
            continue
        try:
            if f.suffix == ".tmp":
                f.unlink()
                deleted += 1
                continue
 
            if f.stat().st_mtime < cutoff:
                f.unlink()
                deleted += 1
        except FileNotFoundError:
            pass
        except Exception:
            pass
 
    return deleted
 