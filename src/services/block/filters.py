"""
block/filters.py
~~~~~~~~~~~~~~~~
Các hàm lọc block/node không cần diff.

Tách từ block_builder.py.
Import bởi: block_builder.py
"""

from __future__ import annotations

import re
from typing import List

from src.services.models.docnode import DocNode
from src.services.models.block import Block


# ══════════════════════════════════════════════════════════════════════════════
# Helpers chuẩn hoá text
# ══════════════════════════════════════════════════════════════════════════════

def _norm(s: str) -> str:
    """Lowercase + collapse whitespace — dùng cho so sánh pattern nội bộ."""
    s = (s or "").lower().strip()
    return re.sub(r"\s+", " ", s)


# ══════════════════════════════════════════════════════════════════════════════
# Pattern skip
# ══════════════════════════════════════════════════════════════════════════════

_SKIP_SECTION_PATTERNS = [
    "muc luc", "mục lục", "table of contents", "目次",
    "ly lich sua doi", "lý lịch sửa đổi", "revision history",
    "change history", "document history", "改訂履歴",
]

_SKIP_BLOCK_PATTERNS = [
    "tiep trang sau", "tiếp trang sau",
    "(tiep trang sau)", "(tiếp trang sau)",
    "continued on next page", "continue on next page",
    "次ページに続く",
]

_TOC_LINE_RE = re.compile(r".+[\.\s·•]{3,}\d+\s*$")
_TOC_HEURISTIC_MIN_LINES = 4


def _is_skip_section_heading(node: DocNode) -> bool:
    text = _norm(node.content.get("text", ""))
    return any(pat in text for pat in _SKIP_SECTION_PATTERNS)


def _is_skip_pattern(text: str) -> bool:
    return any(pat in text for pat in _SKIP_BLOCK_PATTERNS)


def _is_skip_block(node: DocNode) -> bool:
    if node.content.get("in_toc"):
        return True
    if node.type in ("paragraph", "heading"):
        return _is_skip_pattern(_norm(node.content.get("text", "")))
    if node.type == "table":
        non_empty = []
        for n in node.walk():
            if n.type == "cell":
                t = _norm(n.content.get("text", ""))
                if t:
                    non_empty.append(t)
        if not non_empty:
            return False
        return all(_is_skip_pattern(t) for t in non_empty)
    return False


# ══════════════════════════════════════════════════════════════════════════════
# TOC heuristic fallback
# ══════════════════════════════════════════════════════════════════════════════

def _looks_like_toc_line(text: str) -> bool:
    return bool(_TOC_LINE_RE.match(text.strip()))


def remove_heuristic_toc(blocks: List[Block]) -> List[Block]:
    result: List[Block] = []
    i = 0
    while i < len(blocks):
        block = blocks[i]
        if (
            block.type == "paragraph"
            and not block.node.content.get("in_toc")
            and _looks_like_toc_line(_norm(block.node.content.get("text", "")))
        ):
            j = i
            while (
                j < len(blocks)
                and blocks[j].type == "paragraph"
                and not blocks[j].node.content.get("in_toc")
                and _looks_like_toc_line(_norm(blocks[j].node.content.get("text", "")))
            ):
                j += 1
            if (j - i) >= _TOC_HEURISTIC_MIN_LINES:
                i = j
                continue
            else:
                result.extend(blocks[i:j])
                i = j
                continue
        result.append(block)
        i += 1
    return result