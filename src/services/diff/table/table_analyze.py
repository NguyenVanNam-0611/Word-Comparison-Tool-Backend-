# src/services/diff/table/table_analyze.py
"""
Phân tích kết quả diff_logical_table → chọn render mode và filter noise.

Render modes:
  table_deleted        : bảng A bị xóa hoàn toàn
  table_added          : bảng B mới hoàn toàn
  table_layout_changed : structure khác nhưng không có meaningful changes
  full_table           : structure changed + có meaningful changes (hiện cả 2)
  row_modified         : same structure + có thay đổi cụ thể ở row/cell level

THAY ĐỔI so với version cũ:
  - _is_layout_only() không còn chạy trước diff_logical_table().
    Trước: classify layout-only sớm → có thể nuốt meaningful changes.
    Giờ: layout-only chỉ được kết luận sau khi diff xong và not meaningful.
  - render_mode = full_table khi structure_changed + có meaningful changes.
    Trước: luôn dùng row_modified kể cả khi structure lệch.
    Giờ: full_table khi structure_changed, row_modified khi same structure.
  - _is_layout_only() nhận row_states pre-computed để tránh gọi
    match_rows_by_content() 2 lần trong cùng branch.
  - "layout_only" đổi thành "content_unverifiable" — semantic rõ hơn khi
    render_mode = full_table (không còn imply "chỉ là layout").
  - header_row fallback về a_tbl khi b_tbl không có header đủ tin cậy.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.services.models.logical_table import LogicalTable
from src.services.extractor.utils import norm_text as _norm

from src.services.diff.table.table_helpers import (
    detect_structure_change,
    match_rows_by_content,
    get_anchor_rows,
)
from src.services.diff.table.table_serialize import (
    serialize_logical_table,
    get_header_row,
)
from src.services.diff.table.table_diff import diff_logical_table


# ══════════════════════════════════════════════════════════════════════════════
# Render mode constants
# ══════════════════════════════════════════════════════════════════════════════

TABLE_RENDER_FULL           = "full_table"
TABLE_RENDER_DELETED        = "table_deleted"
TABLE_RENDER_ADDED          = "table_added"
TABLE_RENDER_ROW_MODIFIED   = "row_modified"
TABLE_RENDER_LAYOUT_CHANGED = "table_layout_changed"


# ══════════════════════════════════════════════════════════════════════════════
# Layout-only detector — chỉ dùng SAU KHI diff xong và not meaningful
# ══════════════════════════════════════════════════════════════════════════════

_LAYOUT_CHANGED_THRESHOLD = 0.8   # >= 80% rows unchanged → layout only

def _is_layout_only(
    a_tbl: LogicalTable,
    b_tbl: LogicalTable,
    row_states: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Trả True nếu 2 bảng có cùng nội dung nhưng khác layout.

    CHỈ gọi hàm này sau khi diff xong và meaningful changes rỗng.
    Không gọi trước diff để tránh nuốt meaningful changes.

    row_states: kết quả match_rows_by_content() đã tính trước — truyền vào
    để tránh gọi duplicate. Nếu None thì tự tính.

    Điều kiện:
    >= 80% anchor rows của cả 2 phía đều được match là "unchanged"
    theo match_rows_by_content (so sánh content hash, không so position).
    """
    a_rows = get_anchor_rows(a_tbl)
    b_rows = get_anchor_rows(b_tbl)
    total  = max(len(a_rows), len(b_rows))
    if total == 0:
        return False

    if row_states is None:
        row_states = match_rows_by_content(a_tbl, b_tbl)
    a_states   = row_states.get("a_states", {})
    b_states   = row_states.get("b_states", {})

    unchanged_a = sum(1 for s in a_states.values() if s == "unchanged")
    unchanged_b = sum(1 for s in b_states.values() if s == "unchanged")
    unchanged   = min(unchanged_a, unchanged_b)

    return (unchanged / total) >= _LAYOUT_CHANGED_THRESHOLD


# ══════════════════════════════════════════════════════════════════════════════
# Noise filters
# ══════════════════════════════════════════════════════════════════════════════

def _is_meta_event(change: Dict[str, Any]) -> bool:
    """Meta events (structure_changed) không phải meaningful change."""
    return change.get("change_kind") == "meta"


def _is_span_only_change(change: Dict[str, Any]) -> bool:
    """
    row_modified chỉ chứa span_changed mà text không đổi → noise.
    """
    if change.get("type") != "table_row_modified":
        return False
    cell_changes = change.get("cell_changes", [])
    if not cell_changes:
        return False
    return all(
        cc.get("type") == "table_cell_span_changed"
        and _norm(cc.get("left_text", "")) == _norm(cc.get("right_text", ""))
        for cc in cell_changes
    )


def _is_layout_only_change(change: Dict[str, Any]) -> bool:
    if change.get("type") != "table_row_modified":
        return False

    nested_types = {
        "nested_table_to_text",
        "text_to_nested_table",
        "nested_table_modified",
    }

    image_types = {
        "image_added",
        "image_deleted",
        "image_modified",
    }

    for cc in (change.get("cell_changes") or []):
        if cc.get("type") in nested_types:
            return False

        for sub in (cc.get("changes") or []):
            if sub.get("type") in nested_types:
                return False
            if sub.get("type") in image_types:
                return False

    left_text = _norm(" | ".join(
        c.get("left_text", "") or ""
        for c in (change.get("cell_changes") or [])
    ))
    right_text = _norm(" | ".join(
        c.get("right_text", "") or ""
        for c in (change.get("cell_changes") or [])
    ))

    return left_text == right_text


def _filter_noise(raw_changes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Áp dụng tất cả noise filters theo thứ tự.
    Giữ lại meta events (structure_changed) — analyze dùng riêng.
    """
    result = []
    seen: set = set()

    for c in raw_changes:
        if _is_meta_event(c):
            result.append(c)
            continue
        if _is_span_only_change(c):
            continue
        if _is_layout_only_change(c):
            continue
        # Dedup theo (type, anchor_row)
        ar  = c.get("anchor_row")
        ct  = c.get("type", "")
        key = (ct, ar) if ar is not None else id(c)
        if key not in seen:
            seen.add(key)
            result.append(c)

    return result


# ══════════════════════════════════════════════════════════════════════════════
# Main analyzer
# ══════════════════════════════════════════════════════════════════════════════

def analyze_table_change(
    a_tbl: Optional[LogicalTable],
    b_tbl: Optional[LogicalTable],
) -> Optional[Dict[str, Any]]:
    """
    Entry point cho json_builder.

    Trả None nếu 2 bảng giống nhau hoàn toàn.

    Flow:
        1. Handle bảng bị xóa / bảng mới hoàn toàn.
        2. Chạy diff_logical_table() — không classify sớm trước diff.
        3. Filter noise.
        4. Nếu not meaningful:
               structure_changed → TABLE_RENDER_LAYOUT_CHANGED
               else              → None (không thay đổi)
        5. Nếu có meaningful:
               structure_changed → TABLE_RENDER_FULL
               else              → TABLE_RENDER_ROW_MODIFIED
    """

    # ── Bảng bị xóa hoàn toàn ─────────────────────────────────────────────
    if a_tbl is not None and b_tbl is None:
        return {
            "render_mode":         TABLE_RENDER_DELETED,
            "structure_changed":   False,
            "full_table_original": serialize_logical_table(a_tbl),
            "full_table_modified": None,
            "header_row":          get_header_row(a_tbl),
            "added_rows":          [],
            "deleted_rows":        [],
            "table_changes":       [],
        }

    # ── Bảng mới hoàn toàn ────────────────────────────────────────────────
    if b_tbl is not None and a_tbl is None:
        return {
            "render_mode":         TABLE_RENDER_ADDED,
            "structure_changed":   False,
            "full_table_original": None,
            "full_table_modified": serialize_logical_table(b_tbl),
            "header_row":          get_header_row(b_tbl),
            "added_rows":          [],
            "deleted_rows":        [],
            "table_changes":       [],
        }

    assert a_tbl is not None and b_tbl is not None

    structure_changed = detect_structure_change(a_tbl, b_tbl)

    # ── Chạy diff trước — không classify sớm trước bước này ───────────────
    raw_changes = diff_logical_table(a_tbl, b_tbl)
    all_changes = _filter_noise(raw_changes)

    # Tách meta events ra khỏi meaningful changes
    meta_events = [c for c in all_changes if _is_meta_event(c)]
    meaningful  = [c for c in all_changes if not _is_meta_event(c)]

    # ── Không có meaningful changes ───────────────────────────────────────
    if not meaningful:
        if not structure_changed:
            # Hoàn toàn giống nhau
            return None

        # Structure khác nhưng không có meaningful changes.
        # Chạy layout-only check ở đây — đã an toàn vì diff xong rồi.
        # is_layout=True  → cols lệch nhưng content map 1-1 (reformat thuần)
        # is_layout=False → cols lệch, content không đủ tin cậy để kết luận layout-only
        row_states  = match_rows_by_content(a_tbl, b_tbl)
        is_layout   = _is_layout_only(a_tbl, b_tbl, row_states=row_states)
        render_mode = TABLE_RENDER_LAYOUT_CHANGED if is_layout else TABLE_RENDER_FULL
        return {
            "render_mode":           render_mode,
            "structure_changed":     True,
            "content_unverifiable":  not is_layout,
            "full_table_original": serialize_logical_table(a_tbl),
            "full_table_modified": serialize_logical_table(b_tbl),
            "header_row":          get_header_row(b_tbl) or get_header_row(a_tbl),
            "added_rows":          [],
            "deleted_rows":        [],
            "table_changes":       meta_events,
            "row_states":          row_states,
        }

    # ── Có meaningful changes ─────────────────────────────────────────────
    # structure_changed → full_table (hiện cả 2 phiên bản, frontend render diff cạnh nhau)
    # same structure   → row_modified (highlight row/cell cụ thể)
    render_mode  = TABLE_RENDER_FULL if structure_changed else TABLE_RENDER_ROW_MODIFIED
    added_rows   = [c for c in meaningful if c.get("type") == "table_row_added"]
    deleted_rows = [c for c in meaningful if c.get("type") == "table_row_deleted"]

    result: Dict[str, Any] = {
        "render_mode":         render_mode,
        "structure_changed":   structure_changed,
        "full_table_original": serialize_logical_table(a_tbl),
        "full_table_modified": serialize_logical_table(b_tbl),
        "header_row":          get_header_row(b_tbl),
        "added_rows":          added_rows,
        "deleted_rows":        deleted_rows,
        # meta_events đứng đầu để frontend parse trước
        "table_changes":       meta_events + meaningful,
    }

    if structure_changed:
        result["row_states"] = match_rows_by_content(a_tbl, b_tbl)

    return result