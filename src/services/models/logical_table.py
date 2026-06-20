"""
models/logical_table.py
~~~~~~~~~~~~~~~~~~~~~~~
Data model cho table theo logical/master-cell approach.

Mục tiêu:
    - Chỉ lưu master cells, không lưu continuation cell của merge.
    - Giữ đúng thứ tự nội dung trong cell bằng content_blocks (source of truth).
    - Nested table luôn nằm bên trong cell, không bị bung thành row của table cha.
    - Dùng được cho diff theo logical rows và render frontend.

Thay đổi so với version cũ:
    - content_blocks là source of truth duy nhất cho nội dung cell.
    - paragraphs / images / shapes / nested_table / nested_tables là @property
      computed từ content_blocks — backward compat, không lưu trong dataclass.
    - content_key() bỏ double-emit self.text.
    - to_dict() chỉ emit content_blocks, không còn trùng dữ liệu.
    - is_empty() đơn giản lại.
    - CellContentBlock có as_* helpers để diff logic không cần cast.
    - compact_signature() dùng \x00 thay "#" tránh collision với text.
    - LogicalRow không tạo instance thừa khi build logical_rows().

Dùng bởi:
    - extractor/table.py
    - diff/table/table_diff.py
    - diff/table/table_serialize.py
    - serializer/json_builder.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ═════════════════════════════════════════════════════════════════════
# Payload models
# ═════════════════════════════════════════════════════════════════════

@dataclass
class ImagePayload:
    uid: str
    image_url: Optional[str] = None
    sha256: Optional[str] = None
    width_px: Optional[int] = None
    height_px: Optional[int] = None
    mime: Optional[str] = None

    def signature(self) -> str:
        if self.sha256:
            return f"IMG:{self.sha256[:16]}"
        if self.image_url:
            return f"IMG_URL:{self.image_url}"
        return f"IMG:{self.uid}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "uid": self.uid,
            "image_url": self.image_url,
            "sha256": self.sha256,
            "width_px": self.width_px,
            "height_px": self.height_px,
            "mime": self.mime,
        }


@dataclass
class ParagraphPayload:
    uid: str
    text: str = ""           # normalized để diff/signature
    text_display: str = ""   # raw để render
    images: List[ImagePayload] = field(default_factory=list)

    def signature(self) -> str:
        parts = []
        if self.text:
            parts.append(f"TXT:{self.text[:120]}")
        for img in self.images:
            parts.append(img.signature())
        return "|".join(parts) or f"P_EMPTY:{self.uid}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "uid": self.uid,
            "text": self.text,
            "text_display": self.text_display,
            "images": [img.to_dict() for img in self.images],
        }


@dataclass
class ShapePayload:
    uid: str
    text: str = ""
    text_display: str = ""
    images: List[ImagePayload] = field(default_factory=list)

    def signature(self) -> str:
        parts = [f"SHP:{self.text[:80]}"] if self.text else [f"SHP:{self.uid}"]
        for img in self.images:
            parts.append(img.signature())
        return "|".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "uid": self.uid,
            "text": self.text,
            "text_display": self.text_display,
            "images": [img.to_dict() for img in self.images],
        }


@dataclass
class CellContentBlock:
    """
    Một block nội dung trong cell, giữ đúng thứ tự xuất hiện trong Word.

    type:
        - "paragraph"
        - "image"
        - "table"
        - "shape"

    Dùng as_* helpers để truy cập payload có type-safety thay vì cast thủ công.
    """
    type: str
    payload: Any

    # ── Typed access helpers ──────────────────────────────────────

    def as_paragraph(self) -> Optional[ParagraphPayload]:
        return self.payload if self.type == "paragraph" else None

    def as_image(self) -> Optional[ImagePayload]:
        return self.payload if self.type == "image" else None

    def as_shape(self) -> Optional[ShapePayload]:
        return self.payload if self.type == "shape" else None

    def as_table(self) -> Optional["LogicalTable"]:
        return self.payload if self.type == "table" else None

    # ── Signature ─────────────────────────────────────────────────

    def signature(self) -> str:
        if self.type == "paragraph":
            p = self.as_paragraph()
            return p.signature() if p is not None else "P:NONE"

        if self.type == "image":
            img = self.as_image()
            return img.signature() if img is not None else "IMG:NONE"

        if self.type == "shape":
            shp = self.as_shape()
            return shp.signature() if shp is not None else "SHP:NONE"

        if self.type == "table":
            tbl = self.as_table()
            return f"TBL:{tbl.compact_signature()}" if tbl is not None else "TBL:NONE"

        return f"{self.type.upper()}:UNKNOWN"

    # ── Serialize ─────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        payload = self.payload

        if hasattr(payload, "to_dict"):
            payload_dict = payload.to_dict()
        elif isinstance(payload, dict):
            payload_dict = payload
        else:
            payload_dict = {"value": str(payload)}

        return {
            "type": self.type,
            "payload": payload_dict,
        }


# ═════════════════════════════════════════════════════════════════════
# Logical cell / row / table
# ═════════════════════════════════════════════════════════════════════

@dataclass
class LogicalCell:
    uid: str
    anchor_row: int
    anchor_col: int
    row_span: int = 1
    col_span: int = 1

    # Text tổng hợp để diff nhanh / quick lookup
    # Không dùng trong content_key() để tránh double-emit
    text: str = ""
    text_display: str = ""

    # Source of truth duy nhất cho nội dung cell
    content_blocks: List[CellContentBlock] = field(default_factory=list)

    # ── Computed properties — backward compat ────────────────────
    # Không lưu trong dataclass, computed từ content_blocks.
    # Code cũ dùng cell.paragraphs / cell.images / cell.nested_table
    # vẫn hoạt động mà không cần sửa ngay.

    @property
    def paragraphs(self) -> List[ParagraphPayload]:
        return [
            b.payload for b in self.content_blocks
            if b.type == "paragraph" and b.payload is not None
        ]

    @property
    def images(self) -> List[ImagePayload]:
        return [
            b.payload for b in self.content_blocks
            if b.type == "image" and b.payload is not None
        ]

    @property
    def shapes(self) -> List[ShapePayload]:
        return [
            b.payload for b in self.content_blocks
            if b.type == "shape" and b.payload is not None
        ]

    @property
    def nested_tables(self) -> List["LogicalTable"]:
        return [
            b.payload for b in self.content_blocks
            if b.type == "table" and b.payload is not None
        ]

    @property
    def nested_table(self) -> Optional["LogicalTable"]:
        """Backward compat: trả về table đầu tiên trong cell."""
        tables = self.nested_tables
        return tables[0] if tables else None

    # ── Helpers ───────────────────────────────────────────────────

    def is_empty(self) -> bool:
        return not self.text and not self.content_blocks

    def spans_row(self, row: int) -> bool:
        return self.anchor_row <= row < self.anchor_row + self.row_span

    def spans_col(self, col: int) -> bool:
        return self.anchor_col <= col < self.anchor_col + self.col_span

    def covers(self, row: int, col: int) -> bool:
        return self.spans_row(row) and self.spans_col(col)

    def is_anchor_at(self, row: int, col: int) -> bool:
        return self.anchor_row == row and self.anchor_col == col

    def anchor_key(self) -> str:
        return f"r{self.anchor_row}c{self.anchor_col}"

    def content_key(self, max_chars: int = 120) -> str:
        """
        Key nhận dạng cell cho similarity matching.

        Chỉ dùng content_blocks làm source, không emit self.text
        để tránh double-emit khi text được derive từ paragraph content.

        Cell có thể chỉ có image / nested table / shape / empty merged cell —
        signature vẫn đúng vì walk qua từng block.
        """
        parts = [
            block.signature()
            for block in self.content_blocks
        ]

        if parts:
            return "|".join(parts)

        return f"EMPTY:{self.anchor_row}:{self.anchor_col}:{self.row_span}x{self.col_span}"

    def blocks_by_type(self, block_type: str) -> List[CellContentBlock]:
        """Trả danh sách block theo type, giữ thứ tự xuất hiện."""
        return [b for b in self.content_blocks if b.type == block_type]

    # ── Serialize ─────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        return {
            "uid": self.uid,
            "anchor_row": self.anchor_row,
            "anchor_col": self.anchor_col,
            "row_span": self.row_span,
            "col_span": self.col_span,
            "text": self.text,
            "text_display": self.text_display,
            "content_blocks": [b.to_dict() for b in self.content_blocks],
        }


@dataclass
class LogicalRow:
    """
    Một logical row theo index hàng của table cha.

    cells gồm:
        - anchor cells bắt đầu ở row này
        - spanning cells từ row trên kéo xuống

    Khi tạo signature để match row, chỉ dùng anchor cells để
    tránh lặp nội dung rowspan.
    """
    physical_row: int
    cells: List[LogicalCell] = field(default_factory=list)

    def anchor_cells(self) -> List[LogicalCell]:
        return [
            c for c in self.cells
            if c.anchor_row == self.physical_row
        ]

    def spanning_cells(self) -> List[LogicalCell]:
        return [
            c for c in self.cells
            if c.anchor_row < self.physical_row
        ]

    def is_anchor_row(self) -> bool:
        return bool(self.anchor_cells())

    def signature(self, max_chars: int = 120) -> str:
        anchors = sorted(self.anchor_cells(), key=lambda c: c.anchor_col)

        parts = [cell.content_key(max_chars=max_chars) for cell in anchors]

        if parts:
            return "|".join(parts)

        # Row chỉ toàn spanning cell
        span_parts = [
            f"SPAN:{c.anchor_key()}:{c.row_span}x{c.col_span}"
            for c in self.spanning_cells()
        ]

        return "|".join(span_parts) or f"ROW_EMPTY:{self.physical_row}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "physical_row": self.physical_row,
            "cells": [c.to_dict() for c in self.cells],
            "anchor_cells": [c.uid for c in self.anchor_cells()],
            "spanning_cells": [c.uid for c in self.spanning_cells()],
            "signature": self.signature(),
        }


@dataclass
class LogicalTable:
    uid: str
    total_rows: int
    total_cols: int
    cells: List[LogicalCell] = field(default_factory=list)

    # ── Lookup helpers ────────────────────────────────────────────

    def get(self, row: int, col: int) -> Optional[LogicalCell]:
        """
        Lấy cell visible tại vị trí row/col.
        Nếu vị trí nằm trong merged area, trả về master cell.
        """
        for cell in self.cells:
            if cell.covers(row, col):
                return cell
        return None

    def get_master(self, row: int, col: int) -> Optional[LogicalCell]:
        """Chỉ trả về cell nếu row/col đúng là anchor."""
        for cell in self.cells:
            if cell.is_anchor_at(row, col):
                return cell
        return None

    def cells_in_row(self, row: int) -> List[LogicalCell]:
        """Chỉ master cells có anchor_row == row, sorted theo col."""
        return sorted(
            [c for c in self.cells if c.anchor_row == row],
            key=lambda c: c.anchor_col,
        )

    def logical_row_cells(self, row: int) -> List[LogicalCell]:
        """Cells visible tại row này, gồm cả spanning cells, sorted theo col."""
        return sorted(
            [c for c in self.cells if c.spans_row(row)],
            key=lambda c: c.anchor_col,
        )

    def anchor_rows(self) -> List[int]:
        return sorted({c.anchor_row for c in self.cells})

    # ── Logical rows ──────────────────────────────────────────────

    def logical_rows(self) -> List[LogicalRow]:
        """
        Build danh sách LogicalRow cho toàn bộ table.

        Mỗi row được build một lần, không tạo instance thừa.
        Chỉ thêm row nếu có cells (tránh row rỗng do total_rows > actual cells).
        """
        result: List[LogicalRow] = []

        for row_idx in range(self.total_rows):
            cells = self.logical_row_cells(row_idx)
            if cells:
                result.append(
                    LogicalRow(
                        physical_row=row_idx,
                        cells=cells,
                    )
                )

        return result

    def row_signature(self, row: int, max_chars: int = 120) -> str:
        """
        Signature cho một row cụ thể.
        Dùng lại logical_row_cells thay vì tạo LogicalRow riêng.
        """
        cells = self.logical_row_cells(row)
        return LogicalRow(physical_row=row, cells=cells).signature(max_chars=max_chars)

    def signature_list(self, max_chars: int = 120) -> List[str]:
        return [
            row.signature(max_chars=max_chars)
            for row in self.logical_rows()
        ]

    def compact_signature(self, max_rows: int = 12, max_chars: int = 80) -> str:
        """
        Signature ngắn cho nested table / table similarity.
        Không dùng để render.

        Dùng \\x00 làm separator giữa các row thay vì "#"
        để tránh collision với text content.
        """
        row_sigs = self.signature_list(max_chars=max_chars)
        body = "\x00".join(row_sigs[:max_rows])
        return f"{self.total_rows}x{self.total_cols}:{body}"

    # ── Content summary helpers ───────────────────────────────────

    def text_content(self) -> str:
        """
        Text toàn bộ cells nối nhau.
        Ưu tiên lấy từ content_blocks để nhất quán với content_key().
        """
        parts: List[str] = []

        for cell in self.cells:
            if cell.content_blocks:
                for block in cell.content_blocks:
                    if block.type == "paragraph":
                        p = block.as_paragraph()
                        if p and p.text:
                            parts.append(p.text)
                    elif block.type == "shape":
                        shp = block.as_shape()
                        if shp and shp.text:
                            parts.append(shp.text)
            elif cell.text:
                # fallback nếu extractor chưa fill content_blocks
                parts.append(cell.text)

        return " ".join(parts).strip()

    def has_nested_table(self) -> bool:
        return any(
            b.type == "table"
            for cell in self.cells
            for b in cell.content_blocks
        )

    def has_images(self) -> bool:
        return any(
            b.type == "image"
            for cell in self.cells
            for b in cell.content_blocks
        )

    def has_shapes(self) -> bool:
        return any(
            b.type == "shape"
            for cell in self.cells
            for b in cell.content_blocks
        )

    # ── Serialize ─────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        rows = self.logical_rows()
        return {
            "uid": self.uid,
            "total_rows": self.total_rows,
            "total_cols": self.total_cols,
            "cells": [cell.to_dict() for cell in self.cells],
            # rows được derive từ cells — frontend dùng để render theo thứ tự
            "rows": [row.to_dict() for row in rows],
        }