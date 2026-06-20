"""
extractor/hash.py
~~~~~~~~~~~~~~~~~
Tiện ích hash cho ảnh — dùng trong image_diff.py và signature.py.

2 tầng so sánh:
    Tầng 1 — safe_sha256       : byte-exact, nhanh, không decode ảnh
    Tầng 2 — perceptual_hash   : bỏ qua resize/compression nhẹ

Workflow:
    images_changed(orig, mod)
        → Tầng 1 giống → False (không diff)
        → Tầng 1 khác  → Tầng 2
            → Tầng 2 giống (distance <= threshold) → False (không diff)
            → Tầng 2 khác                          → True  (có diff)
"""

from __future__ import annotations

import base64
import hashlib
from io import BytesIO
from typing import Optional

from PIL import Image

try:
    import imagehash as _imagehash
    _IMAGEHASH_AVAILABLE = True
except ImportError:
    _IMAGEHASH_AVAILABLE = False


# ══════════════════════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════════════════════

# pHash size=16 → 256 bit fingerprint
# Đủ để phân biệt ảnh khác nhau, đủ robust với resize nhỏ
_PHASH_SIZE = 16

# Threshold Hamming distance để coi 2 ảnh là "giống nhau"
# 3/256 bit ≈ 1.2% khác biệt — cover resize và minor JPEG compression
# Không cover crop hay thay đổi nội dung
_PHASH_THRESHOLD = 3


# ══════════════════════════════════════════════════════════════════════════════
# Tầng 1: Byte-exact hash
# ══════════════════════════════════════════════════════════════════════════════

def safe_sha256(data: bytes) -> str:
    """
    SHA256 của raw bytes.
    Nhanh, không decode ảnh, stable tuyệt đối.
    Trả "" nếu data rỗng hoặc None.
    """
    if not data:
        return ""
    return hashlib.sha256(data).hexdigest()


# ══════════════════════════════════════════════════════════════════════════════
# Tầng 2: Perceptual hash
# ══════════════════════════════════════════════════════════════════════════════

def perceptual_hash(img_bytes: bytes) -> str:
    """
    pHash của ảnh — stable với resize và minor compression.
    Nhạy với crop, thay đổi nội dung, thay ảnh khác.

    Trả "" nếu không hash được (file lỗi, imagehash không cài, v.v.).

    Yêu cầu: pip install imagehash
    """
    if not img_bytes:
        return ""

    if not _IMAGEHASH_AVAILABLE:
        # imagehash chưa cài → không thể làm tầng 2
        # caller sẽ xử lý trường hợp này (coi như đã thay đổi)
        return ""

    try:
        im = Image.open(BytesIO(img_bytes)).convert("L")  # grayscale
        h  = _imagehash.phash(im, hash_size=_PHASH_SIZE)
        return str(h)
    except Exception:
        return ""


def phash_distance(hash1: str, hash2: str) -> Optional[int]:
    """
    Hamming distance giữa 2 pHash string.
    Trả None nếu không tính được (hash rỗng hoặc imagehash không cài).
    """
    if not hash1 or not hash2 or not _IMAGEHASH_AVAILABLE:
        return None
    try:
        return _imagehash.hex_to_hash(hash1) - _imagehash.hex_to_hash(hash2)
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Main API: 2 tầng so sánh
# ══════════════════════════════════════════════════════════════════════════════

def images_changed(
    orig_bytes: bytes,
    mod_bytes:  bytes,
    phash_threshold: int = _PHASH_THRESHOLD,
) -> bool:
    """
    Kiểm tra 2 ảnh có thực sự thay đổi nội dung không.

    Trả False (không diff) khi:
        - Byte-exact giống nhau (tầng 1)
        - pHash distance <= threshold (tầng 2) — chỉ khác size/compression

    Trả True (có diff) khi:
        - Byte-exact khác VÀ pHash khác đủ nhiều
        - Không hash được tầng 2 (file lỗi) → conservative, coi là đã đổi

    Args:
        orig_bytes:      Raw bytes của ảnh gốc
        mod_bytes:       Raw bytes của ảnh đã sửa
        phash_threshold: Ngưỡng Hamming distance (default 3/256 bit)
    """
    # ── Tầng 1: byte-exact ───────────────────────────────────────────────────
    orig_sha = safe_sha256(orig_bytes)
    mod_sha  = safe_sha256(mod_bytes)

    if orig_sha and mod_sha and orig_sha == mod_sha:
        return False  # Giống hệt nhau → không diff

    # ── Tầng 2: perceptual hash ──────────────────────────────────────────────
    orig_ph = perceptual_hash(orig_bytes)
    mod_ph  = perceptual_hash(mod_bytes)

    if not orig_ph or not mod_ph:
        # Không hash được (imagehash chưa cài hoặc file lỗi)
        # → conservative: coi như đã thay đổi
        return True

    dist = phash_distance(orig_ph, mod_ph)

    if dist is None:
        return True  # Không tính được distance → conservative

    return dist > phash_threshold


# ══════════════════════════════════════════════════════════════════════════════
# Data URI helper
# ══════════════════════════════════════════════════════════════════════════════

def bytes_to_data_uri(img_bytes: bytes, mime: Optional[str] = None) -> str:
    """
    Encode ảnh thành data URI để nhúng vào HTML/JSON.
    Auto-detect MIME type nếu không truyền vào.
    """
    if mime is None:
        mime = _detect_mime(img_bytes)
    b64 = base64.b64encode(img_bytes).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def _detect_mime(data: bytes) -> str:
    """Detect MIME type từ magic bytes."""
    if not data:
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"GIF8":
        return "image/gif"
    if len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"  # fallback