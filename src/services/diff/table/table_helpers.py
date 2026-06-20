"""
Utility thuần cho table diff — dùng LogicalTable/LogicalCell.

Bỏ hoàn toàn: get_rows, get_cells, build_row_display_cells (DocNode-based).
Thay bằng helpers trên LogicalTable trực tiếp.

Import bởi: table_serialize.py, table_diff.py, table_analyze.py

THAY ĐỔI:
    - row_sig(): bỏ is_spanning — chỉ dùng anchor cells để tránh
      physical-row bias khi bảng có rowspan lớn.
    - row_content_hash(): bỏ spanning cells tương tự.
    - detect_structure_change(): nới lỏng từ total_cols != total_cols
      sang dùng resolve_header_map() làm primary signal; fallback
      abs(delta) > 1 khi không có header đủ tin cậy.
"""

from __future__ import annotations

import difflib
import hashlib
from typing import Any, Dict, List, Optional, Set, Tuple

from src.services.models.logical_table import LogicalCell, LogicalTable
from src.services.extractor.utils import norm_text as _norm


# ══════════════════════════════════════════════════════════════════════════════
# LogicalTable accessors
# ══════════════════════════════════════════════════════════════════════════════

def get_anchor_rows(tbl: LogicalTable) -> List[int]:
    """List anchor_row unique, sort tăng dần."""
    return tbl.anchor_rows()


def get_cells_in_row(tbl: LogicalTable, anchor_row: int) -> List[LogicalCell]:
    """Master cells trong 1 anchor row, sort theo anchor_col."""
    return tbl.cells_in_row(anchor_row)


def get_all_cells(tbl: LogicalTable) -> List[LogicalCell]:
    """Tất cả master cells, sort theo (anchor_row, anchor_col)."""
    return sorted(tbl.cells, key=lambda c: (c.anchor_row, c.anchor_col))


def cell_key(cell: LogicalCell) -> Tuple[int, int]:
    return (cell.anchor_row, cell.anchor_col)


# ══════════════════════════════════════════════════════════════════════════════
# Text extraction
# ══════════════════════════════════════════════════════════════════════════════

def cell_norm_text(cell: Optional[LogicalCell]) -> str:
    if not cell:
        return ""
    return _norm(cell.text)

def get_cell_nested_table(cell: Optional[LogicalCell]) -> Optional[LogicalTable]:
    if not cell:
        return None

    if cell.nested_table:
        return cell.nested_table

    for blk in getattr(cell, "content_blocks", []) or []:
        if getattr(blk, "type", None) == "table":
            tbl = blk.as_table()
            if tbl:
                return tbl

    return None


def cell_deep_text(cell: Optional[LogicalCell]) -> str:
    if not cell:
        return ""

    parts: List[str] = []

    txt = _norm(cell.text)
    if txt:
        parts.append(txt)

    nested = get_cell_nested_table(cell)
    if nested:
        for r in nested.anchor_rows():
            for c in nested.cells_in_row(r):
                deep = cell_deep_text(c)
                if deep:
                    parts.append(deep)

    return " ".join(parts)

def row_norm_text(tbl: LogicalTable, anchor_row: int) -> str:
    parts = [
        cell_deep_text(c)
        for c in get_cells_in_row(tbl, anchor_row)
        if cell_deep_text(c)
    ]
    return " | ".join(parts)

def _short_text_hash(text: str, n: int = 80) -> str:
    """
    Hash ngắn của normalized text prefix.
    Dùng cho row_sig để align mềm hơn full content hash,
    nhưng vẫn tránh false equal từ raw prefix text.
    """
    text = _norm(text)
    if not text:
        return ""

    return hashlib.md5(
        text[:n].encode("utf-8")
    ).hexdigest()[:10]
# ══════════════════════════════════════════════════════════════════════════════
# Row signature — dùng cho SequenceMatcher align rows
# ══════════════════════════════════════════════════════════════════════════════

def row_sig(tbl: LogicalTable, anchor_row: int) -> str:
    """
    Signature mềm cho SequenceMatcher align row.

    KHÔNG dùng full content_key() vì quá strict:
    chỉ cần text sửa nhẹ là row bị đẩy vào replace.

    KHÔNG dùng raw text prefix vì dễ false equal
    khi bảng có nhiều row giống đầu câu.

    Strategy:
    - anchor_col để giữ cấu trúc cột
    - nested/image markers để giữ semantic
    - short hash của normalized text prefix
    """
    parts: List[str] = []

    for c in get_cells_in_row(tbl, anchor_row):
        text = cell_deep_text(c)

        layout_kind = (
            "N" if get_cell_nested_table(c) else "T"
        )

        img_count = len(getattr(c, "images", []) or [])

        text_hash = _short_text_hash(text, 80)

        parts.append(
            f"{c.anchor_col}:"
            f"{layout_kind}:"
            f"img{img_count}:"
            f"txt{text_hash}"
        )

    parts.sort()
    return "|".join(parts)


def row_content_hash(tbl: LogicalTable, anchor_row: int) -> str:
    """
    Hash ổn định cho match_rows_by_content.

    Dùng text thật của row.
    KHÔNG dùng row position nếu row vẫn có content.
    Chỉ fallback position với row hoàn toàn rỗng.
    """
    all_parts: List[Tuple[int, str]] = []

    for c in get_cells_in_row(tbl, anchor_row):
        text = cell_deep_text(c)
        if text:
            all_parts.append((c.anchor_col, text))

    all_parts.sort()

    combined = " | ".join(
        text for _, text in all_parts
    )

    # chỉ row rỗng hoàn toàn mới fallback position
    if not combined:
        combined = f"__empty_row_{anchor_row}__"

    return hashlib.md5(
        combined.encode("utf-8")
    ).hexdigest()


# ══════════════════════════════════════════════════════════════════════════════
# Structure detection
# ══════════════════════════════════════════════════════════════════════════════

def detect_structure_change(a: LogicalTable, b: LogicalTable) -> bool:
    """
    Kiểm tra 2 LogicalTable có khác structure không.

    Strategy (ưu tiên từ trên xuống):

    1. Nếu cả 2 bảng có header đủ tin cậy → dùng resolve_header_map().
       Tính mapped_ratio = n_matched / max(n_a, n_b).
       Structure change khi mapped_ratio < 0.4 — tức là map được
       quá ít cột so với tổng (vd: 1/8 cột match vô tình không đủ).
       Lý do: header match semantic hơn total_cols — bảng có subheader
       hoặc merged cell dễ bị lệch total_cols dù vẫn map được.

    2. Fallback khi không có header → dùng abs(delta) > 1.
       Cho phép lệch 1 col (do gridSpan khác nhau giữa 2 file),
       nhưng lệch >= 2 thì coi là structure changed.

    KHÔNG dùng total_cols != total_cols (quá strict — classify
    nhầm full-table quá nhiều).
    """
    # Thử dùng header map trước
    header_result = resolve_header_map(a, b)
    if header_result["has_header"]:
        col_map   = header_result["col_map"]
        n_matched = len(col_map)
        n_a        = n_matched + len(header_result["deleted_cols"])
        n_b        = n_matched + len(header_result["added_cols"])
        denom      = max(n_a, n_b, 1)
        mapped_ratio = n_matched / denom
        # < 0.4 = map được quá ít cột → structure thực sự khác
        return mapped_ratio < 0.4

    # Fallback: không có header đủ tin cậy → dùng delta col count
    return abs(a.total_cols - b.total_cols) > 1


def _detect_header_rows(tbl: LogicalTable) -> List[int]:
    """
    Trả list anchor_rows được coi là header.
    Dừng khi gặp row đầu tiên có numeric content thật sự — tức là data row.
    Tối đa 3 rows.
    """
    import re
    anchor_rows = get_anchor_rows(tbl)
    header_rows: List[int] = []

    for r in anchor_rows[:3]:
        cells = get_cells_in_row(tbl, r)
        if not cells:
            break

        texts = [_norm(c.text) for c in cells if _norm(c.text)]
        if not texts:
            break

        # Có số thực sự (giá tiền, số lượng) → data row, dừng
        has_numeric = any(
            re.search(r"\d{2,}", t)
            and not re.fullmatch(r"\d{1,3}", t)  # không phải STT
            for t in texts
        )
        if has_numeric:
            break

        header_rows.append(r)

    # Fallback: luôn có ít nhất row 0
    if not header_rows and anchor_rows:
        header_rows = [anchor_rows[0]]

    return header_rows


# ══════════════════════════════════════════════════════════════════════════════
# Header resolution — dùng cho header-aware diff khi structure changed
# ══════════════════════════════════════════════════════════════════════════════

def resolve_header_map(
    a: LogicalTable,
    b: LogicalTable,
    fuzzy_threshold: float = 0.6,
) -> Dict[str, Any]:
    """
    Align header rows của 2 bảng → map anchor_col_A → anchor_col_B.
    Hỗ trợ multi-row header: combine text theo từng anchor_col.
    """
    a_rows = get_anchor_rows(a)
    b_rows = get_anchor_rows(b)

    if not a_rows or not b_rows:
        return {"col_map": {}, "added_cols": [], "deleted_cols": [], "has_header": False}

    a_header_rows = _detect_header_rows(a)
    b_header_rows = _detect_header_rows(b)

    def _build_col_text_map(tbl: LogicalTable, header_rows: List[int]) -> Dict[int, str]:
        """
        anchor_col → combined text từ tất cả header rows.
        Cell span nhiều col → text gán cho tất cả cols nó phủ.
        Multi-row → join bằng ' / ', dedup giữ thứ tự.
        """
        col_parts: Dict[int, List[str]] = {}
        for r in header_rows:
            for c in get_cells_in_row(tbl, r):
                txt = _norm(c.text)
                if not txt:
                    continue
                for k in range(c.col_span):
                    col = c.anchor_col + k
                    col_parts.setdefault(col, []).append(txt)

        return {
            col: " / ".join(dict.fromkeys(parts))
            for col, parts in col_parts.items()
        }

    a_headers = _build_col_text_map(a, a_header_rows)
    b_headers = _build_col_text_map(b, b_header_rows)

    if not a_headers or not b_headers:
        return {"col_map": {}, "added_cols": [], "deleted_cols": [], "has_header": False}

    matched_a: Set[int] = set()
    matched_b: Set[int] = set()
    candidates: List[Tuple[float, int, int]] = []

    for a_col, a_text in a_headers.items():
        for b_col, b_text in b_headers.items():
            if not a_text and not b_text:
                ratio = 1.0
            elif not a_text or not b_text:
                ratio = 0.0
            else:
                ratio = difflib.SequenceMatcher(
                    a=a_text, b=b_text, autojunk=False
                ).ratio()
                min_len = min(len(a_text), len(b_text))
                effective_threshold = 0.85 if min_len < 4 else fuzzy_threshold
                if ratio < effective_threshold:
                    continue
            candidates.append((-ratio, a_col, b_col))

    candidates.sort()
    col_map: Dict[int, int] = {}
    for _, a_col, b_col in candidates:
        if a_col in matched_a or b_col in matched_b:
            continue
        col_map[a_col] = b_col
        matched_a.add(a_col)
        matched_b.add(b_col)

    return {
        "col_map":      col_map,
        "added_cols":   sorted(b_col for b_col in b_headers if b_col not in matched_b),
        "deleted_cols": sorted(a_col for a_col in a_headers if a_col not in matched_a),
        "has_header":   True,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Row state matching — dùng cho analyze khi structure changed
# ══════════════════════════════════════════════════════════════════════════════

def match_rows_by_content(
    a: LogicalTable,
    b: LogicalTable,
) -> Dict[str, Any]:
    """
    Match anchor rows giữa 2 bảng theo content hash.

    Trả về:
    {
        "a_states": {anchor_row: "unchanged"|"deleted"},
        "b_states": {anchor_row: "unchanged"|"added"},
    }
    """
    a_rows = get_anchor_rows(a)
    b_rows = get_anchor_rows(b)

    a_hashes = [row_content_hash(a, r) for r in a_rows]
    b_hashes = [row_content_hash(b, r) for r in b_rows]

    sm = difflib.SequenceMatcher(a=a_hashes, b=b_hashes, autojunk=False)

    a_states: Dict[int, str] = {}
    b_states: Dict[int, str] = {}

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                a_states[a_rows[i1 + k]] = "unchanged"
                b_states[b_rows[j1 + k]] = "unchanged"
        elif tag == "delete":
            for i in range(i1, i2):
                a_states[a_rows[i]] = "deleted"
        elif tag == "insert":
            for j in range(j1, j2):
                b_states[b_rows[j]] = "added"
        elif tag == "replace":
            for i in range(i1, i2):
                a_states[a_rows[i]] = "deleted"
            for j in range(j1, j2):
                b_states[b_rows[j]] = "added"

    return {"a_states": a_states, "b_states": b_states}