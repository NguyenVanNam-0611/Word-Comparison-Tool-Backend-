"""
extractor/shape_content.py
~~~~~~~~~~~~~~~~~~~~~~~~~~
Collect nội dung có nghĩa từ DocNode(type="shape") — text và image.

Đây là file TRUNG TÂM duy nhất định nghĩa "shape gồm những gì".
Tất cả nơi cần biết nội dung shape đều import từ đây:
    - signature.py      → _sig_shape dùng collect_shape_parts
    - sequence_align.py → _content_sig, _shape_text dùng collect_shape_parts
    - shape_diff.py     → collect_diffable_nodes dùng để diff

Nguyên tắc:
    - Text: lấy từ paragraph có text thực sự (sau norm_text)
    - Image: lấy sha256 — stable, không phụ thuộc tên file hay vị trí
    - Thứ tự: giữ đúng thứ tự xuất hiện trong shape (paragraph xen kẽ image)
    - Đệ quy: shape lồng nhau, table trong shape → cell → paragraph/image

KHÔNG xử lý: format (bold, font size, alignment).
"""

from __future__ import annotations

from typing import List

from src.services.models.docnode import DocNode
from src.services.extractor.utils import norm_text as _norm_text


# ══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ══════════════════════════════════════════════════════════════════════════════

def _collect_from_cell(cell: DocNode, out: List[str]) -> None:
    """
    Walk 1 cell → collect text và image hash.
    Đệ quy vào nested table trong cell.
    """
    for c in (cell.children or []):
        if c.type == "paragraph":
            _collect_from_paragraph(c, out)
        elif c.type == "table":
            _collect_from_table(c, out)


def _collect_from_paragraph(par: DocNode, out: List[str]) -> None:
    """
    Collect text và image từ 1 paragraph node.
    Image nằm trong children của paragraph (extract bởi shape.py).
    """
    txt = _norm_text(par.content.get("text", ""))
    if txt:
        out.append(txt)

    for gc in (par.children or []):
        if gc.type == "image":
            sha = gc.content.get("sha256", "") or ""
            if sha:
                out.append(f"img:{sha[:16]}")


def _collect_from_table(tbl: DocNode, out: List[str]) -> None:
    """
    Walk table → row → cell → collect text và image.
    Dùng sig của table thay vì flatten text thô
    để giữ cấu trúc row/cell order.
    """
    for row in (tbl.children or []):
        if row.type != "row":
            continue
        for cell in (row.children or []):
            if cell.type == "cell":
                _collect_from_cell(cell, out)


# ══════════════════════════════════════════════════════════════════════════════
# Public API — dùng cho signature và sequence_align
# ══════════════════════════════════════════════════════════════════════════════

def collect_shape_parts(node: DocNode) -> List[str]:
    """
    Collect tất cả nội dung có nghĩa từ shape node thành list string.

    Mỗi phần tử là:
        - text của paragraph (đã norm_text)
        - "img:{sha256[:16]}" cho image

    Thứ tự giữ nguyên theo thứ tự xuất hiện trong shape.
    Đệ quy vào: shape lồng nhau, table, cell.

    Dùng để:
        - Tính signature (join → hash)
        - So sánh nội dung (join → SequenceMatcher)
        - Kiểm tra shape rỗng (len == 0)

    Args:
        node: DocNode(type="shape")

    Returns:
        List[str] — rỗng nếu shape không có text lẫn image.
    """
    out: List[str] = []

    for child in (node.children or []):
        if child.type == "paragraph":
            _collect_from_paragraph(child, out)
        elif child.type == "image":
            # Image là direct child của shape (ít gặp nhưng cần cover)
            sha = child.content.get("sha256", "") or ""
            if sha:
                out.append(f"img:{sha[:16]}")
        elif child.type == "shape":
            # Shape lồng nhau — đệ quy
            out.extend(collect_shape_parts(child))
        elif child.type == "table":
            _collect_from_table(child, out)

    return out


# ══════════════════════════════════════════════════════════════════════════════
# Public API — dùng cho shape_diff
# ══════════════════════════════════════════════════════════════════════════════

def collect_diffable_nodes(node: DocNode) -> List[DocNode]:
    """
    Collect tất cả node có thể diff từ shape theo đúng thứ tự xuất hiện.

    Quy tắc emit:
        - Paragraph có text hoặc image con → emit paragraph là đơn vị duy nhất.
          Image con KHÔNG emit riêng — được xử lý bên trong _handle_replace_pair
          và _diff_image_children trong shape_diff.py.
        - Image direct child của shape (không có paragraph bọc) → emit riêng.
        - Shape lồng nhau, table → đệ quy.

    Tại sao không emit image con của paragraph riêng:
        Trước đây double-emit (paragraph + image con) tình cờ hoạt động vì
        _handle_replace_pair không check image con. Sau khi _handle_replace_pair
        đã tự diff image con, double-emit sẽ gây diff image hai lần.

    Paragraph rỗng hoàn toàn (không text, không image con) → bỏ qua.
    """
    result: List[DocNode] = []

    for child in (node.children or []):
        if child.type == "paragraph":
            has_text   = bool(_norm_text(child.content.get("text", "")))
            has_images = any(gc.type == "image" for gc in (child.children or []))

            if has_text or has_images:
                result.append(child)
            # Paragraph rỗng hoàn toàn → bỏ qua

        elif child.type == "image":
            # Image direct child của shape — không có paragraph bọc ngoài
            result.append(child)

        elif child.type == "shape":
            result.extend(collect_diffable_nodes(child))

        elif child.type == "table":
            result.extend(_collect_diffable_from_table(child))

    return result



def _collect_diffable_from_table(tbl: DocNode) -> List[DocNode]:
    """Walk table → row → cell → collect diffable nodes."""
    result: List[DocNode] = []
    for row in (tbl.children or []):
        if row.type != "row":
            continue
        for cell in (row.children or []):
            if cell.type == "cell":
                result.extend(_collect_diffable_from_cell(cell))
    return result


def _collect_diffable_from_cell(cell: DocNode) -> List[DocNode]:
    """
    Walk cell — áp dụng cùng quy tắc emit như collect_diffable_nodes.
    Image con của paragraph không emit riêng.
    """
    result: List[DocNode] = []
    for c in (cell.children or []):
        if c.type == "paragraph":
            has_text   = bool(_norm_text(c.content.get("text", "")))
            has_images = any(gc.type == "image" for gc in (c.children or []))

            if has_text or has_images:
                result.append(c)

        elif c.type == "image":
            result.append(c)

        elif c.type == "table":
            result.extend(_collect_diffable_from_table(c))
    return result