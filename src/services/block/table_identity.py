"""
block/table_identity.py
~~~~~~~~~~~~~~~~~~~~~~~
Extract feature mềm từ LogicalTable để dùng cho similarity matching.
KHÔNG hash chết — mục đích là identity, không phải equality.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Set

from src.services.models.block import Block
from src.services.extractor.utils import norm_for_signature as _norm


@dataclass
class TableIdentity:
    heading_ctx:   str
    total_cols:    int
    total_rows:    int
    row_texts:     List[str]   # text từng anchor row (join các cell)
    token_set:     Set[str]    # toàn bộ token trong bảng
    image_hashes:  List[str]   # sha256[:16] của ảnh trong bảng
    section_index: int         # vị trí block trong danh sách


def _tokenize(text: str) -> Set[str]:
    return set(re.findall(r"\w+", text.lower()))


def extract_table_identity(block: Block, section_index: int) -> TableIdentity:
    lt = getattr(block.node, "logical_table", None)

    if lt is None:
        txt = _norm(block.node.content.get("text", ""))
        return TableIdentity(
            heading_ctx   = block.heading_ctx or "",
            total_cols    = int(block.node.content.get("col_count", 0) or 0),
            total_rows    = int(block.node.content.get("row_count", 0) or 0),
            row_texts     = [txt] if txt else [],
            token_set     = _tokenize(txt),
            image_hashes  = [],
            section_index = section_index,
        )

    row_texts:    List[str] = []
    all_tokens:   Set[str]  = set()
    image_hashes: List[str] = []

    for r in lt.anchor_rows():
        cells    = lt.cells_in_row(r)
        row_text = " ".join(_norm(c.text) for c in cells if c.text)
        row_texts.append(row_text)
        all_tokens |= _tokenize(row_text)

        for cell in cells:
            for cb in cell.content_blocks:
                if cb.type != "image":
                    continue
                img = cb.as_image()
                if img and img.sha256:
                    image_hashes.append(img.sha256[:16])

    return TableIdentity(
        heading_ctx   = block.heading_ctx or "",
        total_cols    = lt.total_cols,
        total_rows    = lt.total_rows,
        row_texts     = row_texts,
        token_set     = all_tokens,
        image_hashes  = image_hashes,
        section_index = section_index,
    )