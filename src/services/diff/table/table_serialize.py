# src/services/diff/table/table_serialize.py
"""
Serialize LogicalTable / LogicalCell → dict để trả về frontend.

Bỏ hoàn toàn:
- build_row_display_cells (frontend tự render từ anchor + span)
- serialize_node / serialize_row dựa trên DocNode row/cell

Thay bằng:
- serialize_logical_cell   — LogicalCell → dict với đủ rowspan/colspan
- serialize_logical_table  — LogicalTable → dict với cells flat list
- get_header_row           — row đầu tiên đã serialize

Frontend nhận cells flat list + anchor/span → tự dựng CSS Grid.
Không cần backend tính display grid nữa.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.services.models.logical_table import (
    ImagePayload,
    LogicalCell,
    LogicalTable,
    ParagraphPayload,
)
from src.services.diff.table.table_helpers import get_anchor_rows, get_cells_in_row


# ══════════════════════════════════════════════════════════════════════════════
# Image serializer
# ══════════════════════════════════════════════════════════════════════════════

def serialize_image(img: ImagePayload) -> Dict[str, Any]:
    return {
        "uid":       img.uid,
        "image_url": img.image_url,
        "sha256":    img.sha256,
        "width_px":  img.width_px,
        "height_px": img.height_px,
        "mime":      img.mime,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Paragraph serializer
# ══════════════════════════════════════════════════════════════════════════════

def serialize_paragraph(para: ParagraphPayload) -> Dict[str, Any]:
    return {
        "uid":          para.uid,
        "text":         para.text,
        "text_display": para.text_display,
        "images":       [serialize_image(img) for img in para.images],
    }


# ══════════════════════════════════════════════════════════════════════════════
# Cell serializer
# ══════════════════════════════════════════════════════════════════════════════

def serialize_logical_cell(cell: Optional[LogicalCell]) -> Optional[Dict[str, Any]]:
    if cell is None:
        return None

    image_urls: List[str] = []
    standalone_images: List[Dict[str, Any]] = []
    paragraph_images: List[Dict[str, Any]] = []
    paragraphs_out: List[Dict[str, Any]] = []
    content_blocks_out: List[Dict[str, Any]] = []

    for block in cell.content_blocks:
        if block.type == "paragraph":
            para = block.as_paragraph()
            if para is None:
                continue
            p_dict = serialize_paragraph(para)
            paragraphs_out.append(p_dict)
            content_blocks_out.append({"type": "paragraph", "payload": p_dict})
            for img in para.images:
                img_dict = serialize_image(img)
                paragraph_images.append(img_dict)
                if img.image_url:
                    image_urls.append(img.image_url)

        elif block.type == "image":
            img = block.as_image()
            if img is None:
                continue
            img_dict = serialize_image(img)
            standalone_images.append(img_dict)
            content_blocks_out.append({"type": "image", "payload": img_dict})
            if img.image_url:
                image_urls.append(img.image_url)

        elif block.type == "table":
            tbl = block.as_table()
            if tbl is None:
                continue
            tbl_dict = serialize_logical_table(tbl)
            content_blocks_out.append({"type": "table", "payload": tbl_dict})

        elif block.type == "shape":
            shp = block.as_shape()
            if shp is None:
                continue
            # Shape không thêm vào paragraphs nhưng vẫn cần trong content_blocks
            content_blocks_out.append({
                "type": "shape",
                "payload": {
                    "uid": shp.uid,
                    "text": shp.text,
                    "text_display": shp.text_display,
                    "images": [serialize_image(i) for i in shp.images],
                }
            })

    # Fallback: nếu content_blocks rỗng (data cũ chưa migrate)
    # thì fall back về property-based như cũ
    if not content_blocks_out:
        for para in cell.paragraphs:
            p_dict = serialize_paragraph(para)
            paragraphs_out.append(p_dict)
            content_blocks_out.append({"type": "paragraph", "payload": p_dict})
            for img in para.images:
                img_dict = serialize_image(img)
                paragraph_images.append(img_dict)
                if img.image_url:
                    image_urls.append(img.image_url)
        for img in cell.images:
            img_dict = serialize_image(img)
            standalone_images.append(img_dict)
            content_blocks_out.append({"type": "image", "payload": img_dict})
            if img.image_url:
                image_urls.append(img.image_url)

    return {
        "uid":          cell.uid,
        "anchor_row":   cell.anchor_row,
        "anchor_col":   cell.anchor_col,
        "row_span":     cell.row_span,
        "col_span":     cell.col_span,
        "text":         cell.text,
        "text_display": cell.text_display,

        "paragraphs":        paragraphs_out,

        # Backward compat
        "images":            image_urls,

        # Rõ nghĩa hơn
        "image_urls":        image_urls,
        "standalone_images": standalone_images,
        "paragraph_images":  paragraph_images,

        # Source of truth cho frontend render đúng thứ tự
        "content_blocks":    content_blocks_out,

        "nested_table": serialize_logical_table(cell.nested_table) if cell.nested_table else None,
    }

# ══════════════════════════════════════════════════════════════════════════════
# Table serializer
# ══════════════════════════════════════════════════════════════════════════════

def serialize_logical_table(tbl: Optional[LogicalTable]) -> Optional[Dict[str, Any]]:
    if tbl is None:
        return None

    cells = sorted(
        tbl.cells,
        key=lambda c: (c.anchor_row, c.anchor_col)
    )

    return {
        "uid":        tbl.uid,
        "total_rows": tbl.total_rows,
        "total_cols": tbl.total_cols,
        "cells":      [serialize_logical_cell(c) for c in cells],
    }


# ══════════════════════════════════════════════════════════════════════════════
# Header row
# ══════════════════════════════════════════════════════════════════════════════

def get_header_row(tbl: LogicalTable) -> Optional[Dict[str, Any]]:
    """
    Trả về cells của anchor row đầu tiên — dùng làm header.
    """
    rows = get_anchor_rows(tbl)
    if not rows:
        return None
    first_row = rows[0]
    return {
        "anchor_row": first_row,
        "cells": [serialize_logical_cell(c) for c in get_cells_in_row(tbl, first_row)],
    }