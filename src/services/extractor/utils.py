"""
extractor/utils.py
~~~~~~~~~~~~~~~~~~
Helper thuần tuý dùng chung cho extractor/diff.

Nguyên tắc:
    - norm_text          : normalize cơ bản, không tự sửa punctuation quá tay
    - norm_for_signature : normalize ổn định để tạo signature
    - norm_for_diff      : normalize để tokenize word-level diff
    - norm_for_align     : normalize lỏng để align multiline/table text
    - raw_text           : giữ layout để render UI
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, List, Optional

OrderRef = Dict[str, int]


_SPACE_VARIANTS = (
    "\u00a0"  # non-breaking space
    "\u2002"  # en space
    "\u2003"  # em space
    "\u2004"
    "\u2005"
    "\u2006"
    "\u2007"
    "\u2008"
    "\u2009"
    "\u200a"
    "\u202f"
    "\u3000"
)

_DROP_CHARS = (
    "\u200b"  # zero-width space
    "\u200c"  # zero-width non-joiner
    "\u200d"  # zero-width joiner
    "\u2060"  # word joiner
    "\ufeff"  # BOM
    "\u00ad"  # soft hyphen
    # không drop \u2028 / \u2029 ở đây, xử lý riêng thành space/newline
    # không drop \x0b, xử lý riêng Shift+Enter
    "\x0c"  # form feed
    "\x1c"
    "\x1d"
    "\x1e"
)

_TO_SPACE_TABLE = str.maketrans(_SPACE_VARIANTS, " " * len(_SPACE_VARIANTS))
_DROP_TABLE = str.maketrans("", "", _DROP_CHARS)

_SHIFT_ENTER = "\x0b"


def _normalize_base(
    s: str,
    *,
    preserve_break: bool = False,
) -> str:
    """
    Normalize Unicode + xử lý ký tự ẩn.

    preserve_break=True:
        giữ xuống dòng tạm bằng "\\n" để caller quyết định xử lý.
        Với signature hiện tại, newline sẽ được flatten thành space.

    preserve_break=False:
        flatten whitespace bình thường.
    """

    if not s:
        return ""

    s = unicodedata.normalize("NFC", s)

    # ── Shift+Enter / line separator ──────────────────────────
    br = "\n" if preserve_break else " "

    s = s.replace(_SHIFT_ENTER, br)
    s = s.replace("\u2028", br)
    s = s.replace("\u2029", br)

    # ── remove internal chars ─────────────────────────────────
    s = s.translate(_DROP_TABLE)

    # ── normalize spaces ──────────────────────────────────────
    s = s.translate(_TO_SPACE_TABLE)

    # ── control whitespace ────────────────────────────────────
    if preserve_break:
        s = s.replace("\r\n", br).replace("\r", br).replace("\n", br).replace("\t", " ")
    else:
        s = s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ")

    return s


def norm_text(s: str) -> str:
    """
    Normalize cơ bản cho text.

    Dùng cho:
        - paragraph.content["text"]
        - heading.content["text"]
        - text search đơn giản

    Không xử lý punctuation quá tay.
    """
    s = _normalize_base(s)
    return " ".join(s.split()).strip()


def norm_for_signature(s: str) -> str:
    """
    Normalize ổn định để tạo signature.

    Fix:
    - Không sinh token __BR__ nữa.
    - Shift+Enter / line separator / newline được coi như space.
    - Tránh lỗi heading/context hiện chữ BR.
    """
    s = _normalize_base(s, preserve_break=True)

    if not s.replace("\n", "").strip():
        return ""

    # Không dùng "__BR__" vì dễ lộ ra heading/preview/context.
    # Với signature, xuống dòng chỉ cần coi như khoảng trắng.
    s = re.sub(r"\n+", " ", s)
    s = " ".join(s.split()).strip()

    s = re.sub(r"\s*([,;])\s*(?=\S)", r"\1 ", s)
    s = re.sub(r"\s+([,;])$", r"\1", s)

    s = re.sub(r"\(\s+", "(", s)
    s = re.sub(r"\s+\)", ")", s)

    return " ".join(s.split()).strip()


def norm_for_diff(s: str) -> str:
    """
    Normalize để tokenize trong word-level diff.

    Dấu phẩy/chấm phẩy thành token riêng.
    """
    s = norm_for_signature(s)

    s = re.sub(r"\s*([,;])\s*", r" \1 ", s)

    return " ".join(s.split()).strip()


def norm_for_align(s: str) -> str:
    """
    Normalize lỏng để align multiline/table text.

    Coi comma/semicolon gần giống separator dòng.
    Không dùng cho paragraph signature chính nếu muốn bắt punctuation change.
    """
    s = norm_for_signature(s)
    s = re.sub(r"[,;]\s*", " ", s)
    return " ".join(s.split()).strip()


def _detokenize(tokens: List[str]) -> str:
    """
    Join token list về string tự nhiên để render diff span.
    """
    result = ""

    for i, tok in enumerate(tokens):
        if tok in (",", ";"):
            result = result.rstrip() + tok + " "
        elif i == 0:
            result += tok
        else:
            result += " " + tok

    return result.strip()


def raw_text(s: str) -> str:
    if not s:
        return ""

    s = unicodedata.normalize("NFC", s)
    s = s.replace(_SHIFT_ENTER, "\n")
    s = s.replace("\u2028", "\n").replace("\u2029", "\n")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = s.translate(_DROP_TABLE)
    s = s.translate(_TO_SPACE_TABLE)

    if not s.replace("\n", "").strip():
        return ""

    return s.strip()


def next_order(order_ref: Optional[OrderRef]) -> int:
    if order_ref is None:
        return 0

    order_ref["value"] += 1
    return order_ref["value"]


def safe_pt(value) -> Optional[float]:
    try:
        return value.pt if value is not None else None
    except Exception:
        return None
