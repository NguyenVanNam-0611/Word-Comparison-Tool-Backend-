"""
extractor/paragraph.py
~~~~~~~~~~~~~~~~~~~~~~
Extract paragraph và heading từ docx thành DocNode.

Chỉ chịu trách nhiệm:
    - Extract text normalized + text_display
    - Detect heading + heading level
    - Extract numbering/list cơ bản + tính label thực tế ("1.", "a)", "•" ...)
    - Extract inline image
    - Extract shape và emit shape thành sibling node

KHÔNG extract:
    - font name
    - font size
    - color
    - bold/italic/underline
    - paragraph spacing/alignment
    - layout formatting
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from docx.text.paragraph import Paragraph
from docx.oxml.text.run import CT_R
from docx.oxml.ns import qn as _qn
from src.services.models.docnode import DocNode
from src.services.extractor.image import extract_inline_images
from src.services.extractor.shape.shape import extract_shapes_from_paragraph
from src.services.extractor.utils import OrderRef, norm_for_signature, raw_text, next_order
from src.services.extractor.numbering import (
    resolve_numbering,
    compute_numbering_label,
    CounterState,
)

# ═════════════════════════════════════════════════════════════════════
# Heading helpers
# ═════════════════════════════════════════════════════════════════════

_HEADING_RE = re.compile(r"heading\s*(\d+)", re.IGNORECASE)


def extract_page_number(
    par: Paragraph,
    current_page: int,
) -> tuple[int, int]:
    """
    Return:
        (paragraph_page, next_page)

    paragraph_page:
        page của paragraph hiện tại

    next_page:
        page dùng cho paragraph kế tiếp
    """

    paragraph_page = current_page
    next_page = current_page

    pPr = par._p.pPr
    if pPr is not None:
        pbB = pPr.find(_qn("w:pageBreakBefore"))

        if pbB is not None:
            val = pbB.get(_qn("w:val"), "true")
            if val not in ("false", "0"):
                paragraph_page += 1
                next_page += 1

    for elem in par._p.iter():

        # Word render page break
        if elem.tag == _qn("w:lastRenderedPageBreak"):
            next_page += 1

        # Manual page break
        elif elem.tag == _qn("w:br"):
            if elem.get(_qn("w:type")) == "page":
                next_page += 1

    return paragraph_page, next_page


def heading_level(style_name: str) -> int:
    if not style_name:
        return 0
    m = _HEADING_RE.search(style_name.strip())
    return int(m.group(1)) if m else 0


def is_heading(par: Paragraph) -> bool:
    style_name = (par.style.name if par.style else "") or ""
    return style_name.strip().lower().startswith("heading")


# ═════════════════════════════════════════════════════════════════════
# Numbering / list helper
# ═════════════════════════════════════════════════════════════════════


def extract_numbering(
    par: Paragraph,
    numbering_map: Optional[Dict[str, Any]] = None,
    counter_state: Optional[CounterState] = None,
) -> Optional[Dict[str, Any]]:
    """
    Extract numbering/list info của paragraph.

    Trả về:
        {
            "num_id":   "1",
            "level":    0,
            "num_fmt":  "decimal" | "lowerLetter" | "bullet" | ...,
            "lvl_text": "%1." | "%1)" | "" | ...,
            "label":    "1." | "2." | "a)" | "•" | ...   ← text hiển thị thực tế
        }

    label chỉ được tính khi counter_state được truyền vào.
    counter_state phải được khởi tạo 1 lần ở document level và
    truyền qua toàn bộ paragraph theo đúng thứ tự document.
    """
    try:
        pPr = par._p.pPr
        if pPr is None:
            return None

        numPr = pPr.numPr
        if numPr is None:
            return None

        num_id = numPr.numId.val if numPr.numId is not None else None
        level = numPr.ilvl.val if numPr.ilvl is not None else None

        if num_id is None and level is None:
            return None

        resolved: Dict[str, Any] = {}
        label = ""

        if numbering_map:
            num_id_str = str(num_id) if num_id is not None else None
            level_int = int(level) if level is not None else 0

            resolved = resolve_numbering(
                numbering_map=numbering_map,
                num_id=num_id_str,
                level=level_int,
            )

            if counter_state is not None:
                label = compute_numbering_label(
                    numbering_map=numbering_map,
                    counter_state=counter_state,
                    num_id=num_id_str,
                    level=level_int,
                )

        return {
            "num_id": str(num_id) if num_id is not None else None,
            "level": int(level) if level is not None else 0,
            "num_fmt": resolved.get("num_fmt"),
            "lvl_text": resolved.get("lvl_text"),
            "label": label,
        }

    except Exception:
        return None


# ═════════════════════════════════════════════════════════════════════
# Runs text-only
# ═════════════════════════════════════════════════════════════════════
SYM_MAP = {
    ("Wingdings", "F0A3"): "☐",
    ("Wingdings", "F0FE"): "☑",
    ("Wingdings", "F052"): "☑",
    ("Wingdings", "F0FC"): "✓",
    ("Wingdings", "F0A1"): "○",
    ("Wingdings", "F06C"): "●",
    ("Symbol", "F0B7"): "•",
}


def _sym_text(font: str | None, char: str | None) -> str:
    if not char:
        return ""

    font = font or ""

    mapped = SYM_MAP.get((font, char.upper()))
    if mapped:
        return mapped

    try:
        code = int(char, 16)
        if code >= 0xF000:
            code -= 0xF000
        return chr(code)
    except Exception:
        return ""


def paragraph_text_with_symbols(par: Paragraph) -> str:
    parts: List[str] = []

    for child in par._p.iterchildren():
        if not isinstance(child, CT_R):
            continue

        for elem in child.iterchildren():
            tag = elem.tag

            if tag == _qn("w:t"):
                parts.append(elem.text or "")

            elif tag == _qn("w:tab"):
                parts.append("\t")

            elif tag == _qn("w:br"):
                parts.append("\n")

            elif tag == _qn("w:sym"):
                parts.append(
                    _sym_text(
                        elem.get(_qn("w:font")),
                        elem.get(_qn("w:char")),
                    )
                )

    return "".join(parts)


def extract_runs(par: Paragraph) -> List[Dict[str, Any]]:
    """
    Chỉ extract text của run.

    Không lấy:
        - bold / italic / underline
        - font size / color

    Vì project không so sánh style.
    """
    runs: List[Dict[str, Any]] = []

    for idx, run in enumerate(par.runs):
        text = run.text or ""
        if not text:
            continue
        runs.append({"index": idx, "text": text})

    return runs


# ═════════════════════════════════════════════════════════════════════
# Content builder
# ═════════════════════════════════════════════════════════════════════


def build_paragraph_content(
    par: Paragraph,
    imgs: List[DocNode],
    shapes: List[DocNode],
    in_toc: bool,
    numbering_map: Optional[Dict[str, Any]] = None,
    counter_state: Optional[CounterState] = None,
    page: int = 1,
    para_idx: int = 0,
) -> Dict[str, Any]:
    """
    Contract:
        text:         text đã normalize để diff/signature
        text_display: text giữ layout nhẹ để render UI
        numbering:    list info + label thực tế để hiển thị
    """
    raw = paragraph_text_with_symbols(par)
    text = norm_for_signature(raw)
    display = raw_text(raw)

    style_name = (par.style.name if par.style else "") or ""
    heading = is_heading(par)
    level = heading_level(style_name) if heading else 0

    return {
        "text": text,
        "text_display": display,
        "is_heading": heading,
        "heading_level": level,
        "level": level,
        "page": page,
        "para_idx": para_idx,
        "numbering": extract_numbering(
            par,
            numbering_map=numbering_map,
            counter_state=counter_state,
        ),
        "run_count": len(par.runs),
        "runs": extract_runs(par),
        "image_count": len(imgs),
        "shape_count": len(shapes),
        "in_toc": in_toc,
    }


# ═════════════════════════════════════════════════════════════════════
# Node helpers
# ═════════════════════════════════════════════════════════════════════


def _make_text_node(
    *,
    node_type: str,
    uid: str,
    parent_uid: Optional[str],
    order_ref: OrderRef,
    content: Dict[str, Any],
) -> DocNode:
    return DocNode(
        type=node_type,
        uid=uid,
        parent_uid=parent_uid,
        order=next_order(order_ref),
        path=uid.replace(".", "/"),
        content=content,
    )


def _attach_images(
    node: DocNode,
    imgs: List[DocNode],
    order_ref: OrderRef,
    page: int = 1,
) -> None:
    for img in imgs:
        img.order = next_order(order_ref)
        if "page" not in (img.content or {}):
            img.content["page"] = page
        node.add_child(img)


def _emit_shapes(
    shapes: List[DocNode],
    order_ref: OrderRef,
    page: int = 1,
    para_idx: int = 0,
) -> List[DocNode]:
    result: List[DocNode] = []

    for shp in shapes:
        shp.order = next_order(order_ref)

        if not shp.content:
            shp.content = {}

        # Page fallback cũ, giữ lại để phòng COM không trả page
        if "page" not in shp.content:
            shp.content["page"] = page

        # Shape ngoài body: nhớ paragraph body-level đang neo shape.
        # para_idx > 0 mới hợp lệ. Shape trong cell nếu para_idx=0 thì bỏ qua.
        if para_idx:
            shp.content["anchor_para_idx"] = para_idx

        result.append(shp)

    return result


# ═════════════════════════════════════════════════════════════════════
# Public API
# ═════════════════════════════════════════════════════════════════════


def extract_paragraph_nodes(
    par: Paragraph,
    uid: str,
    parent_uid: Optional[str],
    order_ref: OrderRef,
    in_toc: bool = False,
    numbering_map: Optional[Dict[str, Any]] = None,
    counter_state: Optional[CounterState] = None,
    page: int = 1,
    para_idx: int = 0,
) -> List[DocNode]:
    """
    Extract 1 docx Paragraph thành list DocNode.

    Có thể trả:
        - []
        - [paragraph]
        - [heading]
        - [paragraph, shape...]
        - [heading, shape...]

    counter_state phải được truyền từ document level để đảm bảo
    numbering label ("1.", "2.", "a)"...) được tính đúng thứ tự.
    """

    # ── Normal paragraph ──────────────────────────────────────────
    imgs = extract_inline_images(
        par,
        uid_prefix=f"{uid}.img",
        parent_uid=uid,
        order_ref=order_ref,
    )

    shapes = extract_shapes_from_paragraph(
        par,
        uid_prefix=f"{uid}.shp",
        parent_uid=parent_uid,
        order_ref=order_ref,
    )

    content = build_paragraph_content(
        par=par,
        imgs=imgs,
        shapes=shapes,
        in_toc=in_toc,
        numbering_map=numbering_map,
        counter_state=counter_state,
        page=page,
        para_idx=para_idx,
    )

    text = content["text"]
    node_type = "heading" if content["is_heading"] else "paragraph"

    nodes: List[DocNode] = []

    # Có text hoặc image thì tạo text node
    if text or imgs:
        node = _make_text_node(
            node_type=node_type,
            uid=uid,
            parent_uid=parent_uid,
            order_ref=order_ref,
            content=content,
        )
        _attach_images(node, imgs, order_ref, page=page)
        nodes.append(node)

    # Shape emit độc lập, kể cả paragraph rỗng
    nodes.extend(
        _emit_shapes(
            shapes,
            order_ref,
            page=page,
            para_idx=para_idx,
        )
    )

    return nodes
