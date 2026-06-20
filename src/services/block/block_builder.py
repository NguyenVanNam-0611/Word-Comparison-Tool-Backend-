"""
block/block_builder.py
~~~~~~~~~~~~~~~~~~~~~~
Duyệt DocNode tree → List[Block] đã được:
    1. Lọc bỏ section không cần diff (TOC, revision history...)
    2. Track heading_ctx (breadcrumb)
    3. Inject signature(node, heading_ctx) — khớp với signature.py và sequence_align.py
    4. Sort theo order + fix shape heading
    5. Merge bảng bị cắt trang
    6. Fallback heuristic TOC
"""

from __future__ import annotations

from typing import List, Optional

from src.services.models.docnode import DocNode
from src.services.models.block import Block
from src.services.block.signature import signature
from src.services.extractor.utils import norm_for_signature
from src.services.block.filters import (
    _norm,
    _is_skip_section_heading,
    _is_skip_block,
    remove_heuristic_toc,
)
from src.services.block.table_merge import merge_consecutive_tables


# ══════════════════════════════════════════════════════════════════════════════
# Shape helpers
# ══════════════════════════════════════════════════════════════════════════════

def _shape_has_any_child(node: DocNode) -> bool:
    return bool(node.children)


# ══════════════════════════════════════════════════════════════════════════════
# Shape heading fix
# ══════════════════════════════════════════════════════════════════════════════

def _fix_shape_headings(blocks: List[Block]) -> List[Block]:
    """
    Fix heading_ctx cho shape floating bằng index trong list,
    không dùng order value.

    Shape floating có order cao bất thường (XML đặt sau cùng)
    → order-based lookup chọn heading sai (section sau thay vì section hiện tại).

    FIX: duyệt list theo thứ tự thực (đã sort), track heading cuối
    gặp được → assign cho shape ngay phía sau.
    """
    last_heading_ctx: Optional[str] = None

    for block in blocks:
        if block.type == "heading":
            last_heading_ctx = block.heading_ctx
            continue

        if block.type == "shape" and last_heading_ctx is not None:
            if block.heading_ctx != last_heading_ctx:
                block.heading_ctx = last_heading_ctx
                block.signature = signature(block.node, heading_ctx=last_heading_ctx)

    return blocks


# ══════════════════════════════════════════════════════════════════════════════
# Main builder
# ══════════════════════════════════════════════════════════════════════════════

def build_blocks(doc_root: DocNode) -> List[Block]:
    blocks: List[Block] = []

    current_heading: Optional[str]  = None
    current_heading_level: int      = 0
    heading_stack: List[dict]       = []
    skip_until_level: Optional[int] = None

    for node in (doc_root.children or []):

        if node.type == "heading":
            heading_level = int(node.content.get("level", 1) or 1)

            if skip_until_level is not None:
                if heading_level <= skip_until_level:
                    skip_until_level = None
                else:
                    continue

            if _is_skip_section_heading(node):
                skip_until_level = heading_level
                continue

            if _is_skip_block(node):
                continue

            heading_text = (node.content.get("text") or "").strip()

            # FIX: heading rỗng không update heading_ctx
            # → tránh làm nhiễu ctx của các block xung quanh
            if not heading_text:
                blocks.append(Block(
                    type          = "heading",
                    node          = node,
                    signature     = signature(node),
                    heading_ctx   = current_heading,
                    heading_level = current_heading_level,
                    order         = node.order,
                    uid           = node.uid,
                ))
                continue

            while heading_stack and heading_stack[-1]["level"] >= heading_level:
                heading_stack.pop()
            heading_stack.append({"text": heading_text, "level": heading_level})

            current_heading       = " > ".join(x["text"] for x in heading_stack if x["text"])
            current_heading_level = heading_level

            blocks.append(Block(
                type          = "heading",
                node          = node,
                signature     = signature(node),
                heading_ctx   = current_heading,
                heading_level = current_heading_level,
                order         = node.order,
                uid           = node.uid,
            ))
            continue

        if skip_until_level is not None:
            continue

        if _is_skip_block(node):
            continue

        if node.type == "shape" and not _shape_has_any_child(node):
            continue

        # ── Promote image-only paragraph ──────────────────────────────────────
        if node.type == "paragraph":
            txt_norm = norm_for_signature(node.content.get("text", "") or "")
            images = [c for c in (node.children or []) if c.type == "image"]
            if not txt_norm and images:
                for img in images:
                    blocks.append(Block(
                        type          = "image",
                        node          = img,
                        signature     = signature(img, heading_ctx=current_heading or ""),
                        heading_ctx   = current_heading,
                        heading_level = current_heading_level,
                        order         = img.order,
                        uid           = img.uid,
                    ))
                continue

        blocks.append(Block(
            type          = node.type,
            node          = node,
            signature     = signature(node, heading_ctx=current_heading or ""),
            heading_ctx   = current_heading,
            heading_level = current_heading_level,
            order         = node.order,
            uid           = node.uid,
        ))

    # ── Sort + fix shape heading ───────────────────────────────────────────────
    sorted_blocks = sorted(
        blocks,
        key=lambda x: (x.order if x.order is not None else 0, x.uid or ""),
    )
    sorted_blocks = _fix_shape_headings(sorted_blocks)
    sorted_blocks = remove_heuristic_toc(sorted_blocks)

    return merge_consecutive_tables(sorted_blocks)