"""
models/docnode.py
~~~~~~~~~~~~~~~~~
Node cơ bản trong cây tài liệu.

Vai trò:
    - Lưu cây tài liệu raw sau khi extract từ Word.
    - Không chứa logic diff.
    - Không flatten table.
    - Cho phép nested table nằm trong cell.
    - Cho phép shape nằm trong document/cell.

Cây chuẩn:
    document
    ├── heading
    ├── paragraph
    │   └── image
    ├── image
    ├── table
    │   └── row
    │       └── cell
    │           ├── paragraph
    │           ├── image
    │           ├── shape
    │           └── table      ← nested table
    └── shape
        ├── paragraph
        ├── image
        └── table

Quan trọng:
    - find() là DFS toàn cây.
    - find_without_nested_tables() dùng khi xử lý parent table,
      để tránh lấy row/cell của nested table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional

if TYPE_CHECKING:
    from src.services.models.logical_table import LogicalTable


NodeType = str


VALID_CHILDREN: Dict[str, set[str]] = {
    "document":  {"heading", "paragraph", "image", "table", "shape"},
    "table":     {"row"},
    "row":       {"cell"},
    "cell":      {"paragraph", "image", "table", "shape"},
    "shape":     {"image", "paragraph", "table"},
    "paragraph": {"image"},
    "heading":   {"image"},
    "image":     set(),
}


@dataclass
class DocNode:
    type: NodeType
    content: Dict[str, Any] = field(default_factory=dict)
    children: List["DocNode"] = field(default_factory=list)

    uid: Optional[str] = None
    parent_uid: Optional[str] = None
    order: int = 0
    path: Optional[str] = None

    # Gắn sau khi extractor/table.py build xong LogicalTable
    logical_table: Optional["LogicalTable"] = field(default=None, repr=False)

    # Runtime parent reference — không serialize, không so sánh
    parent: Optional["DocNode"] = field(default=None, repr=False, compare=False)

    # ── Validation ────────────────────────────────────────────────

    def __post_init__(self) -> None:
        if not self.type:
            raise ValueError("DocNode.type must not be empty")

    def _assert_valid_child(self, child: "DocNode") -> None:
        allowed = VALID_CHILDREN.get(self.type)

        # Type cha chưa khai báo → cho qua để dễ mở rộng
        if allowed is None:
            return

        if child.type not in allowed:
            raise ValueError(
                f"'{child.type}' không được phép là con của '{self.type}'. "
                f"Hợp lệ: {allowed or 'không có child'}"
            )

    # ── Tree manipulation ─────────────────────────────────────────

    def add_child(self, child: "DocNode") -> None:
        if not isinstance(child, DocNode):
            raise TypeError(f"Expected DocNode, got {type(child)}")

        self._assert_valid_child(child)

        child.parent = self
        child.parent_uid = self.uid
        self.children.append(child)

    def is_leaf(self) -> bool:
        return not self.children

    def direct_children(self, node_type: str) -> Iterator["DocNode"]:
        """
        Chỉ lấy child trực tiếp, không DFS.

        Ví dụ:
            table.direct_children("row")
            row.direct_children("cell")
        """
        for child in self.children:
            if child.type == node_type:
                yield child

    # ── Text helpers ──────────────────────────────────────────────

    @property
    def text(self) -> str:
        """
        Text trực tiếp của node.
        Chỉ đáng tin với paragraph/heading.
        Không dùng cho cell/table nếu cần full recursive text.
        """
        return self.content.get("text", "") or ""

    def plain_text(self, include_nested_tables: bool = True) -> str:
        """
        Lấy text recursive từ node.

        include_nested_tables=False:
            Dùng khi tính text/signature của parent table.
            Khi walk đến một cell, bỏ qua child table (nested table)
            để tránh kéo nội dung nested table lên table cha.

        Logic lọc:
            Chỉ skip child.type == "table" khi self là "cell" hoặc "shape".
            Không skip ở các node khác vì:
            - "table" không có direct child "table" (phải qua row → cell)
            - "row" không có direct child "table"
            - Chỉ "cell" và "shape" mới có child "table" trực tiếp
        """
        parts: List[str] = []

        if self.text:
            parts.append(self.text)

        for child in self.children:
            if (
                not include_nested_tables
                and child.type == "table"
                and self.type in {"cell", "shape"}
            ):
                continue

            txt = child.plain_text(include_nested_tables=include_nested_tables)
            if txt:
                parts.append(txt)

        return " ".join(parts).strip()

    # ── Traversal ─────────────────────────────────────────────────

    def walk(self) -> Iterator["DocNode"]:
        """DFS toàn bộ cây, bao gồm nested table."""
        yield self
        for child in self.children:
            yield from child.walk()

    def find(self, node_type: str) -> Iterator["DocNode"]:
        """
        DFS toàn bộ cây.

        Cẩn thận: nếu gọi table.find("cell") sẽ lấy cả cell trong nested table.
        Dùng find_without_nested_tables() nếu chỉ muốn cell của table hiện tại.
        """
        for node in self.walk():
            if node.type == node_type:
                yield node

    def walk_without_nested_tables(self) -> Iterator["DocNode"]:
        """
        DFS nhưng không đi sâu vào nested table.

        Dùng khi đang xử lý một table cha.
        Mục tiêu:
            parent table row/cell không bị trộn với nested table row/cell.

        Vẫn yield nested table node để biết cell có nested table,
        nhưng không đi sâu vào row/cell của nested table đó.
        """
        yield self

        for child in self.children:
            if child.type == "table" and self.type in {"cell", "shape"}:
                # Yield node nested table nhưng không recurse vào bên trong
                yield child
                continue

            yield from child.walk_without_nested_tables()

    def find_without_nested_tables(self, node_type: str) -> Iterator["DocNode"]:
        """
        Tìm node nhưng không đi sâu vào nested table.

        Ví dụ:
            parent_table.find_without_nested_tables("cell")
        → lấy cell của parent table, không lấy cell trong nested table.
        """
        for node in self.walk_without_nested_tables():
            if node.type == node_type:
                yield node

    # ── Common iterators ──────────────────────────────────────────

    def iter_tables(self, include_nested: bool = True) -> Iterator["DocNode"]:
        if include_nested:
            yield from self.find("table")
        else:
            yield from self.find_without_nested_tables("table")

    def iter_images(self, include_nested_tables: bool = True) -> Iterator["DocNode"]:
        if include_nested_tables:
            yield from self.find("image")
        else:
            yield from self.find_without_nested_tables("image")

    def iter_cells(self, include_nested_tables: bool = True) -> Iterator["DocNode"]:
        if include_nested_tables:
            yield from self.find("cell")
        else:
            yield from self.find_without_nested_tables("cell")

    # ── Type helpers ──────────────────────────────────────────────

    def is_document(self) -> bool:
        return self.type == "document"

    def is_table(self) -> bool:
        return self.type == "table"

    def is_row(self) -> bool:
        return self.type == "row"

    def is_cell(self) -> bool:
        return self.type == "cell"

    def is_paragraph(self) -> bool:
        return self.type == "paragraph"

    def is_heading(self) -> bool:
        return self.type == "heading"

    def is_image(self) -> bool:
        return self.type == "image"

    def is_shape(self) -> bool:
        return self.type == "shape"

    # ── Equality / Hash ───────────────────────────────────────────

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DocNode):
            return NotImplemented

        if self.uid and other.uid:
            return self.uid == other.uid

        return self is other

    def __hash__(self) -> int:
        return hash(self.uid) if self.uid else id(self)

    # ── Serialization ─────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "uid":        self.uid,
            "parent_uid": self.parent_uid,
            "order":      self.order,
            "path":       self.path,
            "type":       self.type,
            "content":    self.content,
            "children":   [child.to_dict() for child in self.children],
        }

        if self.logical_table is not None:
            data["logical_table"] = self.logical_table.to_dict()

        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DocNode":
        node = cls(
            type=data["type"],
            content=data.get("content", {}),
            uid=data.get("uid"),
            parent_uid=data.get("parent_uid"),
            order=data.get("order", 0),
            path=data.get("path"),
        )

        for child_data in data.get("children", []):
            child = cls.from_dict(child_data)
            node.add_child(child)

        # Không restore logical_table ở đây để tránh circular dependency.
        # Rebuild lại ở extractor/table hoặc json layer nếu cần.

        return node

    # ── Debug ─────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return (
            "DocNode("
            f"type={self.type!r}, "
            f"uid={self.uid!r}, "
            f"order={self.order}, "
            f"children={len(self.children)}, "
            f"logical_table={self.logical_table is not None}"
            ")"
        )