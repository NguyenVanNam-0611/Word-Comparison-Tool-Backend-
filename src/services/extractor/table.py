"""
extractor/table.py
~~~~~~~~~~~~~~~~~~
Extract table từ docx thành LogicalTable.

Chỉ lưu master cells — bỏ hoàn toàn continuation cell (vMerge/hMerge).
Không tạo row/cell DocNode nữa.

Public API:
    extract_logical_table(tbl, uid, parent_uid, order_ref) → LogicalTable

Vẫn giữ extract_table() như wrapper để tương thích với block_builder
— trả DocNode(type="table") với logical_table được inject vào.

Thay đổi so với version cũ:
    - _extract_cell_content trả List[CellContentBlock] thay vì tuple
      (paragraphs, images, nested_table) riêng rẽ.
    - extract_logical_table fill cell.content_blocks trực tiếp,
      không gán cell.paragraphs / cell.images / cell.nested_table
      (các field đó là @property trên model mới, không thể gán).
    - Nested table được wrap vào CellContentBlock(type="table") theo
      đúng thứ tự xuất hiện trong XML, không còn bị tách ra khỏi
      content_blocks.
    - text / text_display vẫn được derive từ paragraph text để giữ
      backward compat với các nơi dùng cell.text.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

from docx.oxml.ns import qn
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph

from src.services.models.docnode import DocNode
from src.services.models.logical_table import (
    CellContentBlock,
    ImagePayload,
    LogicalCell,
    LogicalTable,
    ParagraphPayload,
    ShapePayload,
)
from src.services.extractor.paragraph import extract_paragraph_nodes
from src.services.extractor.utils import OrderRef, next_order, norm_text, raw_text
from src.services.extractor.numbering import CounterState

BlockItem = Union[Paragraph, Table]


# ══════════════════════════════════════════════════════════════════════════════
# XML helpers
# ══════════════════════════════════════════════════════════════════════════════


def _iter_cell_items(cell: _Cell) -> Iterator[BlockItem]:
    for child in cell._tc.iterchildren():
        if isinstance(child, CT_P):
            par = Paragraph(child, cell)
            has_text = bool(norm_text(par.text))
            has_image = any(
                run._r is not None
                and (run._r.find(qn("w:drawing")) is not None or run._r.find(qn("w:pict")) is not None)
                for run in par.runs
            )
            if has_text or has_image:
                yield par
        elif isinstance(child, CT_Tbl):
            yield Table(child, cell)


def _get_xml_rows(tbl: Table) -> List[List]:
    TR = qn("w:tr")
    TC = qn("w:tc")
    return [list(tr.iterchildren(TC)) for tr in tbl._tbl.iterchildren(TR)]


def _tc_col_span(tc) -> int:
    tcPr = tc.find(qn("w:tcPr"))
    if tcPr is None:
        return 1
    gs = tcPr.find(qn("w:gridSpan"))
    return int(gs.get(qn("w:val"), 1)) if gs is not None else 1


def _count_cols_from_xml(xml_rows: List[List]) -> int:
    if not xml_rows:
        return 0
    return max(sum(_tc_col_span(tc) for tc in row) for row in xml_rows)


def _get_cell_merge_info(cell: _Cell) -> Tuple[int, bool, bool]:
    tc = cell._tc
    tcPr = tc.find(qn("w:tcPr"))
    if tcPr is None:
        return 1, False, False

    gs = tcPr.find(qn("w:gridSpan"))
    col_span = int(gs.get(qn("w:val"), 1)) if gs is not None else 1

    vMerge = tcPr.find(qn("w:vMerge"))
    if vMerge is None:
        return col_span, False, False

    val = vMerge.get(qn("w:val"), "")
    if val == "restart":
        return col_span, True, False
    return col_span, False, True


def _count_row_span(xml_rows: List[List], row_idx: int, grid_col: int) -> int:
    span = 1
    for r in range(row_idx + 1, len(xml_rows)):
        pos = 0
        found_tc = None
        for tc in xml_rows[r]:
            tc_width = _tc_col_span(tc)
            if pos <= grid_col < pos + tc_width:
                found_tc = tc
                break
            pos += tc_width

        if found_tc is None:
            break

        tcPr = found_tc.find(qn("w:tcPr"))
        vMerge = tcPr.find(qn("w:vMerge")) if tcPr is not None else None

        if vMerge is None:
            break
        if vMerge.get(qn("w:val"), "") == "restart":
            break

        span += 1
    return span


# ══════════════════════════════════════════════════════════════════════════════
# Cell content extractors → CellContentBlock list
# ══════════════════════════════════════════════════════════════════════════════


def _extract_image_payload(img_node: DocNode) -> ImagePayload:
    c = img_node.content or {}
    w = round(c.get("width_emu", 0) * 96 / 914400) if c.get("width_emu") else None
    h = round(c.get("height_emu", 0) * 96 / 914400) if c.get("height_emu") else None
    return ImagePayload(
        uid=img_node.uid or "",
        image_url=c.get("image_url"),
        sha256=c.get("sha256"),
        width_px=w,
        height_px=h,
        mime=c.get("mime"),
    )


def _extract_cell_blocks(
    cell: _Cell,
    uid_prefix: str,
    order_ref: OrderRef,
    numbering_map: Optional[Dict[str, Any]] = None,
    counter_state: Optional[CounterState] = None,
    page: int = 1,
) -> Tuple[List[CellContentBlock], str, str]:
    """
    Extract nội dung cell → trả về:
        (content_blocks, text, text_display)

    content_blocks : ordered list CellContentBlock theo thứ tự XML.
                     Mỗi block là paragraph / image standalone / shape / table.
    text           : normalized, join bằng " " — dùng để diff/signature nhanh.
    text_display   : raw, join bằng "\\n" — dùng để hiển thị.

    Thứ tự block phản ánh đúng thứ tự nội dung trong Word cell:
        paragraph → image → paragraph → nested_table → ...

    Image standalone (paragraph chỉ có ảnh không có text) được emit
    thành CellContentBlock(type="image") để signature không bỏ sót.
    Paragraph có cả text lẫn ảnh inline → một CellContentBlock(type="paragraph")
    với images được lưu trong ParagraphPayload.images.
    """
    blocks: List[CellContentBlock] = []
    text_parts: List[str] = []
    display_parts: List[str] = []
    n = 0

    for item in _iter_cell_items(cell):
        n += 1

        # ── Nested table ──────────────────────────────────────────
        if isinstance(item, Table):
            nested = extract_logical_table(
                item,
                uid=f"{uid_prefix}.t{n}",
                parent_uid=uid_prefix,
                order_ref=order_ref,
                numbering_map=numbering_map,
                counter_state=counter_state,
                page=page,
            )
            blocks.append(CellContentBlock(type="table", payload=nested))
            continue

        # ── Paragraph ─────────────────────────────────────────────
        assert isinstance(item, Paragraph)

        nodes = list(
            extract_paragraph_nodes(
                item,
                uid=f"{uid_prefix}.p{n}",
                parent_uid=uid_prefix,
                order_ref=order_ref,
                in_toc=False,
                numbering_map=numbering_map,
                counter_state=counter_state,
                page=page,
            )
        )

        # Tách text/heading nodes và shape nodes.
        # extract_paragraph_nodes() có thể trả:
        #   [paragraph]
        #   [heading]
        #   [paragraph, shape...]  — shape floating
        text_nodes = [nd for nd in nodes if nd.type in ("paragraph", "heading")]
        shape_nodes = [nd for nd in nodes if nd.type == "shape"]

        # Emit text/heading node thành CellContentBlock paragraph.
        # Shift+Enter được giữ trong text_display bằng "\n",
        # không tách thành nhiều node.
        for t_node in text_nodes:
            t = t_node.content.get("text") or ""
            d = t_node.content.get("text_display") or ""

            para_images: List[ImagePayload] = [
                _extract_image_payload(img_child) for img_child in (t_node.children or []) if img_child.type == "image"
            ]

            if t:
                text_parts.append(t)
                display_parts.append(d)
                blocks.append(
                    CellContentBlock(
                        type="paragraph",
                        payload=ParagraphPayload(
                            uid=t_node.uid or f"{uid_prefix}.p{n}",
                            text=t,
                            text_display=d,
                            images=para_images,
                        ),
                    )
                )
            elif para_images:
                # Paragraph chỉ có ảnh — emit từng image như standalone block
                for img_payload in para_images:
                    blocks.append(CellContentBlock(type="image", payload=img_payload))

        # Shape trong cell emit thành CellContentBlock(type="shape")
        # giữ đúng thứ tự xuất hiện sau paragraph
        for shp_node in shape_nodes:
            shp_images: List[ImagePayload] = [
                _extract_image_payload(img_child)
                for img_child in (shp_node.children or [])
                if img_child.type == "image"
            ]
            blocks.append(
                CellContentBlock(
                    type="shape",
                    payload=ShapePayload(
                        uid=shp_node.uid or f"{uid_prefix}.shp{n}",
                        text=shp_node.content.get("text") or "",
                        text_display=shp_node.content.get("text_display") or "",
                        images=shp_images,
                    ),
                )
            )

    text = " ".join(text_parts)
    text_display = "\n".join(display_parts)

    return blocks, text, text_display


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════


def extract_logical_table(
    tbl: Table,
    uid: Optional[str] = None,
    parent_uid: Optional[str] = None,
    order_ref: Optional[OrderRef] = None,
    numbering_map: Optional[Dict[str, Any]] = None,
    counter_state: Optional[CounterState] = None,
    page: int = 1,
) -> LogicalTable:
    """
    Extract docx Table → LogicalTable.

    Chỉ lưu master cells, bỏ hoàn toàn continuation cell.
    Mỗi cell chỉ có content_blocks — không gán paragraphs/images/nested_table
    trực tiếp (các field đó là @property trên LogicalCell).

    counter_state truyền từ document level để đảm bảo numbering label
    trong list item bên trong table được tính đúng thứ tự.
    """
    if order_ref is None:
        order_ref = {"value": 0}

    xml_rows = _get_xml_rows(tbl)
    n_rows = len(xml_rows)
    n_cols = _count_cols_from_xml(xml_rows)

    cells: List[LogicalCell] = []
    skip_map: Dict[Tuple[int, int], bool] = {}

    for r_idx, xml_tcs in enumerate(xml_rows):
        logical_col = 0

        for tc in xml_tcs:
            # Bỏ qua continuation do skip_map (rowspan từ row trên)
            while skip_map.get((r_idx, logical_col)):
                logical_col += 1

            cell = _Cell(tc, tbl)
            col_span, is_restart, is_continue = _get_cell_merge_info(cell)

            # Bỏ qua vMerge continuation
            if is_continue:
                continue

            row_span = 1
            if is_restart:
                row_span = _count_row_span(xml_rows, r_idx, logical_col)
                for dr in range(1, row_span):
                    for dc in range(col_span):
                        skip_map[(r_idx + dr, logical_col + dc)] = True

            cell_uid = f"{uid}.r{r_idx}c{logical_col}" if uid else f"r{r_idx}c{logical_col}"

            content_blocks, text, text_display = _extract_cell_blocks(
                cell=cell,
                uid_prefix=cell_uid,
                order_ref=order_ref,
                numbering_map=numbering_map,
                counter_state=counter_state,
                page=page,
            )

            cells.append(
                LogicalCell(
                    uid=cell_uid,
                    anchor_row=r_idx,
                    anchor_col=logical_col,
                    row_span=row_span,
                    col_span=col_span,
                    text=text,
                    text_display=text_display,
                    content_blocks=content_blocks,
                )
            )

            logical_col += col_span

    return LogicalTable(
        uid=uid or "table",
        total_rows=n_rows,
        total_cols=n_cols,
        cells=cells,
    )


def extract_table(
    tbl: Table,
    uid: Optional[str] = None,
    parent_uid: Optional[str] = None,
    order_ref: Optional[OrderRef] = None,
    numbering_map: Optional[Dict[str, Any]] = None,
    counter_state: Optional[CounterState] = None,
    page: int = 1,
    table_idx: int = 0,
) -> DocNode:
    """
    Wrapper tương thích với block_builder hiện tại.

    Trả DocNode(type="table") với logical_table được inject.
    DocNode không có row/cell children — chỉ dùng logical_table.
    """
    if order_ref is None:
        order_ref = {"value": 0}

    logical = extract_logical_table(
        tbl,
        uid=uid,
        parent_uid=parent_uid,
        order_ref=order_ref,
        numbering_map=numbering_map,
        counter_state=counter_state,
        page=page,
    )

    # text summary cho DocNode.content — dùng logical.text_content()
    # để nhất quán với cách LogicalTable tổng hợp text từ content_blocks
    table_text = "\n".join(" | ".join(c.text for c in logical.cells_in_row(r)) for r in logical.anchor_rows())
    all_texts = [c.text for c in logical.cells if c.text]

    table_node = DocNode(
        type="table",
        uid=uid,
        parent_uid=parent_uid,
        order=next_order(order_ref),
        path=(uid or "").replace(".", "/"),
        content={
            "row_count": logical.total_rows,
            "col_count": logical.total_cols,
            "text": table_text,
            "text_set": " ".join(sorted(set(all_texts))),
            "page": page,
            "table_idx": table_idx,
        },
    )

    table_node.logical_table = logical

    return table_node
