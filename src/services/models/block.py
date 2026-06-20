"""
models/block.py
~~~~~~~~~~~~~~~
Block là wrapper phẳng của DocNode, dùng cho bước align và diff.

Mỗi Block đại diện cho 1 đơn vị so sánh ở document level:
    - heading
    - paragraph
    - image
    - shape
    - table     ← diff nội dung bên trong do TableDiff xử lý riêng

KHÔNG tạo Block cho: row, cell (chỉ tồn tại bên trong TableDiff)

block_builder.py chịu trách nhiệm duyệt DocNode tree và tạo List[Block].
Block không tự tính signature — signature được inject từ signature.py.

Thay đổi so với version cũ:
    - get_text() cho table: dùng content_blocks làm source chính
      để nhất quán với LogicalCell.content_key().
      Fallback path (khi chưa có LogicalTable) giữ nguyên.
    - preview_text cho table: ưu tiên LogicalTable nếu có.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from src.services.models.docnode import DocNode

BlockType = str

BLOCK_TYPES: frozenset = frozenset({
    "heading",
    "paragraph",
    "image",
    "shape",
    "table",
})


@dataclass
class Block:
    type: BlockType
    node: DocNode

    # inject từ signature.py
    signature: str = ""

    # inject từ block_builder.py
    heading_ctx: Optional[str] = None
    heading_level: int = 0

    # metadata từ node
    uid: Optional[str] = None
    parent_uid: Optional[str] = None
    path: Optional[str] = None
    order: int = 0

    # page number (Word page)
    page: int = 1

    # flags — auto tính từ type
    is_heading: bool = field(default=False, init=False)
    is_paragraph: bool = field(default=False, init=False)
    is_image: bool = field(default=False, init=False)
    is_shape: bool = field(default=False, init=False)
    is_table: bool = field(default=False, init=False)

    # preview text cho debug / log
    preview_text: str = field(default="", init=False)

    # ── Init ──────────────────────────────────────────────────────

    def __post_init__(self) -> None:
        if not isinstance(self.node, DocNode):
            raise TypeError(
                f"Block.node must be DocNode, got {type(self.node)}"
            )

        if self.type not in BLOCK_TYPES:
            raise ValueError(
                f"Block.type {self.type!r} không hợp lệ. "
                f"Hợp lệ: {sorted(BLOCK_TYPES)}"
            )

        # ── Fill metadata từ node ──────────────────────────────────
        self.uid = self.uid or self.node.uid
        self.parent_uid = self.parent_uid or self.node.parent_uid
        self.path = self.path or self.node.path

        if self.order == 0 and self.node.order:
            self.order = self.node.order

        # ── Page number ───────────────────────────────────────────
        raw_page = self.node.content.get("page")

        try:
            self.page = int(raw_page) if raw_page is not None else 1
        except (TypeError, ValueError):
            self.page = 1

        # ── Flags ─────────────────────────────────────────────────
        self.is_heading = self.type == "heading"
        self.is_paragraph = self.type == "paragraph"
        self.is_image = self.type == "image"
        self.is_shape = self.type == "shape"
        self.is_table = self.type == "table"

        # ── Preview ───────────────────────────────────────────────
        self.preview_text = self._build_preview()

    # ── Preview text ──────────────────────────────────────────────

    def _build_preview(self) -> str:
        content = self.node.content

        if self.type in ("heading", "paragraph"):
            text = content.get("text") or ""

        elif self.type == "image":
            alt = content.get("alt_text") or ""
            text = f"[IMAGE: {alt}]" if alt else "[IMAGE]"

        elif self.type == "shape":
            para = next(
                (c for c in self.node.children if c.type == "paragraph"),
                None,
            )
            inner = para.content.get("text") or "" if para else ""
            text = f"[SHAPE: {inner}]" if inner else "[SHAPE]"

        elif self.type == "table":
            lt = self.node.logical_table

            if lt is not None:
                text = f"[TABLE {lt.total_rows}×{lt.total_cols}]"
            else:
                rows = self.node.children
                n_rows = len(rows)
                n_cols = len(rows[0].children) if rows else 0
                text = f"[TABLE {n_rows}×{n_cols}]"

        else:
            text = ""

        return text[:200].strip()

    # ── Helpers ───────────────────────────────────────────────────

    def get_text(self) -> str:
        """
    Text đầy đủ của block.

    Dùng cho preview / search / similarity nhẹ.

    Lưu ý:
        Với table, hàm này chỉ lấy text từ paragraph/shape.
        KHÔNG dùng làm equality signature vì sẽ bỏ qua image,
        nested table và composition trong cell.

        Table equality/signature phải dùng signature.py riêng,
        dựa trên LogicalTable.compact_signature()
        hoặc LogicalCell.content_key().
    """
        if self.type in ("heading", "paragraph"):
            return self.node.content.get("text") or ""

        if self.type == "shape":
            return " ".join(
                c.content.get("text") or ""
                for c in self.node.children
                if c.type == "paragraph"
            )

        if self.type == "table":
            lt = self.node.logical_table

            if lt is not None:
                parts: List[str] = []
                for cell in lt.cells:
                    for block in cell.content_blocks:
                        if block.type == "paragraph":
                            p = block.as_paragraph()
                            if p and p.text:
                                parts.append(p.text)
                        elif block.type == "shape":
                            shp = block.as_shape()
                            if shp and shp.text:
                                parts.append(shp.text)
                    # fallback nếu cell chưa fill content_blocks
                    if not cell.content_blocks and cell.text:
                        parts.append(cell.text)
                return " ".join(parts)

            # Fallback khi chưa có LogicalTable
            return " ".join(
                cell.content.get("text") or ""
                for cell in self.node.find_without_nested_tables("cell")
            )

        return ""

    def get_runs(self) -> List[dict]:
        """Runs của paragraph / heading, dùng cho inline diff."""
        return self.node.content.get("runs") or []

    def has_children(self) -> bool:
        return bool(self.node.children)

    def get_logical_table(self):
        """Trả LogicalTable nếu table đã extract."""
        return self.node.logical_table

    # ── Equality ──────────────────────────────────────────────────

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Block):
            return NotImplemented

        if self.signature and other.signature:
            return self.signature == other.signature

        return self.uid == other.uid

    def __hash__(self) -> int:
        return hash(self.signature or self.uid)

    # ── Debug ─────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return (
            f"Block("
            f"type={self.type!r}, "
            f"uid={self.uid!r}, "
            f"page={self.page}, "
            f"order={self.order}, "
            f"heading_ctx={self.heading_ctx!r}, "
            f"preview={self.preview_text!r}"
            f")"
        )