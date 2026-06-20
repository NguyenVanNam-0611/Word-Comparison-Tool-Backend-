"""
serializer/json_builder.py

Build UI JSON từ aligned block opcodes.

Schema nhất quán cho mọi change object:
──────────────────────────────────────────────────────────────────────────────
{
  id, heading, type, change_kind, order, seq_index,
  left:  _serialize_block_side(a)
  right: _serialize_block_side(b)

  # Per-type extra fields:
  word_diff        : paragraph_modified / heading_modified
  old_level        : heading_modified
  new_level        : heading_modified
  level_changed    : heading_modified
  table_analysis   : table_modified / table_inserted / table_deleted
  shape_changes    : shape_modified
  shape_cleared    : shape_modified
  shape_text_preview: shape_modified / shape_inserted / shape_deleted
  image_change     : image_modified / image_inserted / image_deleted
  left_img         : image_modified / image_deleted
  right_img        : image_modified / image_inserted
  left_context     : { previous, next }
  right_context    : { previous, next }
}

Fixes:
1. _process_insert/_process_delete: guard b_logical/a_logical is None
   → analyze_table_change(None, None) không bao giờ được gọi
2. mixed type replace: seq dùng offset lớn hơn tránh collision
3. _collect_shape_text_lines: đọc logical_table cho table trong shape
4. cid=0 placeholder đã được overwrite — giữ nguyên, thêm comment
5. table_layout_changed pass-through đúng (analyze trả đủ fields)
6. shape_cleared: dùng a_has_content thay vì a_has_text
   → shape chỉ có image (không text) bị xóa vẫn được đánh dấu cleared đúng
7. replace multi-block pair theo vị trí: thêm assert 1-1 + fallback rõ
   → không pair sai thầm lặng khi aligner emit replace slice dài
8. _shape_has_content: dùng _collect_shape_text_lines + _collect_shape_images
   → tránh coi shape có children placeholder rỗng là có content
9. _process_table: fallback rõ khi logical_table thiếu một bên
   → không crash, frontend vẫn thấy table changed
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from src.services.models.block import Block
from src.services.diff.paragraph_diff import diff_words
from src.services.diff.table.table_analyze import analyze_table_change
from src.services.diff.shape_diff import diff_shape
from src.services.diff.image_diff import images_equal, build_image_change
from src.services.extractor.utils import norm_text as _norm

_SEQ_MULT = 100_000


def _seq_for_insert(anchor_i: int, offset: int = 0) -> int:
    """
    Insert được neo vào block A tại anchor_i thì cho đứng trước block đó.
    Ví dụ:
        A B C
        A X B C

    insert X có anchor_i = index(B)
    seq = B_seq - 50_000
    → X đứng trước B.
    """
    return anchor_i * _SEQ_MULT - (_SEQ_MULT // 2) + offset


# ══════════════════════════════════════════════════════════════════════════════
# Text helpers
# ══════════════════════════════════════════════════════════════════════════════

def _display_text(content: dict) -> str:
    return (content.get("text_display") or content.get("text") or "").strip()

# ══════════════════════════════════════════════════════════════════════════════
# Node serializer
# ══════════════════════════════════════════════════════════════════════════════

def _serialize_node(node) -> Optional[Dict[str, Any]]:
    if not node:
        return None

    content = getattr(node, "content", {}) or {}
    safe_content = {
        k: v for k, v in content.items()
        if k not in ("raw_bytes", "data_uri")
    }

    out: Dict[str, Any] = {
        "uid":      getattr(node, "uid", None),
        "type":     getattr(node, "type", None),
        "content":  safe_content,
        "children": [_serialize_node(c) for c in (getattr(node, "children", []) or [])],
    }

    if out["type"] == "image":
        image_url = content.get("image_url") or content.get("data_uri")
        out["image_url"] = image_url
        out["image"] = {
            "image_url":  image_url,
            "width_px":   round((content.get("width_emu", 0) or 0) * 96 / 914400) or None,
            "height_px":  round((content.get("height_emu", 0) or 0) * 96 / 914400) or None,
            "mime":       content.get("mime"),
            "sha256":     content.get("sha256"),
            "floating":   content.get("floating", False),
        }

    return out


# ══════════════════════════════════════════════════════════════════════════════
# Block serializers
# ══════════════════════════════════════════════════════════════════════════════

def _serialize_block_side(block: Optional[Block]) -> Optional[Dict[str, Any]]:
    if not block:
        return None

    content = getattr(block.node, "content", {}) or {}

    page = (
        content.get("page")
        or getattr(block, "page", None)
        or 1
    )

    page_start = (
        content.get("page_start")
        or page
        or 1
    )

    page_end = (
        content.get("page_end")
        or page_start
        or page
        or 1
    )

    try:
        page = int(page)
        page_start = int(page_start)
        page_end = int(page_end)
    except Exception:
        page = 1
        page_start = 1
        page_end = 1

    if page_end < page_start:
        page_end = page_start

    return {
        "uid":          block.uid,
        "type":         block.type,
        "order":        block.order,

        # Page fields cho frontend tự render
        "page":         page,
        "page_start":   page_start,
        "page_end":     page_end,

        "heading":      block.heading_ctx,
        "preview_text": block.preview_text,
        "node":         _serialize_node(block.node),
    }


def _ctx_item(block: Optional[Block]) -> Optional[Dict[str, Any]]:
    if not block:
        return None
    return {
        "uid":          block.uid,
        "type":         block.type,
        "order":        block.order,
        "heading":      block.heading_ctx,
        "preview_text": block.preview_text,
    }


def _build_context(blocks: List[Block], index: int) -> Dict[str, Any]:
    return {
        "previous": _ctx_item(blocks[index - 1] if index > 0               else None),
        "next":     _ctx_item(blocks[index + 1] if index + 1 < len(blocks) else None),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Shape serializer
# Fix #3: đọc logical_table cho table node trong shape
# ══════════════════════════════════════════════════════════════════════════════

def _collect_shape_text_lines(node) -> List[str]:
    lines: List[str] = []
    for child in (getattr(node, "children", []) or []):
        t = getattr(child, "type", None)

        if t == "paragraph":
            content = getattr(child, "content", {}) or {}
            txt = _display_text(content)
            if txt:
                lines.append(txt)

        elif t == "shape":
            lines.extend(_collect_shape_text_lines(child))

        elif t == "table":
            lt = getattr(child, "logical_table", None)
            if lt is not None:
                for r in lt.anchor_rows():
                    cells = lt.cells_in_row(r)
                    cell_texts = [
                        c.text_display or c.text
                        for c in cells
                        if (c.text_display or c.text)
                    ]
                    if cell_texts:
                        lines.append(" | ".join(cell_texts))
            else:
                for row in (getattr(child, "children", []) or []):
                    if getattr(row, "type", None) != "row":
                        continue
                    cell_texts = []
                    for cell in (getattr(row, "children", []) or []):
                        if getattr(cell, "type", None) != "cell":
                            continue
                        content = getattr(cell, "content", {}) or {}
                        ct = _display_text(content)
                        if ct:
                            cell_texts.append(ct)
                    if cell_texts:
                        lines.append(" | ".join(cell_texts))

        elif t == "image":
            lines.append("[Image]")

    return lines


def _collect_shape_images(node) -> List[Dict[str, Any]]:
    imgs = []
    for ch in (getattr(node, "children", []) or []):
        if getattr(ch, "type", None) == "image":
            content = getattr(ch, "content", {}) or {}
            px = 96 / 914400
            imgs.append({
                "image_url": content.get("image_url") or content.get("data_uri"),
                "width_px":  round((content.get("width_emu", 0) or 0) * px) or None,
                "height_px": round((content.get("height_emu", 0) or 0) * px) or None,
                "mime":      content.get("mime"),
                "sha256":    content.get("sha256"),
            })
        else:
            imgs.extend(_collect_shape_images(ch))
    return imgs


def _serialize_shape_side(block: Optional[Block]) -> Optional[Dict[str, Any]]:
    if not block:
        return None
    base = _serialize_block_side(block)
    if base is None:
        return None

    lines = _collect_shape_text_lines(block.node)

    if not lines:
        content   = block.node.content or {}
        img_count = content.get("image_count", 0) or 0
        tbl_count = content.get("table_count", 0) or 0
        preview   = []
        if img_count:
            preview.append(f"Image × {img_count}")
        if tbl_count:
            preview.append(f"Table × {tbl_count}")
        lines = [f"[{' , '.join(preview)}]"] if preview else ["[Empty shape]"]

    base["shape_text_lines"] = lines
    base["shape_images"]     = _collect_shape_images(block.node)
    return base


# ══════════════════════════════════════════════════════════════════════════════
# Shape content helpers
# ══════════════════════════════════════════════════════════════════════════════

def _shape_has_text(block: Block) -> bool:
    def _check(node) -> bool:
        for child in (getattr(node, "children", []) or []):
            if getattr(child, "type", None) == "paragraph":
                if _norm((getattr(child, "content", {}) or {}).get("text", "")):
                    return True
            elif getattr(child, "type", None) == "shape":
                if _check(child):
                    return True
        return False
    return _check(block.node)


def _shape_has_content(block: Block) -> bool:
    # Fix #8: dùng thực nội dung thay vì bool(children)
    # → tránh coi shape có children placeholder rỗng là có content
    return bool(
        _collect_shape_text_lines(block.node)
        or _collect_shape_images(block.node)
    )


# ══════════════════════════════════════════════════════════════════════════════
# Section key
# ══════════════════════════════════════════════════════════════════════════════

def _section_key(h: Optional[str]) -> str:
    return h if h else "(No heading)"

def _heading_meta(left: Optional[Dict[str, Any]], right: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    left_h = (left or {}).get("heading")
    right_h = (right or {}).get("heading")

    if left_h and right_h and left_h != right_h:
        return {
            "heading": None,
            "left_heading": left_h,
            "right_heading": right_h,
            "heading_changed": True,
        }

    h = left_h or right_h
    return {
        "heading": h,
        "left_heading": left_h,
        "right_heading": right_h,
        "heading_changed": False,
    }

# ══════════════════════════════════════════════════════════════════════════════
# Change builder
# ══════════════════════════════════════════════════════════════════════════════

def _change_kind(left: Any, right: Any) -> str:
    if left is not None and right is None:
        return "delete"
    if left is None and right is not None:
        return "insert"
    return "replace"


def _make_change(
    cid:         int,
    change_type: str,
    heading:     Optional[str],
    order:       int,
    left:        Optional[Dict[str, Any]],
    right:       Optional[Dict[str, Any]],
    extra:       Optional[Dict[str, Any]] = None,
    seq_index:   int = 0,
) -> Dict[str, Any]:
    hm = _heading_meta(left, right)

    return {
        "id":              cid,
        "heading":         hm["heading"] if hm["heading"] is not None else heading,
        "left_heading":    hm["left_heading"],
        "right_heading":   hm["right_heading"],
        "heading_changed": hm["heading_changed"],
        "type":            change_type,
        "change_kind":     _change_kind(left, right),
        "order":           order,
        "seq_index":       seq_index,
        "left":            left,
        "right":           right,
        **(extra or {}),
    }

# ══════════════════════════════════════════════════════════════════════════════
# Per-type processors
# ══════════════════════════════════════════════════════════════════════════════

def _process_paragraph(cid, a, b, ai, bj, a_blocks, b_blocks):
    old_raw     = a.node.content.get("text", "")
    new_raw     = b.node.content.get("text", "")
    old_display = a.node.content.get("text_display") or old_raw
    new_display = b.node.content.get("text_display") or new_raw

    text_same = _norm(old_raw) == _norm(new_raw)

    old_numbering = a.node.content.get("numbering")
    new_numbering = b.node.content.get("numbering")
    numbering_same = old_numbering == new_numbering

    a_imgs = [c for c in (a.node.children or []) if c.type == "image"]
    b_imgs = [c for c in (b.node.children or []) if c.type == "image"]
    imgs_same = (
        len(a_imgs) == len(b_imgs)
        and all(images_equal(ai_img, bi_img) for ai_img, bi_img in zip(a_imgs, b_imgs))
    )

    if text_same and imgs_same and numbering_same:
        return None

    heading = a.heading_ctx or b.heading_ctx
    order   = min(a.order, b.order)
    return _make_change(
        cid=cid, change_type="paragraph_modified", heading=heading, order=order,
        left=_serialize_block_side(a), right=_serialize_block_side(b),
        extra={
            "word_diff": diff_words(
                old_text=old_raw,
                new_text=new_raw,
                old_display=old_display,
                new_display=new_display,
                old_numbering=old_numbering,
                new_numbering=new_numbering,
            ),
            "left_context":  _build_context(a_blocks, ai),
            "right_context": _build_context(b_blocks, bj),
        },
    )


def _process_heading(cid, a, b, ai, bj, a_blocks, b_blocks):
    old_raw   = a.node.content.get("text", "")
    new_raw   = b.node.content.get("text", "")
    old_norm  = _norm(old_raw)
    new_norm  = _norm(new_raw)
    old_level = int(a.node.content.get("level", 1) or 1)
    new_level = int(b.node.content.get("level", 1) or 1)

    old_numbering = a.node.content.get("numbering")
    new_numbering = b.node.content.get("numbering")
    numbering_same = old_numbering == new_numbering

    if old_norm == new_norm and old_level == new_level and numbering_same:
        return None

    heading = a.heading_ctx or b.heading_ctx
    order   = min(a.order, b.order)
    return _make_change(
        cid=cid, change_type="heading_modified", heading=heading, order=order,
        left=_serialize_block_side(a), right=_serialize_block_side(b),
        extra={
            "word_diff": (
                diff_words(
                    old_text=old_raw,
                    new_text=new_raw,
                    old_display=a.node.content.get("text_display"),
                    new_display=b.node.content.get("text_display"),
                    old_numbering=a.node.content.get("numbering"),
                    new_numbering=b.node.content.get("numbering"),
                )
                if (
                    old_norm != new_norm
                    or a.node.content.get("numbering") != b.node.content.get("numbering")
                )
                else None
            ),
            "old_level":     old_level,
            "new_level":     new_level,
            "level_changed": old_level != new_level,
            "left_context":  _build_context(a_blocks, ai),
            "right_context": _build_context(b_blocks, bj),
        },
    )


def _process_table(cid, a, b, ai, bj, a_blocks, b_blocks):
    """
    Gọi analyze_table_change(LogicalTable, LogicalTable).
    Trả None nếu không có thay đổi thật sự.
    """
    a_logical = getattr(a.node, "logical_table", None)
    b_logical = getattr(b.node, "logical_table", None)

    # Cả 2 đều None → không diff được, bỏ
    if a_logical is None and b_logical is None:
        return None

    # Fix #9: thiếu logical_table một bên → vẫn emit change, analysis=None
    # tránh crash và để frontend thấy table changed
    if a_logical is None or b_logical is None:
        heading = a.heading_ctx or b.heading_ctx
        return _make_change(
            cid=cid, change_type="table_modified", heading=heading,
            order=min(a.order, b.order),
            left=_serialize_block_side(a), right=_serialize_block_side(b),
            extra={
                "table_analysis": None,
                "left_context":   _build_context(a_blocks, ai),
                "right_context":  _build_context(b_blocks, bj),
            },
        )

    analysis = analyze_table_change(a_tbl=a_logical, b_tbl=b_logical)
    if analysis is None:
        return None

    heading = a.heading_ctx or b.heading_ctx
    return _make_change(
        cid=cid, change_type="table_modified", heading=heading,
        order=min(a.order, b.order),
        left=_serialize_block_side(a), right=_serialize_block_side(b),
        extra={
            "table_analysis": analysis,
            "left_context":   _build_context(a_blocks, ai),
            "right_context":  _build_context(b_blocks, bj),
        },
    )


def _process_shape(cid, a, b, ai, bj, a_blocks, b_blocks):
    a_has_text    = _shape_has_text(a)
    b_has_text    = _shape_has_text(b)
    a_has_content = _shape_has_content(a)
    b_has_content = _shape_has_content(b)

    if not a_has_text and not b_has_text and not a_has_content and not b_has_content:
        return None

    left_side  = _serialize_shape_side(a)
    right_side = _serialize_shape_side(b)
    heading    = a.heading_ctx or b.heading_ctx
    order      = min(a.order, b.order)

    shape_changes = diff_shape(a.node, b.node)

    if a_has_content and b_has_content and not shape_changes:
        return None

    return _make_change(
        cid=cid, change_type="shape_modified", heading=heading, order=order,
        left=left_side, right=right_side,
        extra={
            # Fix #6: dùng a_has_content thay vì a_has_text
            # → shape chỉ có image (không text) bị xóa vẫn được đánh dấu cleared đúng
            "shape_cleared":      a_has_content and not b_has_content,
            "shape_changes":      shape_changes,
            "shape_text_preview": (left_side or {}).get("shape_text_lines", []),
            "left_context":       _build_context(a_blocks, ai),
            "right_context":      _build_context(b_blocks, bj),
        },
    )


def _process_image(cid, a, b, ai, bj, a_blocks, b_blocks):
    if images_equal(a.node, b.node):
        return None
    heading    = a.heading_ctx or b.heading_ctx
    img_change = build_image_change(a.node, b.node)
    return _make_change(
        cid=cid, change_type="image_modified", heading=heading,
        order=min(a.order, b.order),
        left=_serialize_block_side(a), right=_serialize_block_side(b),
        extra={
            "image_change":  img_change,
            "left_img":      img_change.get("left_img"),
            "right_img":     img_change.get("right_img"),
            "left_context":  _build_context(a_blocks, ai),
            "right_context": _build_context(b_blocks, bj),
        },
    )


def _process_insert(cid, b, bj, b_blocks):
    if b.type == "shape" and not _shape_has_text(b) and not _shape_has_content(b):
        return None

    right_side = _serialize_shape_side(b) if b.type == "shape" else _serialize_block_side(b)

    extra: Dict[str, Any] = {
        "left_context":  None,
        "right_context": _build_context(b_blocks, bj),
    }

    if b.type == "table":
        b_logical = getattr(b.node, "logical_table", None)
        # Fix #1: guard None — chỉ gọi khi có logical_table
        if b_logical is not None:
            extra["table_analysis"] = analyze_table_change(a_tbl=None, b_tbl=b_logical)
        else:
            extra["table_analysis"] = None

    elif b.type == "shape":
        extra["shape_text_preview"] = (right_side or {}).get("shape_text_lines", [])

    elif b.type == "image":
        img_change = build_image_change(None, b.node)
        extra["image_change"] = img_change
        extra["right_img"]    = img_change.get("right_img")

    return _make_change(
        cid=cid, change_type=f"{b.type}_inserted",
        heading=b.heading_ctx, order=b.order,
        left=None, right=right_side, extra=extra,
    )


def _process_delete(cid, a, ai, a_blocks):
    if a.type == "shape" and not _shape_has_text(a) and not _shape_has_content(a):
        return None

    left_side = _serialize_shape_side(a) if a.type == "shape" else _serialize_block_side(a)

    extra: Dict[str, Any] = {
        "left_context":  _build_context(a_blocks, ai),
        "right_context": None,
    }

    if a.type == "table":
        a_logical = getattr(a.node, "logical_table", None)
        # Fix #1: guard None — chỉ gọi khi có logical_table
        if a_logical is not None:
            extra["table_analysis"] = analyze_table_change(a_tbl=a_logical, b_tbl=None)
        else:
            extra["table_analysis"] = None

    elif a.type == "shape":
        extra["shape_text_preview"] = (left_side or {}).get("shape_text_lines", [])

    elif a.type == "image":
        img_change = build_image_change(a.node, None)
        extra["image_change"] = img_change
        extra["left_img"]     = img_change.get("left_img")

    return _make_change(
        cid=cid, change_type=f"{a.type}_deleted",
        heading=a.heading_ctx, order=a.order,
        left=left_side, right=None, extra=extra,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Replace dispatcher
# ══════════════════════════════════════════════════════════════════════════════

def _dispatch_replace(
    refined: List[Tuple[int, Dict[str, Any]]],
    tag: str, i1: int, i2: int, j1: int, j2: int,
    a_blocks: List[Block],
    b_blocks: List[Block],
) -> None:
    """
    Fix #7: aligner emit replace 1-1 sau refine.
    Nếu vẫn nhận slice dài (aligner fail), không tự đoán pair theo vị trí.
    Emit delete hết + insert hết — safe hơn pair sai.
    """
    a_slice = a_blocks[i1:i2]
    b_slice = b_blocks[j1:j2]

    # Guard: aligner nên luôn emit 1-1 sau refine.
    # Nếu nhận slice dài → aligner fail, builder không tự đoán pair.
    # Emit delete hết rồi insert hết — an toàn hơn pair theo vị trí.
    if len(a_slice) != 1 or len(b_slice) != 1:
        for i in range(i1, i2):
            seq    = i * _SEQ_MULT
            change = _process_delete(0, a_blocks[i], i, a_blocks)
            if change is not None:
                refined.append((seq, change))

        for j in range(j1, j2):
            seq = _seq_for_insert(i2, j - j1)
            change = _process_insert(0, b_blocks[j], j, b_blocks)
            if change is not None:
                refined.append((seq, change))
        return

    # 1-1 case
    ai = i1
    bj = j1
    a  = a_blocks[ai]
    b  = b_blocks[bj]
    seq = ai * _SEQ_MULT

    change: Optional[Dict[str, Any]] = None

    if a.type == "paragraph" and b.type == "paragraph":
        change = _process_paragraph(0, a, b, ai, bj, a_blocks, b_blocks)
    elif a.type == "heading" and b.type == "heading":
        change = _process_heading(0, a, b, ai, bj, a_blocks, b_blocks)
    elif a.type == "table" and b.type == "table":
        change = _process_table(0, a, b, ai, bj, a_blocks, b_blocks)
    elif a.type == "shape" and b.type == "shape":
        change = _process_shape(0, a, b, ai, bj, a_blocks, b_blocks)
    elif a.type == "image" and b.type == "image":
        change = _process_image(0, a, b, ai, bj, a_blocks, b_blocks)
    else:
        # Fix #2: mixed type — dùng offset lớn tránh seq collision
        del_change = _process_delete(0, a, ai, a_blocks)
        ins_change = _process_insert(0, b, bj, b_blocks)
        if del_change is not None:
            refined.append((seq, del_change))
        if ins_change is not None:
            refined.append((_seq_for_insert(ai, 0), ins_change))
        return

    if change is not None:
        refined.append((seq, change))


# ══════════════════════════════════════════════════════════════════════════════
# Main builder
# ══════════════════════════════════════════════════════════════════════════════

def build_ui_json(
    a_blocks: List[Block],
    b_blocks: List[Block],
    opcodes,
) -> Dict[str, Any]:
    opcodes = sorted(opcodes, key=lambda op: (op[1], op[3]))

    global_pending: List[Tuple[int, Dict[str, Any]]] = []

    def _collect(change: Optional[Dict[str, Any]], seq: int) -> None:
        if change is not None:
            global_pending.append((seq, change))

    for tag, i1, i2, j1, j2 in opcodes:

        if tag == "equal":
            continue

        if tag == "insert":
            for j in range(j1, j2):
                seq = _seq_for_insert(i1, j - j1)
                change = _process_insert(0, b_blocks[j], j, b_blocks)
                _collect(change, seq)

        elif tag == "delete":
            for i in range(i1, i2):
                seq    = i * _SEQ_MULT
                change = _process_delete(0, a_blocks[i], i, a_blocks)
                _collect(change, seq)

        elif tag == "replace":
            _dispatch_replace(global_pending, tag, i1, i2, j1, j2, a_blocks, b_blocks)

    global_pending.sort(key=lambda x: x[0])

    flat_changes:     List[Dict[str, Any]] = []
    ordered_sections: List[Dict[str, Any]] = []
    last_heading:     Optional[str]        = None
    current_section:  Optional[Dict[str, Any]] = None

    for cid, (seq, change) in enumerate(global_pending, start=1):
        change["id"]        = cid
        change["seq_index"] = seq

        if change.get("heading_changed"):
            section_key = (
                f"{change.get('left_heading') or '(No heading)'}"
                f"__TO__"
                f"{change.get('right_heading') or '(No heading)'}"
            )

            section_data = {
                "heading": None,
                "left_heading": change.get("left_heading"),
                "right_heading": change.get("right_heading"),
                "heading_changed": True,
                "changes": [],
            }
        else:
            section_key = _section_key(change.get("heading"))

            section_data = {
                "heading": section_key,
                "left_heading": change.get("left_heading"),
                "right_heading": change.get("right_heading"),
                "heading_changed": False,
                "changes": [],
            }

        if current_section is None or section_key != last_heading:
            current_section = section_data
            ordered_sections.append(current_section)
            last_heading = section_key

        current_section["changes"].append(change)
        flat_changes.append(change)

    sort_key = lambda x: (x.get("seq_index", 0), x.get("id", 0))
    for section in ordered_sections:
        section["changes"].sort(key=sort_key)

    return {
        "sections": ordered_sections,
        "changes":  sorted(flat_changes, key=sort_key),
    }