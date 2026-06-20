# src/services/diff/table/table_diff.py
"""
So sánh 2 LogicalTable theo cell-based approach.

4 loại bảng được xử lý:
  Loại 1: Same content, restructure layout (multi-col) → table_layout_changed
  Loại 2: Nested table ↔ plain text → flatten + word diff
  Loại 3: Nested table ↔ nested table, rowspan khác → đệ quy diff
  Loại 4: Plain text cell edit → word diff bình thường

Pipeline:
1. SequenceMatcher align anchor rows theo row_sig
2. equal rows   → vẫn _diff_row (có thể miss spanning cell change)
3. insert rows  → table_row_added
4. delete rows  → table_row_deleted
5. replace rows → _align_replace_rows → diff từng cặp row

Fixes so với cũ:
- equal rows không skip (bug #1)
- image diff restructure if/elif (bug #2)
- span_changed consistent (bug #3)
- _diff_paragraphs dùng sub-SequenceMatcher (bug #4)
- structure_changed meta event (bug #5)
- has_para_change dùng change_kind (bug #6)
- a_rows/b_rows dùng get_anchor_rows() thay vì range(total_rows) (bug #7)
"""

from __future__ import annotations

import difflib
from typing import Any, Dict, List, Optional, Tuple

from src.services.models.logical_table import (
    ImagePayload,
    LogicalCell,
    LogicalTable,
    ParagraphPayload,
)
from src.services.diff.paragraph_diff import diff_words
from src.services.extractor.utils import norm_text as _norm

from src.services.diff.table.table_helpers import (
    get_anchor_rows,
    row_sig,
    detect_structure_change,
    resolve_header_map,
    get_cell_nested_table, cell_deep_text,
)
from src.services.diff.table.table_serialize import (
    serialize_logical_cell,
    serialize_logical_table,
    serialize_image,
    serialize_paragraph,
)


# ══════════════════════════════════════════════════════════════════════════════
# Flatten helpers — dùng cho loại 2 (nested table ↔ plain text)
# ══════════════════════════════════════════════════════════════════════════════

def _flatten_table_to_text(tbl: LogicalTable) -> str:
    """
    Flatten toàn bộ nested table thành plain text.
    Join cells theo anchor row, rows cách nhau bằng newline.
    Dùng để word diff với plain text phía còn lại.
    """
    rows: List[str] = []
    for r in tbl.anchor_rows():
        cells = tbl.cells_in_row(r)
        row_parts = [_norm(c.text) for c in cells if _norm(c.text)]
        if row_parts:
            rows.append(" ".join(row_parts))
    return "\n".join(rows)


# ══════════════════════════════════════════════════════════════════════════════
# Image diff helpers
# ══════════════════════════════════════════════════════════════════════════════

def _build_image_change(
    a: Optional[ImagePayload],
    b: Optional[ImagePayload],
) -> Dict[str, Any]:
    if a is None:
        return {
            "type":        "image_added",
            "change_kind": "insert",
            "original":    None,
            "modified":    serialize_image(b) if b else None,
        }
    if b is None:
        return {
            "type":        "image_deleted",
            "change_kind": "delete",
            "original":    serialize_image(a),
            "modified":    None,
        }
    return {
        "type":        "image_modified",
        "change_kind": "replace",
        "original":    serialize_image(a),
        "modified":    serialize_image(b),
    }



# ══════════════════════════════════════════════════════════════════════════════
# Paragraph diff — fix #4: sub-SequenceMatcher trong replace slice
# ══════════════════════════════════════════════════════════════════════════════

def _diff_paragraphs(
    a_paras: List[ParagraphPayload],
    b_paras: List[ParagraphPayload],
) -> List[Dict[str, Any]]:
    """
    Diff list ParagraphPayload dùng SequenceMatcher 2 lớp.
    Lớp ngoài align các đoạn.
    Lớp trong (replace slice) sub-align lại để tránh pair sai.
    """
    changes: List[Dict[str, Any]] = []

    if not a_paras and not b_paras:
        return changes

    a_texts = [_norm(p.text) for p in a_paras]
    b_texts = [_norm(p.text) for p in b_paras]

    sm = difflib.SequenceMatcher(a=a_texts, b=b_texts, autojunk=False)

    for tag, i1, i2, j1, j2 in sm.get_opcodes():

        if tag == "equal":
            for k in range(i2 - i1):
                ap = a_paras[i1 + k]
                bp = b_paras[j1 + k]
                changes.append({
                    "type":        "paragraph_unchanged",
                    "change_kind": "equal",
                    "original":    serialize_paragraph(ap),
                    "modified":    serialize_paragraph(bp),
                })

        elif tag == "insert":
            for j in range(j1, j2):
                changes.append({
                    "type":        "paragraph_inserted",
                    "change_kind": "insert",
                    "original":    None,
                    "modified":    serialize_paragraph(b_paras[j]),
                })

        elif tag == "delete":
            for i in range(i1, i2):
                changes.append({
                    "type":        "paragraph_deleted",
                    "change_kind": "delete",
                    "original":    serialize_paragraph(a_paras[i]),
                    "modified":    None,
                })

        elif tag == "replace":
            # Fix #4: sub-SequenceMatcher để align đúng trong replace slice
            a_sub = a_paras[i1:i2]
            b_sub = b_paras[j1:j2]
            sub_sm = difflib.SequenceMatcher(
                a=[_norm(p.text) for p in a_sub],
                b=[_norm(p.text) for p in b_sub],
                autojunk=False,
            )
            for stag, si1, si2, sj1, sj2 in sub_sm.get_opcodes():
                if stag == "equal":
                    for k in range(si2 - si1):
                        ap = a_sub[si1 + k]
                        bp = b_sub[sj1 + k]
                        changes.append({
                            "type":        "paragraph_unchanged",
                            "change_kind": "equal",
                            "original":    serialize_paragraph(ap),
                            "modified":    serialize_paragraph(bp),
                        })
                elif stag == "insert":
                    for j in range(sj1, sj2):
                        changes.append({
                            "type":        "paragraph_inserted",
                            "change_kind": "insert",
                            "original":    None,
                            "modified":    serialize_paragraph(b_sub[j]),
                        })
                elif stag == "delete":
                    for i in range(si1, si2):
                        changes.append({
                            "type":        "paragraph_deleted",
                            "change_kind": "delete",
                            "original":    serialize_paragraph(a_sub[i]),
                            "modified":    None,
                        })
                elif stag == "replace":
                    pairs = min(si2 - si1, sj2 - sj1)
                    for k in range(pairs):
                        ap = a_sub[si1 + k]
                        bp = b_sub[sj1 + k]
                        old_text = _norm(ap.text)
                        new_text = _norm(bp.text)
                        if old_text != new_text:
                            word_diff = diff_words(
                                old_text, new_text,
                                old_display=ap.text_display,
                                new_display=bp.text_display,
                            ) or {}
                            changes.append({
                                "type":          "paragraph_modified",
                                "change_kind":   "replace",
                                "old_full_text": word_diff.get("old_full_text", old_text),
                                "new_full_text": word_diff.get("new_full_text", new_text),
                                "spans":         word_diff.get("spans", []),
                                "original":      serialize_paragraph(ap),
                                "modified":      serialize_paragraph(bp),
                            })
                        else:
                            changes.append({
                                "type":        "paragraph_unchanged",
                                "change_kind": "equal",
                                "original":    serialize_paragraph(ap),
                                "modified":    serialize_paragraph(bp),
                            })
                    # Phần dư sau pairs
                    for i in range(si1 + pairs, si2):
                        changes.append({
                            "type":        "paragraph_deleted",
                            "change_kind": "delete",
                            "original":    serialize_paragraph(a_sub[i]),
                            "modified":    None,
                        })
                    for j in range(sj1 + pairs, sj2):
                        changes.append({
                            "type":        "paragraph_inserted",
                            "change_kind": "insert",
                            "original":    None,
                            "modified":    serialize_paragraph(b_sub[j]),
                        })

    return changes


# ══════════════════════════════════════════════════════════════════════════════
# Cell diff — xử lý 4 loại
# ══════════════════════════════════════════════════════════════════════════════

def _diff_cell(a: LogicalCell, b: LogicalCell) -> List[Dict[str, Any]]:
    """
    Diff 2 LogicalCell.
    Trả list rỗng nếu không có gì thay đổi.

    Loại 2: nested ↔ text → flatten nested rồi word diff
    Loại 3: nested ↔ nested → đệ quy
    Loại 4: text ↔ text → paragraph diff + image diff riêng,
            interleave theo block position trong content_blocks.
    """
    changes: List[Dict[str, Any]] = []
    a_nested = get_cell_nested_table(a)
    b_nested = get_cell_nested_table(b)
    # ── Loại 3 ───────────────────────────────────────────────────────────────
    if a_nested and b_nested:
        nested_changes = diff_logical_table(a_nested, b_nested)
        if nested_changes:
            changes.append({
                "type":        "nested_table_modified",
                "change_kind": "replace",
                "original":    serialize_logical_table(a_nested),
                "modified":    serialize_logical_table(b_nested),
                "changes":     nested_changes,
            })
        return changes


    # ── Loại 2a ───────────────────────────────────────────────────────────────
    if a_nested and not b_nested:
        a_flat = _flatten_table_to_text(a_nested)
        b_text = _norm(b.text)
        word_diff = diff_words(a_flat, b_text) or {}
        changes.append({
            "type":           "nested_table_to_text",
            "change_kind":    "replace",
            "old_full_text":  word_diff.get("old_full_text", a_flat),
            "new_full_text":  word_diff.get("new_full_text", b_text),
            "spans":          word_diff.get("spans", []),
            "original_table": serialize_logical_table(a_nested),
            "modified_text":  b_text,
        })
        return changes

    # ── Loại 2b ───────────────────────────────────────────────────────────────
    if b_nested and not a_nested:
        a_text = _norm(a.text)
        b_flat = _flatten_table_to_text(b_nested)
        word_diff = diff_words(a_text, b_flat) or {}
        changes.append({
            "type":           "text_to_nested_table",
            "change_kind":    "replace",
            "old_full_text":  word_diff.get("old_full_text", a_text),
            "new_full_text":  word_diff.get("new_full_text", b_flat),
            "spans":          word_diff.get("spans", []),
            "original_text":  a_text,
            "modified_table": serialize_logical_table(b_nested),
        })
        return changes

    # ── Loại 4: Text ↔ text ───────────────────────────────────────────────────
    #
    # Tách riêng paragraphs và images để diff chính xác (p↔p, img↔img).
    # Sau đó interleave kết quả theo block position trong content_blocks
    # để giữ đúng thứ tự xuất hiện.
    #
    # Dùng content_blocks làm source of truth nếu có.
    # Fallback về a.paragraphs / a.images nếu content_blocks rỗng (data cũ).

    a_blocks = a.content_blocks or []
    b_blocks = b.content_blocks or []

    if a_blocks or b_blocks:
        # Build danh sách (block_index, payload) cho từng loại
        a_paras: List[Tuple[int, ParagraphPayload]] = [
            (i, blk.as_paragraph())
            for i, blk in enumerate(a_blocks)
            if blk.type == "paragraph" and blk.as_paragraph() is not None
        ]
        b_paras: List[Tuple[int, ParagraphPayload]] = [
            (i, blk.as_paragraph())
            for i, blk in enumerate(b_blocks)
            if blk.type == "paragraph" and blk.as_paragraph() is not None
        ]
        a_imgs_indexed: List[Tuple[int, ImagePayload]] = [
            (i, blk.as_image())
            for i, blk in enumerate(a_blocks)
            if blk.type == "image" and blk.as_image() is not None
        ]
        b_imgs_indexed: List[Tuple[int, ImagePayload]] = [
            (i, blk.as_image())
            for i, blk in enumerate(b_blocks)
            if blk.type == "image" and blk.as_image() is not None
        ]

        # Diff riêng từng loại
        para_changes = _diff_paragraphs(
            [p for _, p in a_paras],
            [p for _, p in b_paras],
        )
        img_changes = _diff_images_indexed(
            [img for _, img in a_imgs_indexed],
            [img for _, img in b_imgs_indexed],
        )

        has_para_change = any(c.get("change_kind") != "equal" for c in para_changes)
        has_img_change  = any(c.get("change_kind") != "equal" for c in img_changes)

        if not has_para_change and not has_img_change:
            return []

        # Interleave: gán sort_key cho mỗi change dựa trên block position
        # Para change thứ k → lấy block index từ a_paras[k] nếu có original,
        # hoặc b_paras[k] nếu insert.
        # Img change tương tự.

        tagged: List[Tuple[float, Dict[str, Any]]] = []

        # Track cursor trong a_paras / b_paras để map change → block index
        ai_cursor = 0
        bi_cursor = 0
        for ch in para_changes:
            kind = ch.get("change_kind")
            if kind == "equal":
                pos = a_paras[ai_cursor][0] if ai_cursor < len(a_paras) else float("inf")
                ai_cursor += 1
                bi_cursor += 1
            elif kind == "delete":
                pos = a_paras[ai_cursor][0] if ai_cursor < len(a_paras) else float("inf")
                ai_cursor += 1
            elif kind == "insert":
                # insert: dùng position của b, offset nhỏ sau block trước
                pos = (b_paras[bi_cursor][0] if bi_cursor < len(b_paras) else float("inf"))
                bi_cursor += 1
            else:  # replace
                pos = a_paras[ai_cursor][0] if ai_cursor < len(a_paras) else float("inf")
                ai_cursor += 1
                bi_cursor += 1
            tagged.append((float(pos), ch))

        ai_cursor = 0
        bi_cursor = 0
        for ch in img_changes:
            kind = ch.get("change_kind")
            if kind == "equal":
                pos = a_imgs_indexed[ai_cursor][0] if ai_cursor < len(a_imgs_indexed) else float("inf")
                ai_cursor += 1
                bi_cursor += 1
            elif kind == "delete":
                pos = a_imgs_indexed[ai_cursor][0] if ai_cursor < len(a_imgs_indexed) else float("inf")
                ai_cursor += 1
            elif kind == "insert":
                pos = (b_imgs_indexed[bi_cursor][0] if bi_cursor < len(b_imgs_indexed) else float("inf"))
                bi_cursor += 1
            else:  # replace
                pos = a_imgs_indexed[ai_cursor][0] if ai_cursor < len(a_imgs_indexed) else float("inf")
                ai_cursor += 1
                bi_cursor += 1
            tagged.append((float(pos), ch))

        # Sort theo block position → thứ tự đúng trong cell
        tagged.sort(key=lambda x: x[0])

        # Chỉ emit nếu có actual change (bỏ equal)
        for _, ch in tagged:
            if ch.get("change_kind") != "equal":
                changes.append(ch)

        return changes

    # ── Fallback: data cũ không có content_blocks ─────────────────────────────
    para_changes = _diff_paragraphs(a.paragraphs, b.paragraphs)
    has_para_change = any(c.get("change_kind") != "equal" for c in para_changes)
    if has_para_change:
        changes.extend(para_changes)

    a_imgs = a.images
    b_imgs = b.images
    if a_imgs or b_imgs:
        changes.extend(_diff_images_indexed(a_imgs, b_imgs))

    return changes


def _diff_images_indexed(
    a_imgs: List[ImagePayload],
    b_imgs: List[ImagePayload],
) -> List[Dict[str, Any]]:
    """Diff list ImagePayload, trả changes kể cả equal (để interleave)."""
    changes: List[Dict[str, Any]] = []
    if not a_imgs and not b_imgs:
        return changes

    a_sigs = [i.sha256 or i.uid or "" for i in a_imgs]
    b_sigs = [i.sha256 or i.uid or "" for i in b_imgs]

    sm = difflib.SequenceMatcher(a=a_sigs, b=b_sigs, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            # emit equal để interleave biết vị trí — filter ra sau
            for k in range(i2 - i1):
                changes.append({
                    **_build_image_change(a_imgs[i1 + k], b_imgs[j1 + k]),
                    "change_kind": "equal",
                    "type": "image_unchanged",
                })
        elif tag == "insert":
            for j in range(j1, j2):
                changes.append(_build_image_change(None, b_imgs[j]))
        elif tag == "delete":
            for i in range(i1, i2):
                changes.append(_build_image_change(a_imgs[i], None))
        elif tag == "replace":
            pairs = min(i2 - i1, j2 - j1)
            for k in range(pairs):
                changes.append(_build_image_change(a_imgs[i1 + k], b_imgs[j1 + k]))
            for i in range(i1 + pairs, i2):
                changes.append(_build_image_change(a_imgs[i], None))
            for j in range(j1 + pairs, j2):
                changes.append(_build_image_change(None, b_imgs[j]))

    return changes


# ══════════════════════════════════════════════════════════════════════════════
# Row builders
# ══════════════════════════════════════════════════════════════════════════════

def _make_row_added(tbl: LogicalTable, anchor_row: int) -> Dict[str, Any]:
    cells = tbl.logical_row_cells(anchor_row)
    return {
        "type":         "table_row_added",
        "change_kind":  "insert",
        "anchor_row":   anchor_row,
        "total_cols":   tbl.total_cols,
        "left_cells":   [],
        "right_cells":  [serialize_logical_cell(c) for c in cells],
        "left_text":    "",
        "right_text":   " | ".join(_norm(c.text) for c in cells if _norm(c.text)),
        "cell_changes": [],
    }


def _make_row_deleted(tbl: LogicalTable, anchor_row: int) -> Dict[str, Any]:
    cells = tbl.logical_row_cells(anchor_row)
    return {
        "type":         "table_row_deleted",
        "change_kind":  "delete",
        "anchor_row":   anchor_row,
        "total_cols":   tbl.total_cols,
        "left_cells":   [serialize_logical_cell(c) for c in cells],
        "right_cells":  [],
        "left_text":    " | ".join(_norm(c.text) for c in cells if _norm(c.text)),
        "right_text":   "",
        "cell_changes": [],
    }


# ══════════════════════════════════════════════════════════════════════════════
# Row diff
# ══════════════════════════════════════════════════════════════════════════════

def _diff_row(
    a_tbl: LogicalTable,
    b_tbl: LogicalTable,
    a_row: int,
    b_row: int,
    header_map: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Diff 2 anchor rows.
    Trả None nếu không có gì thay đổi.

    Fix #3: span_changed — chọn emit hoặc bỏ nhất quán.
    Ở đây: emit span_changed và set row_changed=True
    để frontend có thể filter nếu muốn.
    """
    a_cells_list = a_tbl.logical_row_cells(a_row)
    b_cells_list = b_tbl.logical_row_cells(b_row)

    a_map: Dict[int, LogicalCell] = {c.anchor_col: c for c in a_cells_list}
    b_map: Dict[int, LogicalCell] = {c.anchor_col: c for c in b_cells_list}

    # Xác định cặp (a_col, b_col)
    pairs: List[Tuple[Optional[int], Optional[int]]] = []

    if header_map and header_map.get("has_header") and header_map.get("col_map"):
        col_map      = header_map["col_map"]
        added_cols   = set(header_map.get("added_cols", []))
        deleted_cols = set(header_map.get("deleted_cols", []))
        for a_col, b_col in col_map.items():
            pairs.append((a_col, b_col))
        for a_col in sorted(deleted_cols):
            pairs.append((a_col, None))
        for b_col in sorted(added_cols):
            pairs.append((None, b_col))
    else:
        all_cols = sorted(set(a_map.keys()) | set(b_map.keys()))
        for col in all_cols:
            pairs.append((
                col if col in a_map else None,
                col if col in b_map else None,
            ))

    row_changed = False
    cell_changes: List[Dict[str, Any]] = []

    for a_col, b_col in pairs:
        ac = a_map.get(a_col) if a_col is not None else None
        bc = b_map.get(b_col) if b_col is not None else None
        display_col = b_col if b_col is not None else a_col

        if ac is None and bc is not None:
            row_changed = True
            cell_changes.append({
                "type":        "table_cell_added",
                "change_kind": "insert",
                "anchor_col":  display_col,
                "left_cell":   None,
                "right_cell":  serialize_logical_cell(bc),
                "left_text":   "",
                "right_text":  _norm(bc.text),
                "changes":     [],
            })
            continue

        if bc is None and ac is not None:
            row_changed = True
            cell_changes.append({
                "type":        "table_cell_deleted",
                "change_kind": "delete",
                "anchor_col":  display_col,
                "left_cell":   serialize_logical_cell(ac),
                "right_cell":  None,
                "left_text":   _norm(ac.text),
                "right_text":  "",
                "changes":     [],
            })
            continue

        if ac is None or bc is None:
            continue

        span_changed = (ac.row_span != bc.row_span or ac.col_span != bc.col_span)
        content_changes = _diff_cell(ac, bc)

        if span_changed and not content_changes:
            row_changed = True
            cell_changes.append({
                "type":           "table_cell_span_changed",
                "change_kind":    "replace",
                "anchor_col":     display_col,
                "left_cell":      serialize_logical_cell(ac),
                "right_cell":     serialize_logical_cell(bc),
                "left_row_span":  ac.row_span,
                "right_row_span": bc.row_span,
                "left_col_span":  ac.col_span,
                "right_col_span": bc.col_span,
                "left_text":      _norm(ac.text),
                "right_text":     _norm(bc.text),
                "changes":        [],
            })
        elif content_changes:
            row_changed = True
            cell_changes.append({
                "type":        "table_cell_modified",
                "change_kind": "replace",
                "anchor_col":  display_col,
                "left_cell":   serialize_logical_cell(ac),
                "right_cell":  serialize_logical_cell(bc),
                "left_text":   _norm(ac.text),
                "right_text":  _norm(bc.text),
                "changes":     content_changes,
            })

    if not row_changed:
        return None

    return {
        "type":         "table_row_modified",
        "change_kind":  "replace",
        "anchor_row":   b_row,
        "total_cols":   b_tbl.total_cols,
        "left_cells":   [serialize_logical_cell(c) for c in a_cells_list],
        "right_cells":  [serialize_logical_cell(c) for c in b_cells_list],
        "left_text":    " | ".join(_norm(c.text) for c in a_cells_list if _norm(c.text)),
        "right_text":   " | ".join(_norm(c.text) for c in b_cells_list if _norm(c.text)),
        "cell_changes": cell_changes,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Replace slice aligner
# ══════════════════════════════════════════════════════════════════════════════

_ROW_SIMILARITY_THRESHOLD = 0.55  # min score để coi 2 rows là cùng row


def _row_text(tbl: LogicalTable, row: int) -> str:
    cells = tbl.logical_row_cells(row)
    return " | ".join(
        cell_deep_text(c)
        for c in cells
        if cell_deep_text(c)
    )


def _row_similarity(
    a_tbl: LogicalTable, a_row: int,
    b_tbl: LogicalTable, b_row: int,
) -> float:
    a_text = _row_text(a_tbl, a_row)
    b_text = _row_text(b_tbl, b_row)
    if not a_text and not b_text:
        return 1.0
    if not a_text or not b_text:
        return 0.0
    return difflib.SequenceMatcher(None, a_text, b_text, autojunk=False).ratio()


def _align_replace_rows(
    a_tbl: LogicalTable,
    b_tbl: LogicalTable,
    a_slice: List[int],
    b_slice: List[int],
    header_map: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Sub-align replace slice bằng SequenceMatcher.

    Fix #1: equal rows không bị skip — vẫn gọi _diff_row
    vì spanning cell thay đổi có thể không làm thay đổi row_sig.
    """
    changes: List[Dict[str, Any]] = []

    a_sigs = [row_sig(a_tbl, r) for r in a_slice]
    b_sigs = [row_sig(b_tbl, r) for r in b_slice]

    sm = difflib.SequenceMatcher(a=a_sigs, b=b_sigs, autojunk=False)

    for tag, i1, i2, j1, j2 in sm.get_opcodes():

        if tag == "equal":
            for k in range(i2 - i1):
                result = _diff_row(
                    a_tbl, b_tbl,
                    a_slice[i1 + k],
                    b_slice[j1 + k],
                    header_map=header_map,
                )
                if result:
                    changes.append(result)

        elif tag == "insert":
            for j in range(j1, j2):
                changes.append(_make_row_added(b_tbl, b_slice[j]))

        elif tag == "delete":
            for i in range(i1, i2):
                changes.append(_make_row_deleted(a_tbl, a_slice[i]))

        elif tag == "replace":
            # Bước 1: greedy match — giữ nguyên logic cũ
            used_b: set[int] = set()
            matched: Dict[int, Optional[int]] = {}

            for ai in range(i1, i2):
                a_row = a_slice[ai]
                best_j: Optional[int] = None
                best_score = 0.0

                for bj in range(j1, j2):
                    if bj in used_b:
                        continue
                    score = _row_similarity(a_tbl, a_row, b_tbl, b_slice[bj])
                    if score > best_score:
                        best_score = score
                        best_j = bj

                if best_j is not None and best_score >= _ROW_SIMILARITY_THRESHOLD:
                    used_b.add(best_j)
                    matched[ai] = best_j
                else:
                    matched[ai] = None

            # Bước 2: emit theo thứ tự từ trên xuống, interleave added/deleted
            last_b_emitted = j1 - 1

            for ai in range(i1, i2):
                a_row = a_slice[ai]
                best_j = matched[ai]

                if best_j is not None:
                    # emit các b_row unmatched nằm TRƯỚC best_j (added)
                    for bj in range(last_b_emitted + 1, best_j):
                        if bj not in used_b:
                            changes.append(_make_row_added(b_tbl, b_slice[bj]))
                    last_b_emitted = best_j

                    result = _diff_row(
                        a_tbl, b_tbl,
                        a_row,
                        b_slice[best_j],
                        header_map=header_map,
                    )
                    if result:
                        changes.append(result)
                else:
                    changes.append(_make_row_deleted(a_tbl, a_row))

            # emit các b_row unmatched còn lại ở cuối
            for bj in range(last_b_emitted + 1, j2):
                if bj not in used_b:
                    changes.append(_make_row_added(b_tbl, b_slice[bj]))

    return changes


# ══════════════════════════════════════════════════════════════════════════════
# Main entry point
# ══════════════════════════════════════════════════════════════════════════════

def diff_logical_table(
    a: LogicalTable,
    b: LogicalTable,
) -> List[Dict[str, Any]]:
    """
    Diff 2 LogicalTable → list changes.

    Fix #5: emit table_structure_changed meta event khi cols khác nhau.
    Fix #7: dùng get_anchor_rows() thay vì range(total_rows) để chỉ
            diff các row thực sự có master cells, bỏ qua continuation
            rows không tồn tại trong cells list.

    Pipeline:
    1. Detect structure change
    2. SequenceMatcher align anchor rows theo row_sig
    3. equal   → _diff_row (không skip)
    4. insert  → row_added
    5. delete  → row_deleted
    6. replace → _align_replace_rows
    """
    changes: List[Dict[str, Any]] = []

    # Fix #7: dùng anchor rows, không phải range(total_rows)
    # range(total_rows) bao gồm continuation rows từ vMerge — không có
    # master cell, get_cells_in_row() trả rỗng → diff sai.
    a_rows = get_anchor_rows(a)
    b_rows = get_anchor_rows(b)

    if not a_rows and not b_rows:
        return changes

    structure_changed = detect_structure_change(a, b)
    header_map: Optional[Dict[str, Any]] = None

    if structure_changed:
        changes.append({
            "type":        "table_structure_changed",
            "change_kind": "meta",
            "a_cols":      a.total_cols,
            "b_cols":      b.total_cols,
            "a_rows":      a.total_rows,
            "b_rows":      b.total_rows,
        })
        header_map = resolve_header_map(a, b)
        if not header_map.get("has_header") or not header_map.get("col_map"):
            header_map = None

    a_sigs = [row_sig(a, r) for r in a_rows]
    b_sigs = [row_sig(b, r) for r in b_rows]

    sm = difflib.SequenceMatcher(a=a_sigs, b=b_sigs, autojunk=False)

    for tag, i1, i2, j1, j2 in sm.get_opcodes():

        if tag == "equal":
            for k in range(i2 - i1):
                result = _diff_row(
                    a, b,
                    a_rows[i1 + k],
                    b_rows[j1 + k],
                    header_map=header_map,
                )
                if result:
                    changes.append(result)

        elif tag == "insert":
            for j in range(j1, j2):
                changes.append(_make_row_added(b, b_rows[j]))

        elif tag == "delete":
            for i in range(i1, i2):
                changes.append(_make_row_deleted(a, a_rows[i]))

        elif tag == "replace":
            changes.extend(_align_replace_rows(
                a, b,
                a_rows[i1:i2],
                b_rows[j1:j2],
                header_map=header_map,
            ))

    return changes