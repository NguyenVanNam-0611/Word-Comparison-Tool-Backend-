"""
page/page_injector.py
~~~~~~~~~~~~~~~~~~~~~
Inject page thật từ COM vào DocNode tree.

Chiến lược chuẩn:
- heading/paragraph ngoài table : lấy page theo para_idx
- table                         : lấy page_start/page_end theo table_idx
- row/cell/nested table          : không lấy riêng, inherit range từ table cha
- shape                          : lấy page theo shape anchor
- image                          : inherit từ paragraph/table/shape cha

Nếu COM không trả page cho uid nào đó → giữ page fallback cũ.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from src.services.models.docnode import DocNode

logger = logging.getLogger(__name__)


PageInfo = Dict[str, int]
PageMap = Dict[str, PageInfo]


def collect_uid_index_pairs(
    root: DocNode,
) -> List[Tuple[str, str, Any]]:
    """
    Duyệt tree, trả list (uid, obj_type, idx_or_id) cho các node
    cần lấy page thật bằng COM.

    obj_type = "para"  -> idx là int, dùng doc.Paragraphs(idx)
    obj_type = "table" -> idx là int, dùng doc.Tables(idx)
    obj_type = "shape" -> shape_id/name, dùng doc.Shapes map

    Không collect:
    - row
    - cell
    - nested table con
    - image

    Lý do:
    - table chỉ lấy range table cha
    - image inherit page từ parent
    - row/cell gọi COM nhiều dễ chậm/crash và sai với merged table
    """
    pairs: List[Tuple[str, str, Any]] = []

    def _walk(node: DocNode, inside_table: bool = False) -> None:
        t = node.type
        content = node.content or {}

        if t in ("heading", "paragraph"):
            idx = content.get("para_idx")
            if idx and not inside_table:
                pairs.append((node.uid, "para", int(idx)))

        elif t == "table":
            idx = content.get("table_idx")

            # Chỉ lấy COM page cho table top-level.
            # Nested table inherit từ table cha.
            if idx and not inside_table:
                pairs.append((node.uid, "table", int(idx)))

            # Không đi sâu collect page riêng cho paragraph/row/cell trong table.
            # Những node con sẽ inherit page_start/page_end khi inject.
            return

        elif t == "shape":
            # Shape ngoài body lấy page theo paragraph anchor.
            # Không dùng shape_id để map sang COM Shapes.ID/Name nữa vì dễ lệch.
            anchor_para_idx = content.get("anchor_para_idx") or content.get("para_idx")

            if anchor_para_idx and not inside_table:
                pairs.append((node.uid, "para", int(anchor_para_idx)))

        elif t == "image":
            # Image inherit từ parent.
            return

        for child in node.children or []:
            _walk(child, inside_table=inside_table)

    for child in root.children or []:
        _walk(child, inside_table=False)

    return pairs


def inject_pages(
    root: DocNode,
    page_map: PageMap | Dict[str, int],
) -> None:
    """
    Inject page vào node.content.

    Hỗ trợ cả page_map mới:
        uid -> {"page": 2, "page_start": 2, "page_end": 3}

    Và page_map cũ:
        uid -> 2

    In-place.
    """
    if not page_map:
        return

    normalized = _normalize_page_map(page_map)

    def _walk(
        node: DocNode,
        inherited_page: int | None = None,
        inherited_start: int | None = None,
        inherited_end: int | None = None,
        inside_table: bool = False,
    ) -> None:
        content = node.content or {}
        node.content = content

        info = normalized.get(node.uid)

        # 1. Node có page thật từ COM
        if info:
            _apply_page_info(node, info)

            cur_page = info.get("page")
            cur_start = info.get("page_start") or cur_page
            cur_end = info.get("page_end") or cur_start

        # 2. Node không có page COM thì inherit từ parent nếu phù hợp
        else:
            cur_page = content.get("page") or inherited_page
            cur_start = content.get("page_start") or inherited_start or cur_page
            cur_end = content.get("page_end") or inherited_end or cur_start

            if _should_inherit(node, inside_table):
                _set_page_fields(node, cur_page, cur_start, cur_end)

        # 3. Nếu node là table thì toàn bộ con inherit range của table
        child_inside_table = inside_table or node.type == "table"

        if node.type == "table":
            table_page = node.content.get("page") or cur_page
            table_start = node.content.get("page_start") or table_page or cur_start
            table_end = node.content.get("page_end") or table_start or cur_end

            _set_page_fields(node, table_page, table_start, table_end)

            for child in node.children or []:
                _walk(
                    child,
                    inherited_page=table_page,
                    inherited_start=table_start,
                    inherited_end=table_end,
                    inside_table=True,
                )
            return

        # 4. Shape/paragraph/heading truyền page xuống image con
        parent_page = node.content.get("page") or cur_page
        parent_start = node.content.get("page_start") or parent_page or cur_start
        parent_end = node.content.get("page_end") or parent_start or cur_end

        for child in node.children or []:
            _walk(
                child,
                inherited_page=parent_page,
                inherited_start=parent_start,
                inherited_end=parent_end,
                inside_table=child_inside_table,
            )

    _walk(root)

    logger.info(f"[INJECTOR] Đã inject page cho {len(normalized)} nodes.")


def _normalize_page_map(
    page_map: PageMap | Dict[str, int],
) -> PageMap:
    """
    Convert page_map cũ/mới về cùng format.
    """
    normalized: PageMap = {}

    for uid, value in page_map.items():
        if isinstance(value, dict):
            page = value.get("page")
            page_start = value.get("page_start") or page
            page_end = value.get("page_end") or page_start

            if page_start and not page:
                page = page_start

            if page:
                normalized[uid] = {
                    "page": int(page),
                    "page_start": int(page_start or page),
                    "page_end": int(page_end or page_start or page),
                }

        else:
            page = int(value)
            normalized[uid] = {
                "page": page,
                "page_start": page,
                "page_end": page,
            }

    return normalized


def _apply_page_info(node: DocNode, info: PageInfo) -> None:
    page = info.get("page")
    page_start = info.get("page_start") or page
    page_end = info.get("page_end") or page_start

    _set_page_fields(node, page, page_start, page_end)


def _set_page_fields(
    node: DocNode,
    page: int | None,
    page_start: int | None,
    page_end: int | None,
) -> None:
    if not node.content:
        node.content = {}

    if page:
        node.content["page"] = int(page)

    if page_start:
        node.content["page_start"] = int(page_start)

    if page_end:
        node.content["page_end"] = int(page_end)


def _should_inherit(node: DocNode, inside_table: bool) -> bool:
    """
    Node nào được inherit page từ parent.

    - image luôn inherit
    - row/cell/paragraph/heading/table con trong table inherit range table
    - shape trong table nếu có thì cũng inherit table range nếu không có page riêng
    """
    if node.type == "image":
        return True

    if inside_table and node.type in (
        "row",
        "cell",
        "paragraph",
        "heading",
        "table",
        "shape",
        "image",
    ):
        return True

    return False
