"""
block/signature.py
~~~~~~~~~~~~~~~~~~
Tạo fingerprint ngắn gọn cho mỗi DocNode.

Nguyên tắc thiết kế:
1. STABLE     — cùng nội dung, khác vị trí trong file → signature giống nhau
2. SENSITIVE  — khác nội dung dù nhỏ → signature khác nhau
3. NO TRUNCATE — không cắt text trước khi hash (tránh collision bảng lớn)
4. SEMANTIC   — chỉ hash những gì có nghĩa với người dùng,
                bỏ qua metadata format

Thay đổi so với version cũ:
    - _sig_logical_cell: walk content_blocks theo thứ tự thay vì đọc
      cell.images / cell.paragraphs / cell.nested_table riêng rẽ.
      Đảm bảo thứ tự nội dung trong cell được phản ánh vào signature,
      và shape không còn bị bỏ qua.
    - _sig_logical_table: không đổi logic, chỉ dùng cells_in_row().
    - _sig_row, _sig_cell: giữ nguyên để tương thích các nơi còn import
      nhưng không được gọi từ _sig_logical_cell / _sig_logical_table.
"""

from __future__ import annotations

import hashlib
import re
from typing import List, Tuple

from src.services.models.docnode import DocNode
from src.services.models.logical_table import (
    CellContentBlock,
    LogicalCell,
    LogicalTable,
)
from src.services.extractor.utils import norm_for_signature as _norm_text
from src.services.extractor.shape.shape_content import collect_shape_parts
from src.services.utils.shape_sig import stable_empty_shape_id


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _hash(value: str, length: int = 16) -> str:
    if not value:
        return "empty"
    return hashlib.md5(value.encode("utf-8")).hexdigest()[:length]


def _numbering_key(node: DocNode) -> str:
    """
    Semantic key cho numbering/list.

    Dùng level + num_fmt + lvl_text thay vì num_id vì num_id
    chỉ ổn định trong cùng document, không semantic.
    """
    numbering = node.content.get("numbering") or None
    if not numbering:
        return ""

    level    = int(numbering.get("level", 0) or 0)
    num_fmt  = _norm_text(numbering.get("num_fmt",  "") or "")
    lvl_text = _norm_text(numbering.get("lvl_text", "") or "")

    return f"N:{level}:{num_fmt}:{lvl_text}"


# ══════════════════════════════════════════════════════════════════════════════
# Heading split
# ══════════════════════════════════════════════════════════════════════════════

_CHAPTER_RE = re.compile(r"^(\d+(?:\.\d+)*)\s+(.*)", re.DOTALL)


def _split_heading(text: str) -> Tuple[str, str]:
    t = _norm_text(text).lower()
    m = _CHAPTER_RE.match(t)
    if m:
        return m.group(1), m.group(2).strip()
    return "", t


# ══════════════════════════════════════════════════════════════════════════════
# Per-type signature builders
# ══════════════════════════════════════════════════════════════════════════════

def _inline_image_keys(node: DocNode) -> List[str]:
    """
    Key cho inline image nằm trong paragraph/heading.

    Ưu tiên sha256. Fallback theo name/size/uid để tránh
    paragraph có ảnh bị coi như text-only.
    """
    keys: List[str] = []

    for child in node.children or []:
        if child.type != "image":
            continue

        sha = (child.content.get("sha256", "") or "").strip()
        if sha:
            keys.append(f"img:{sha[:16]}")
            continue

        name   = _norm_text(child.content.get("name", "") or "")
        width  = int(child.content.get("width_emu",  0) or 0)
        height = int(child.content.get("height_emu", 0) or 0)

        fallback = (
            name
            or (f"{width}x{height}" if width or height else "")
            or getattr(child, "uid", "")
            or ""
        )

        keys.append(f"img:nosha:{_hash(fallback, 8)}")

    return keys


def _sig_heading(node: DocNode) -> str:
    parts: List[str] = []

    txt   = _norm_text(node.content.get("text", ""))
    level = int(node.content.get("level", 0) or 0)

    if txt:
        parts.append(f"txt:{txt}")

    nk = _numbering_key(node)
    if nk:
        parts.append(nk)

    parts.extend(_inline_image_keys(node))

    combined = "|".join(parts)
    return f"H{level}:{_hash(combined)}"


def _sig_paragraph(node: DocNode) -> str:
    parts: List[str] = []

    txt = _norm_text(node.content.get("text", ""))
    if txt:
        parts.append(f"txt:{txt}")

    nk = _numbering_key(node)
    if nk:
        parts.append(nk)

    parts.extend(_inline_image_keys(node))

    combined = "|".join(parts)
    return f"P:{_hash(combined)}"


# ── LogicalCell / LogicalTable ────────────────────────────────────────────────

def _key_for_block(block: CellContentBlock) -> str:
    """
    Hash key cho một CellContentBlock.

    Walk theo thứ tự content_blocks để signature phản ánh đúng
    thứ tự nội dung trong cell (paragraph xen kẽ image/shape/table).

    Dùng as_* helpers thay vì cast thủ công.
    """
    if block.type == "paragraph":
        p = block.as_paragraph()
        if p is None:
            return "P:NONE"

        parts: List[str] = []
        txt = _norm_text(p.text)
        if txt:
            parts.append(f"txt:{txt}")

        # Inline images trong paragraph
        for img in p.images:
            if img.sha256:
                parts.append(f"img:{img.sha256[:16]}")
            elif img.uid:
                parts.append(f"img:nosha:{_hash(img.uid, 8)}")

        return f"P:{_hash('|'.join(parts))}" if parts else f"P:empty:{p.uid}"

    if block.type == "image":
        img = block.as_image()
        if img is None:
            return "IMG:NONE"

        if img.sha256:
            return f"img:{img.sha256[:16]}"
        if img.uid:
            return f"img:nosha:{_hash(img.uid, 8)}"
        return "img:unknown"

    if block.type == "shape":
        shp = block.as_shape()
        if shp is None:
            return "SHP:NONE"

        parts = []
        txt = _norm_text(shp.text)
        if txt:
            parts.append(f"txt:{txt}")
        for img in shp.images:
            if img.sha256:
                parts.append(f"img:{img.sha256[:16]}")
            elif img.uid:
                parts.append(f"img:nosha:{_hash(img.uid, 8)}")

        return f"SHP:{_hash('|'.join(parts))}" if parts else f"SHP:empty:{shp.uid}"

    if block.type == "table":
        tbl = block.as_table()
        if tbl is None:
            return "TBL:NONE"
        return _sig_logical_table(tbl)

    return f"{block.type.upper()}:UNKNOWN"


def _sig_logical_cell(cell: LogicalCell) -> str:
    """
    Signature của 1 LogicalCell.

    Walk content_blocks theo thứ tự xuất hiện trong Word — không hash
    anchor_row / anchor_col / span vì đây là layout, không phải nội dung.

    Thứ tự block được phản ánh vào signature:
        [P:"Mô tả", IMG:chart, P:"Ghi chú"] ≠ [P:"Ghi chú", IMG:chart, P:"Mô tả"]

    Nếu content_blocks rỗng (extractor cũ chưa migrate), fallback về cell.text
    để không bị "empty" sai.
    """
    if cell.content_blocks:
        parts = [_key_for_block(b) for b in cell.content_blocks]
        combined = "|".join(parts)
        return f"C:{_hash(combined)}"

    # Fallback — extractor chưa fill content_blocks
    if cell.text:
        return f"C:{_hash(_norm_text(cell.text))}"

    # Empty cell — dùng span để 2 cell cùng span match được,
    # không dùng position (anchor_row/col) vì vi phạm STABLE
    return f"C:empty:{cell.row_span}x{cell.col_span}"


def _sig_logical_table(tbl: LogicalTable) -> str:
    """
    Signature của LogicalTable — hash theo từng anchor row.

    Mỗi anchor row được hash từ các master cell trong row đó,
    sort theo anchor_col để đảm bảo thứ tự nhất quán.
    """
    row_sigs: List[str] = []

    for r in tbl.anchor_rows():
        cells     = tbl.cells_in_row(r)
        cell_sigs = "|".join(_sig_logical_cell(c) for c in cells)
        row_sigs.append(f"R:{_hash(cell_sigs)}")

    combined = "|".join(row_sigs)
    return f"T:{_hash(combined)}"


def _sig_table(node: DocNode) -> str:
    """
    Signature của table DocNode.

    Ưu tiên logical_table nếu có (path mới).
    Fallback về text trong content nếu không có (tương thích cũ).
    """
    logical: LogicalTable | None = getattr(node, "logical_table", None)
    if logical is not None:
        return _sig_logical_table(logical)

    # Fallback tương thích cũ
    rows = int(node.content.get("row_count", 0) or 0)
    cols = int(node.content.get("col_count", 0) or 0)
    txt  = _norm_text(node.content.get("text", ""))
    return f"T:{rows}x{cols}:{_hash(txt)}"


def _sig_row_from_logical(tbl: LogicalTable, anchor_row: int) -> str:
    """
    Signature của 1 anchor row trong LogicalTable.
    Dùng bởi diff_table để align rows.
    """
    cells     = tbl.cells_in_row(anchor_row)
    cell_sigs = "|".join(_sig_logical_cell(c) for c in cells)
    return f"R:{_hash(cell_sigs)}"


# ── Tương thích cũ — không gọi từ logical path ───────────────────────────────

def _sig_row(node: DocNode) -> str:
    """Giữ lại để tương thích các nơi còn import trực tiếp."""
    if node.children:
        cells_sig = "|".join(
            _sig_cell(c) for c in node.children if c.type == "cell"
        )
        return f"R:{_hash(cells_sig)}"
    row_text = _norm_text(node.content.get("text", ""))
    return f"R:{_hash(row_text)}"


def _sig_cell(node: DocNode) -> str:
    """Giữ lại để tương thích các nơi còn import trực tiếp."""
    parts: List[str] = []

    if node.children:
        for child in node.children:
            if child.type == "paragraph":
                txt = _norm_text(child.content.get("text", ""))
                if txt:
                    parts.append(txt)
                for gc in child.children or []:
                    if gc.type == "image":
                        sha = gc.content.get("sha256", "") or ""
                        if sha:
                            parts.append(f"img:{sha[:16]}")
                        else:
                            uid = getattr(gc, "uid", "") or ""
                            parts.append(f"img:nosha:{_hash(uid, 8)}")
            elif child.type == "image":
                sha = child.content.get("sha256", "") or ""
                if sha:
                    parts.append(f"img:{sha[:16]}")
                else:
                    name = child.content.get("name", "") or ""
                    w    = child.content.get("width_emu",  0) or 0
                    h    = child.content.get("height_emu", 0) or 0
                    parts.append(f"img:nosha:{_hash(name or f'{w}x{h}', 8)}")
            elif child.type == "table":
                parts.append(_sig_table(child))
    else:
        txt = _norm_text(node.content.get("text", ""))
        if txt:
            parts.append(txt)

    combined = "|".join(parts)
    return f"C:{_hash(combined)}"


def _sig_image(node: DocNode) -> str:
    sha    = (node.content.get("sha256", "") or "").strip()
    width  = int(node.content.get("width_emu",  0) or 0)
    height = int(node.content.get("height_emu", 0) or 0)

    if sha:
        return f"I:{sha[:16]}:{width}x{height}"

    name = _norm_text(node.content.get("name", "") or "")
    if name:
        return f"I:{_hash(name, 12)}:{width}x{height}"

    uid = getattr(node, "uid", "") or ""
    return f"I:nodata:{_hash(uid, 8)}:{width}x{height}"


def _sig_shape(node: DocNode, heading_ctx: str = "") -> str:
    shape_type = node.content.get("shape_type", "") or ""
    parts      = collect_shape_parts(node)
    combined   = " | ".join(parts)

    if not combined:
        sid = stable_empty_shape_id(
            uid=getattr(node, "uid", None),
            parent_uid=getattr(node, "parent_uid", None),
            heading_ctx=heading_ctx,
        )
        return f"S:{shape_type}:empty:{sid}"

    if heading_ctx:
        combined = f"{heading_ctx}||{combined}"

    return f"S:{shape_type}:{_hash(combined)}"


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def signature(node: DocNode, heading_ctx: str = "") -> str:
    t = node.type
    if t == "heading":   return _sig_heading(node)
    if t == "paragraph": return _sig_paragraph(node)
    if t == "table":     return _sig_table(node)
    if t == "row":       return _sig_row(node)
    if t == "cell":      return _sig_cell(node)
    if t == "image":     return _sig_image(node)
    if t == "shape":     return _sig_shape(node, heading_ctx)

    txt = _norm_text(node.content.get("text", ""))
    return f"U:{t}:{_hash(txt, 12)}"