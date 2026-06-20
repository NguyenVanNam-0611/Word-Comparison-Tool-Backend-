"""
extractor/document.py
~~~~~~~~~~~~~~~~~~~~~
Entry point: đọc file .docx và chuyển thành cây DocNode.

Chỉ chịu trách nhiệm:
    - Duyệt block item ở document level (paragraph, table)
    - Track trạng thái TOC field
    - Gọi paragraph.py và table.py để extract từng loại
    - Trả về root DocNode(type="document")

KHÔNG chứa logic extract chi tiết — xem paragraph.py và table.py.
Import từ: utils.py, paragraph.py, table.py

Iterator _iter_block_items nằm ở đây vì chỉ document.py dùng.

Cây trả về:
    document
    ├── heading
    │   └── image       (inline trong heading)
    ├── paragraph
    │   └── image       (inline)
    ├── table
    │   └── row
    │       └── cell
    │           ├── paragraph
    │           │   └── image
    │           └── table  (nested)
    └── shape
"""

from __future__ import annotations

import re
from typing import Iterator, Union

from docx import Document
from docx.document import Document as _Document
from docx.oxml.ns import qn
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph
from src.services.models.docnode import DocNode
from src.services.extractor.table import extract_table
from src.services.extractor.utils import OrderRef
from src.services.extractor.numbering import build_numbering_map, CounterState
from src.services.extractor.paragraph import extract_paragraph_nodes, extract_page_number
from src.services.word_converter import convert_doc_to_docx_if_needed

# ── Types ─────────────────────────────────────────────────────────────────────

BlockItem = Union[Paragraph, Table]


# ══════════════════════════════════════════════════════════════════════════════
# Document iterator
# ══════════════════════════════════════════════════════════════════════════════


def _iter_block_items(doc: _Document) -> Iterator[BlockItem]:
    """
    Duyệt các phần tử con trực tiếp của document body.
    Yield Paragraph hoặc Table theo đúng thứ tự XML.

    Không lọc — giữ nguyên toàn bộ kể cả paragraph rỗng
    (TOC tracker cần xử lý mọi paragraph).
    """
    for child in doc.element.body.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, doc)
        elif isinstance(child, CT_Tbl):
            yield Table(child, doc)


# ══════════════════════════════════════════════════════════════════════════════
# TOC field tracker
# ══════════════════════════════════════════════════════════════════════════════


class _TocFieldTracker:
    """
    Track trạng thái field TOC khi duyệt qua từng paragraph.

    Word dùng 3 loại w:fldChar để đánh dấu field:
        fldCharType="begin"    → bắt đầu field
        fldCharType="separate" → hết phần instruction, bắt đầu content
        fldCharType="end"      → kết thúc field
    và w:instrText chứa lệnh (vd: " TOC \\o \"1-3\" \\h \\z \\u ").

    Các trường hợp cover:
    1. TOC đơn giản : begin → instrText(TOC) → end trong 1 paragraph
    2. TOC phức tạp : begin/instrText/end trải qua nhiều paragraph
    3. Nested field : dùng depth counter thay vì bool flag
    4. TOC style    : paragraph dùng style "TOC 1", "TOC 2"...
    """

    def __init__(self) -> None:
        self._depth: int = 0
        self._toc_depth: int = 0
        self._inside_toc: bool = False
        self._pending_check: bool = False

    @property
    def inside_toc(self) -> bool:
        return self._inside_toc

    def process_paragraph(self, par: Paragraph) -> bool:
        """
        Cập nhật state dựa trên paragraph hiện tại.
        Trả True nếu paragraph này nằm trong TOC field.
        """
        for elem in par._p.iter():
            tag = elem.tag

            if tag == qn("w:fldChar"):
                fld_type = elem.get(qn("w:fldCharType"), "")

                if fld_type == "begin":
                    self._depth += 1
                    self._pending_check = True

                elif fld_type == "end":
                    if self._depth > 0:
                        self._depth -= 1
                    if self._inside_toc and self._depth < self._toc_depth:
                        self._inside_toc = False
                        self._toc_depth = 0
                    self._pending_check = False

                elif fld_type == "separate":
                    self._pending_check = False

            elif tag == qn("w:instrText"):
                instr = (elem.text or "").strip().upper()
                if self._pending_check and "TOC" in instr:
                    self._inside_toc = True
                    self._toc_depth = self._depth
                self._pending_check = False

        return self._inside_toc

    @staticmethod
    def is_toc_style(par: Paragraph) -> bool:
        """Phát hiện TOC qua style name: 'TOC 1', 'TOC 2', ..."""
        style_name = (par.style.name if par.style else "") or ""
        return bool(re.match(r"toc\s*\d+", style_name.strip(), re.IGNORECASE))


def _is_toc_paragraph(par: Paragraph, tracker: _TocFieldTracker) -> bool:
    """True nếu paragraph nằm trong TOC field hoặc dùng TOC style."""
    return tracker.process_paragraph(par) or _TocFieldTracker.is_toc_style(par)


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════


def extract_doc_tree(docx_path: str) -> DocNode:
    docx_path = convert_doc_to_docx_if_needed(docx_path)
    doc = Document(docx_path)
    numbering_map = build_numbering_map(doc)
    counter_state: CounterState = {}  # track counter toàn document, dùng chung cho cả paragraph lẫn table
    order_ref: OrderRef = {"value": 0}
    toc_tracker = _TocFieldTracker()
    current_page = 1

    root = DocNode(
        type="document",
        uid="document",
        parent_uid=None,
        order=0,
        path="document",
        content={"source": docx_path},
    )

    para_idx = 0
    table_idx = 0

    for i, item in enumerate(_iter_block_items(doc), start=1):
        uid = f"n{i}"

        if isinstance(item, Paragraph):
            para_idx += 1

            # ── TOC tracking ───────────────────────────
            in_toc = _is_toc_paragraph(item, toc_tracker)

            # ── Detect page của paragraph ─────────────
            paragraph_page, next_page = extract_page_number(
                item,
                current_page,
            )

            # ── Extract paragraph nodes ───────────────
            nodes = extract_paragraph_nodes(
                item,
                uid=uid,
                parent_uid="document",
                order_ref=order_ref,
                in_toc=in_toc,
                numbering_map=numbering_map,
                counter_state=counter_state,
                page=paragraph_page,
                para_idx=para_idx,
            )

            for node in nodes:
                root.add_child(node)

            # update page cho block kế tiếp
            current_page = next_page

        else:
            table_idx += 1

            # table dùng page hiện tại
            root.add_child(
                extract_table(
                    item,
                    uid=uid,
                    parent_uid="document",
                    order_ref=order_ref,
                    numbering_map=numbering_map,
                    counter_state=counter_state,
                    page=current_page,
                    table_idx=table_idx,
                )
            )

    return root
