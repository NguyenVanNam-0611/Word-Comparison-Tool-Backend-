"""
extractor/shape.py
~~~~~~~~~~~~~~~~~~
Extract shape / textbox từ một Paragraph → DocNode.

Cover:
    ✅ Textbox VML : w:pict → v:textbox → w:txbxContent
    ✅ Textbox DML : w:drawing → wp:anchor → wps:txbx → w:txbxContent
    ✅ Paragraph, bảng, ảnh lồng bên trong textbox
    ✅ Merged cell (colspan / rowspan) trong bảng bên trong textbox
    ✅ shape_id ổn định từ XML
    ✅ Vị trí floating shape (x_emu, y_emu, w_emu, h_emu)
    ✅ Dedup textbox element an toàn
    ✅ Bỏ qua mc:Fallback — tránh extract duplicate

Không xử lý:
    ❌ SmartArt, Chart, OLE object
    ❌ Shape không có txbxContent (chỉ là hình vẽ thuần)

Giữ riêng — xử lý XML thô (lxml), không dùng python-docx object.

Import từ:
    utils.py         — norm_text, next_order
    utils/hash.py    — safe_sha256, bytes_to_data_uri

KHÔNG import từ shape_content.py — tránh circular import.
shape_content.py làm việc trên DocNode đã built, shape.py build DocNode.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from docx.text.paragraph import Paragraph

from src.services.models.docnode import DocNode
from src.services.extractor.utils import OrderRef, norm_text, next_order
from src.services.utils.hash import safe_sha256
from src.services.utils.image_store import save_image


# ══════════════════════════════════════════════════════════════════════════════
# Namespaces
# ══════════════════════════════════════════════════════════════════════════════

_W   = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R   = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_A   = "http://schemas.openxmlformats.org/drawingml/2006/main"
_WP  = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_WPS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
_V   = "urn:schemas-microsoft-com:vml"
_MC  = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_WPG = "http://schemas.microsoft.com/office/word/2010/wordprocessingGroup"

def _tag(ns: str, local: str) -> str:
    return f"{{{ns}}}{local}"


def _iter_tag(elem, ns: str, local: str):
    return elem.iter(_tag(ns, local))


# ══════════════════════════════════════════════════════════════════════════════
# XML text helper
# ══════════════════════════════════════════════════════════════════════════════

def _get_all_text(xml_elem) -> str:
    """Gom toàn bộ w:t text bên trong xml_elem."""
    parts = [t.text or "" for t in _iter_tag(xml_elem, _W, "t")]
    return norm_text("".join(parts))


def _emu_to_int(v: Any) -> int:
    try:
        return int(v)
    except Exception:
        return 0


# ══════════════════════════════════════════════════════════════════════════════
# Shape position extractors
# ══════════════════════════════════════════════════════════════════════════════

def _extract_anchor_position(drawing_xml) -> Dict[str, Any]:
    """Lấy vị trí và kích thước từ wp:anchor (DML floating shape)."""
    result: Dict[str, Any] = {
        "x_emu": 0, "y_emu": 0, "w_emu": 0, "h_emu": 0,
        "is_floating": False,
        "relative_from_h": "page",
        "relative_from_v": "page",
    }

    anchor = next(_iter_tag(drawing_xml, _WP, "anchor"), None)

    if anchor is None:
        inline = next(_iter_tag(drawing_xml, _WP, "inline"), None)
        if inline is not None:
            extent = next(_iter_tag(inline, _WP, "extent"), None)
            if extent is not None:
                result["w_emu"] = _emu_to_int(extent.get("cx", 0))
                result["h_emu"] = _emu_to_int(extent.get("cy", 0))
        return result

    result["is_floating"] = True

    extent = next(_iter_tag(anchor, _WP, "extent"), None)
    if extent is not None:
        result["w_emu"] = _emu_to_int(extent.get("cx", 0))
        result["h_emu"] = _emu_to_int(extent.get("cy", 0))

    pos_h = next(_iter_tag(anchor, _WP, "positionH"), None)
    if pos_h is not None:
        result["relative_from_h"] = pos_h.get("relativeFrom", "page")
        offset = next(_iter_tag(pos_h, _WP, "posOffset"), None)
        if offset is not None and offset.text:
            try:
                result["x_emu"] = int(offset.text)
            except ValueError:
                pass

    pos_v = next(_iter_tag(anchor, _WP, "positionV"), None)
    if pos_v is not None:
        result["relative_from_v"] = pos_v.get("relativeFrom", "page")
        offset = next(_iter_tag(pos_v, _WP, "posOffset"), None)
        if offset is not None and offset.text:
            try:
                result["y_emu"] = int(offset.text)
            except ValueError:
                pass

    return result


def _extract_vml_size(pict_xml) -> Dict[str, Any]:
    """Lấy vị trí và kích thước từ v:shape style (VML shape)."""
    result: Dict[str, Any] = {
        "x_emu": 0, "y_emu": 0, "w_emu": 0, "h_emu": 0,
        "is_floating": True,
        "relative_from_h": "page",
        "relative_from_v": "page",
    }
    _PT_TO_EMU = 12700

    for shape in _iter_tag(pict_xml, _V, "shape"):
        style = shape.get("style", "")
        if not style:
            continue
        parts = {
            p.split(":")[0].strip(): p.split(":")[1].strip()
            for p in style.split(";") if ":" in p
        }

        def _pt_to_emu(val: str) -> int:
            val = val.replace("pt", "").replace("px", "").strip()
            try:
                return int(float(val) * _PT_TO_EMU)
            except Exception:
                return 0

        result["w_emu"] = _pt_to_emu(parts.get("width", "0"))
        result["h_emu"] = _pt_to_emu(parts.get("height", "0"))
        result["x_emu"] = _pt_to_emu(parts.get("margin-left", "0"))
        result["y_emu"] = _pt_to_emu(parts.get("margin-top", "0"))
        break

    return result


# ══════════════════════════════════════════════════════════════════════════════
# Shape ID helpers
# ══════════════════════════════════════════════════════════════════════════════

def _get_shape_id_from_drawing(drawing_xml) -> str:
    for doc_pr in _iter_tag(drawing_xml, _WP, "docPr"):
        sid  = doc_pr.get("id", "")
        name = doc_pr.get("name", "")
        if sid or name:
            return f"{sid}_{name}" if (sid and name) else (sid or name)
    return ""


def _get_shape_id_from_vml(pict_xml) -> str:
    for shape in _iter_tag(pict_xml, _V, "shape"):
        sid = shape.get("id", "")
        if sid:
            return sid
    return ""


# ══════════════════════════════════════════════════════════════════════════════
# Image extractor (trong textbox)
# ══════════════════════════════════════════════════════════════════════════════

def _extract_image_node(
    blip_elem,
    extent_elem,
    part,
    uid: str,
    parent_uid: Optional[str],
    order: int,
) -> Optional[DocNode]:
    rid = blip_elem.get(_tag(_R, "embed"))
    if not rid:
        return None

    image_part = part.related_parts.get(rid) if part else None
    if image_part is None:
        return None

    blob = image_part.blob
    if not blob:
        return None

    cx   = _emu_to_int(extent_elem.get("cx")) if extent_elem is not None else 0
    cy   = _emu_to_int(extent_elem.get("cy")) if extent_elem is not None else 0
    mime = getattr(image_part, "content_type", None) or "image/png"
    sha  = safe_sha256(blob)
    url  = save_image(sha, blob, mime)         # lưu file, trả URL

    return DocNode(
        type="image",
        uid=uid,
        parent_uid=parent_uid,
        order=order,
        path=uid.replace(".", "/"),
        content={
            "rid":        rid,
            "hash":       sha,
            "sha256":     sha,
            "mime":       mime,
            "width_emu":  cx,
            "height_emu": cy,
            "alt_text":   "",
            "image_url":  url,               
        },
    )

def _extract_images_from_xml(
    xml_elem,
    part,
    uid_prefix: str,
    parent_uid: Optional[str],
    order_ref: Optional[OrderRef],
) -> List[DocNode]:
    nodes: List[DocNode] = []
    blip_tag = _tag(_A, "blip")
    _O      = "urn:schemas-microsoft-com:office:office"

    # ── DrawingML ──────────────────────────────────────────────────────────
    for draw_idx, drawing in enumerate(_iter_tag(xml_elem, _W, "drawing"), start=1):
        extent = next(_iter_tag(drawing, _WP, "extent"), None)
        for blip_idx, blip in enumerate(drawing.iter(blip_tag), start=1):
            img_uid = f"{uid_prefix}.d{draw_idx}.b{blip_idx}"
            node = _extract_image_node(
                blip_elem=blip,
                extent_elem=extent,
                part=part,
                uid=img_uid,
                parent_uid=parent_uid,
                order=next_order(order_ref),
            )
            if node:
                nodes.append(node)

    # ── VML helper (dùng chung cho pict và object) ─────────────────────────
    def _build_vml_node(imgdata, container, uid: str) -> Optional[DocNode]:
        rid = imgdata.get(_tag(_R, "id"))
        if not rid:
            return None
        image_part = part.related_parts.get(rid) if part else None
        if image_part is None:
            return None
        blob = image_part.blob
        if not blob:
            return None

        mime = getattr(image_part, "content_type", None) or "image/png"
        from src.services.extractor.image import _normalize_blob
        blob, mime = _normalize_blob(blob, mime)

        sha = safe_sha256(blob)
        url = save_image(sha, blob, mime)

        w_emu = h_emu = 0
        for vshape in _iter_tag(container, _V, "shape"):
            style = vshape.get("style", "")
            props = {
                p.split(":")[0].strip(): p.split(":")[1].strip()
                for p in style.split(";") if ":" in p
            }
            def _pt(val: str) -> int:
                val = val.replace("pt", "").replace("px", "").strip()
                try:
                    return int(float(val) * 12700)
                except Exception:
                    return 0
            w_emu = _pt(props.get("width", "0"))
            h_emu = _pt(props.get("height", "0"))
            break

        return DocNode(
            type="image",
            uid=uid,
            parent_uid=parent_uid,
            order=next_order(order_ref),
            path=uid.replace(".", "/"),
            content={
                "rid":        rid,
                "hash":       sha,
                "sha256":     sha,
                "mime":       mime,
                "width_emu":  w_emu,
                "height_emu": h_emu,
                "alt_text":   imgdata.get(_tag(_O, "title"), ""),
                "image_url":  url,
            },
        )

    # ── VML (w:pict > v:imagedata) ─────────────────────────────────────────
    for pict_idx, pict in enumerate(_iter_tag(xml_elem, _W, "pict"), start=1):
        for img_idx, imgdata in enumerate(
            _iter_tag(pict, _V, "imagedata"), start=1
        ):
            node = _build_vml_node(imgdata, pict, f"{uid_prefix}.p{pict_idx}.v{img_idx}")
            if node:
                nodes.append(node)

    # ── OLE object preview (w:object > v:imagedata) ────────────────────────
    for obj_idx, obj in enumerate(_iter_tag(xml_elem, _W, "object"), start=1):
        for img_idx, imgdata in enumerate(
            _iter_tag(obj, _V, "imagedata"), start=1
        ):
            node = _build_vml_node(imgdata, obj, f"{uid_prefix}.obj{obj_idx}.v{img_idx}")
            if node:
                nodes.append(node)

    return nodes


# ══════════════════════════════════════════════════════════════════════════════
# Paragraph parser (trong textbox)
# ══════════════════════════════════════════════════════════════════════════════

def _parse_p_xml(
    p_xml,
    part,
    uid_prefix: str,
    parent_uid: Optional[str],
    order_ref: Optional[OrderRef],
) -> List[DocNode]:
    """
    Parse 1 paragraph XML bên trong textbox → DocNode(type=paragraph).

    Image được add vào children của paragraph — không phải direct child
    của shape. Đây là cấu trúc chuẩn để shape_content.py collect đúng.
    """
    text = _get_all_text(p_xml)
    imgs = _extract_images_from_xml(
        p_xml, part,
        uid_prefix=f"{uid_prefix}.img",
        parent_uid=uid_prefix,
        order_ref=order_ref,
    )

    if not text and not imgs:
        return []

    pnode = DocNode(
        type="paragraph",
        uid=uid_prefix,
        parent_uid=parent_uid,
        order=next_order(order_ref),
        path=uid_prefix.replace(".", "/"),
        content={"text": text, "image_count": len(imgs)},
    )
    for img in imgs:
        pnode.add_child(img)

    return [pnode]


# ══════════════════════════════════════════════════════════════════════════════
# Merged cell helpers
# ══════════════════════════════════════════════════════════════════════════════

def _tc_col_span(tc) -> int:
    tcPr = next((c for c in tc if c.tag == _tag(_W, "tcPr")), None)
    if tcPr is None:
        return 1
    gs = next((c for c in tcPr if c.tag == _tag(_W, "gridSpan")), None)
    if gs is None:
        return 1
    try:
        return int(gs.get(_tag(_W, "val"), 1))
    except Exception:
        return 1


def _get_vmerge(tc) -> Tuple[bool, bool]:
    """Trả (is_restart, is_continue)."""
    tcPr = next((c for c in tc if c.tag == _tag(_W, "tcPr")), None)
    if tcPr is None:
        return False, False
    vm = next((c for c in tcPr if c.tag == _tag(_W, "vMerge")), None)
    if vm is None:
        return False, False
    val = vm.get(_tag(_W, "val"), "")
    return (val == "restart"), (val != "restart")


def _build_xml_rows(tbl_xml) -> List[List]:
    tr_tag = _tag(_W, "tr")
    tc_tag = _tag(_W, "tc")
    return [
        [tc for tc in tr if tc.tag == tc_tag]
        for tr in tbl_xml if tr.tag == tr_tag
    ]


def _count_row_span(xml_rows: List[List], row_idx: int, grid_col: int) -> int:
    span = 1
    for r in range(row_idx + 1, len(xml_rows)):
        pos   = 0
        found = None
        for tc in xml_rows[r]:
            if pos == grid_col:
                found = tc
                break
            pos += _tc_col_span(tc)
            if pos > grid_col:
                break
        if found is None:
            break
        tcPr = next((c for c in found if c.tag == _tag(_W, "tcPr")), None)
        vm   = next(
            (c for c in tcPr if c.tag == _tag(_W, "vMerge")), None
        ) if tcPr is not None else None
        if vm is None:
            break
        if vm.get(_tag(_W, "val"), "") == "restart":
            break
        span += 1
    return span


# ══════════════════════════════════════════════════════════════════════════════
# Table parser (trong textbox)
# ══════════════════════════════════════════════════════════════════════════════

def _parse_tbl_xml(
    tbl_xml,
    part,
    uid_prefix: str,
    parent_uid: Optional[str],
    order_ref: Optional[OrderRef],
) -> DocNode:
    """Parse 1 table XML bên trong textbox → DocNode(type=table)."""
    xml_rows  = _build_xml_rows(tbl_xml)
    row_count = len(xml_rows)
    col_count = max(
        (sum(_tc_col_span(tc) for tc in row) for row in xml_rows),
        default=0,
    )

    table_text = "\n".join(
        " | ".join(_get_all_text(tc) for tc in row)
        for row in xml_rows
    )

    tnode = DocNode(
        type="table",
        uid=uid_prefix,
        parent_uid=parent_uid,
        order=next_order(order_ref),
        path=uid_prefix.replace(".", "/"),
        content={
            "row_count": row_count,
            "col_count": col_count,
            "text":      table_text,
        },
    )

    skip_map: Dict[Tuple[int, int], bool] = {}

    for r_idx, row_tcs in enumerate(xml_rows):
        row_uid  = f"{uid_prefix}.r{r_idx}"
        row_text = " | ".join(_get_all_text(tc) for tc in row_tcs)

        rnode = DocNode(
            type="row",
            uid=row_uid,
            parent_uid=uid_prefix,
            order=next_order(order_ref),
            path=row_uid.replace(".", "/"),
            content={"row_index": r_idx, "text": row_text},
        )
        tnode.add_child(rnode)

        logical_col = 0
        for tc in row_tcs:
            col_span            = _tc_col_span(tc)
            is_restart, is_cont = _get_vmerge(tc)

            if skip_map.get((r_idx, logical_col)) or is_cont:
                logical_col += col_span
                continue

            row_span  = 1
            is_merged = col_span > 1

            if is_restart:
                row_span  = _count_row_span(xml_rows, r_idx, logical_col)
                is_merged = True
                for dr in range(1, row_span):
                    for dc in range(col_span):
                        skip_map[(r_idx + dr, logical_col + dc)] = True

            cell_uid = f"{uid_prefix}.r{r_idx}.c{logical_col}"
            cnode = DocNode(
                type="cell",
                uid=cell_uid,
                parent_uid=row_uid,
                order=next_order(order_ref),
                path=cell_uid.replace(".", "/"),
                content={
                    "row_index": r_idx,
                    "col_index": logical_col,
                    "col_span":  col_span,
                    "row_span":  row_span,
                    "is_merged": is_merged,
                    "text":      _get_all_text(tc),
                },
            )
            rnode.add_child(cnode)

            child_idx = 0
            for child in tc:
                if child.tag == _tag(_W, "p"):
                    child_idx += 1
                    for n in _parse_p_xml(
                        child, part,
                        f"{cell_uid}.p{child_idx}", cell_uid, order_ref,
                    ):
                        cnode.add_child(n)
                elif child.tag == _tag(_W, "tbl"):
                    child_idx += 1
                    cnode.add_child(
                        _parse_tbl_xml(
                            child, part,
                            f"{cell_uid}.t{child_idx}", cell_uid, order_ref,
                        )
                    )

            logical_col += col_span

    return tnode


# ══════════════════════════════════════════════════════════════════════════════
# txbxContent parser
# ══════════════════════════════════════════════════════════════════════════════

def _count_images_in_children(children: List[DocNode]) -> int:
    """
    Đếm tổng số image trong children của shape.

    Image luôn nằm trong paragraph.children (vì _parse_p_xml add image
    vào pnode.children) — không bao giờ là direct child của shape.
    Dòng `sum(1 for x in children if x.type == "image")` cũ luôn = 0,
    bỏ đi cho rõ ràng.
    """
    count = 0
    for child in children:
        if child.type == "paragraph":
            count += sum(1 for gc in (child.children or []) if gc.type == "image")
        elif child.type == "table":
            # Walk vào table để đếm image trong cell
            for row in (child.children or []):
                for cell in (row.children or [] if row.type == "row" else []):
                    if cell.type == "cell":
                        for cc in (cell.children or []):
                            if cc.type == "paragraph":
                                count += sum(
                                    1 for gc in (cc.children or [])
                                    if gc.type == "image"
                                )
    return count


def _parse_txbx_content(
    txbx_xml, part, shape_id, uid_prefix, parent_uid, order_ref, position=None,
) -> DocNode:
    """
    Parse txbxContent → DocNode(type="shape") với đầy đủ children.

    order_ref (global): chỉ dùng để đặt order cho shape node cha.
    local_ref: counter riêng cho children bên trong shape —
               tránh làm lộn xộn global order của document.
    """
    pos = position or {}
    local_ref: OrderRef = {"value": 0}

    shape = DocNode(
        type="shape",
        uid=uid_prefix,
        parent_uid=parent_uid,
        order=next_order(order_ref),
        path=uid_prefix.replace(".", "/"),
        content={
            "shape_id":        shape_id or uid_prefix,
            "shape_type":      "textbox",
            "x_emu":           pos.get("x_emu", 0),
            "y_emu":           pos.get("y_emu", 0),
            "w_emu":           pos.get("w_emu", 0),
            "h_emu":           pos.get("h_emu", 0),
            "is_floating":     pos.get("is_floating", False),
            "relative_from_h": pos.get("relative_from_h", "page"),
            "relative_from_v": pos.get("relative_from_v", "page"),
        },
    )

    children: List[DocNode] = []
    child_idx = 0
    for child in txbx_xml:
        if child.tag == _tag(_W, "p"):
            child_idx += 1
            children.extend(
                _parse_p_xml(
                    child, part,
                    f"{uid_prefix}.p{child_idx}", uid_prefix, local_ref,
                )
            )
        elif child.tag == _tag(_W, "tbl"):
            child_idx += 1
            children.append(
                _parse_tbl_xml(
                    child, part,
                    f"{uid_prefix}.t{child_idx}", uid_prefix, local_ref,
                )
            )

    shape.content.update({
        "paragraph_count": sum(1 for x in children if x.type == "paragraph"),
        "image_count":     _count_images_in_children(children),  # FIX: bỏ dòng thừa
        "table_count":     sum(1 for x in children if x.type == "table"),
    })
    for ch in children:
        shape.add_child(ch)

    return shape


# ══════════════════════════════════════════════════════════════════════════════
# txbxContent collector — dedup an toàn
# ══════════════════════════════════════════════════════════════════════════════

def _collect_txbx_elements(p_xml) -> List[Tuple]:
    """
    Thu thập tất cả txbxContent từ paragraph XML.
    Dedup bằng id(element) để tránh xử lý 2 lần cùng 1 textbox.

    Bỏ qua mc:Fallback — Word embed cùng 1 shape 2 lần trong
    mc:AlternateContent (Choice=DML, Fallback=VML). Chỉ lấy Choice
    để tránh extract duplicate dẫn đến double change row trên UI.
    """
    results: List[Tuple] = []
    seen: set = set()
    txbx_tag = _tag(_W, "txbxContent")

    fallback_ids: set = set()
    for alt in p_xml.iter(_tag(_MC, "AlternateContent")):
        for child in alt:
            if child.tag == _tag(_MC, "Fallback"):
                for el in child.iter():
                    fallback_ids.add(id(el))

    for container in (
        list(_iter_tag(p_xml, _W, "pict")) +
        list(_iter_tag(p_xml, _W, "drawing"))
    ):
        if id(container) in fallback_ids:
            continue

        is_drawing = container.tag == _tag(_W, "drawing")

        if is_drawing:
            shape_id = _get_shape_id_from_drawing(container)
            position = _extract_anchor_position(container)
        else:
            shape_id = _get_shape_id_from_vml(container)
            position = _extract_vml_size(container)

        for txbx in container.iter(txbx_tag):
            key = id(txbx)
            if key not in seen:
                seen.add(key)
                results.append((txbx, shape_id, position))

    return results


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════
def _is_group_drawing(drawing_xml) -> bool:
    has_group = any(True for _ in _iter_tag(drawing_xml, _WPG, "wgp"))
    has_txbx  = any(True for _ in _iter_tag(drawing_xml, _W, "txbxContent"))
    return has_group and not has_txbx


def _parse_group_drawing(
    drawing_xml,
    part,
    shape_id,
    uid_prefix,
    parent_uid,
    order_ref,
    position=None,
) -> Optional[DocNode]:
    pos = position or {}
    local_ref: OrderRef = {"value": 0}

    imgs = _extract_images_from_xml(
        drawing_xml,
        part,
        uid_prefix=f"{uid_prefix}.p1.img",
        parent_uid=f"{uid_prefix}.p1",
        order_ref=local_ref,
    )

    if not imgs:
        return None

    shape = DocNode(
        type="shape",
        uid=uid_prefix,
        parent_uid=parent_uid,
        order=next_order(order_ref),
        path=uid_prefix.replace(".", "/"),
        content={
            "shape_id": shape_id or uid_prefix,
            "shape_type": "group",
            "x_emu": pos.get("x_emu", 0),
            "y_emu": pos.get("y_emu", 0),
            "w_emu": pos.get("w_emu", 0),
            "h_emu": pos.get("h_emu", 0),
            "is_floating": pos.get("is_floating", False),
            "relative_from_h": pos.get("relative_from_h", "page"),
            "relative_from_v": pos.get("relative_from_v", "page"),
            "paragraph_count": 1,
            "image_count": len(imgs),
            "table_count": 0,
        },
    )

    pnode = DocNode(
        type="paragraph",
        uid=f"{uid_prefix}.p1",
        parent_uid=uid_prefix,
        order=next_order(local_ref),
        path=f"{uid_prefix}.p1".replace(".", "/"),
        content={"text": "", "image_count": len(imgs)},
    )

    for img in imgs:
        pnode.add_child(img)

    shape.add_child(pnode)
    return shape

def extract_shapes_from_paragraph(
    paragraph: Paragraph,
    uid_prefix: str = "shp",
    parent_uid: Optional[str] = None,
    order_ref: Optional[OrderRef] = None,
) -> List[DocNode]:
    part  = getattr(paragraph, "part", None)
    p_xml = paragraph._p
    out: List[DocNode] = []

    idx = 0

    # 1) Textbox shape
    for txbx_xml, shape_id, position in _collect_txbx_elements(p_xml):
        idx += 1
        out.append(
            _parse_txbx_content(
                txbx_xml=txbx_xml,
                part=part,
                shape_id=shape_id,
                uid_prefix=f"{uid_prefix}.{idx}",
                parent_uid=parent_uid,
                order_ref=order_ref,
                position=position,
            )
        )

    # 2) Group shape có ảnh, không có textbox
    for drawing in _iter_tag(p_xml, _W, "drawing"):
        if not _is_group_drawing(drawing):
            continue

        idx += 1
        node = _parse_group_drawing(
            drawing_xml=drawing,
            part=part,
            shape_id=_get_shape_id_from_drawing(drawing),
            uid_prefix=f"{uid_prefix}.{idx}",
            parent_uid=parent_uid,
            order_ref=order_ref,
            position=_extract_anchor_position(drawing),
        )

        if node:
            out.append(node)

    return out