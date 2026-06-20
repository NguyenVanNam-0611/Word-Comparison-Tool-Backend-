"""
diff/shape_diff.py
~~~~~~~~~~~~~~~~~~
So sánh nội dung trong 2 shape node — text và ảnh.

Thứ tự fix:
  1. _handle_replace_pair: paragraph ↔ paragraph nay check cả image con.
     Trước đây nếu text giống nhau nhưng image con thay đổi → bị miss.

  2. collect_diffable_nodes (shape_content.py) bỏ double-emit image con
     của paragraph → paragraph là đơn vị diff duy nhất, image con được
     xử lý bên trong _handle_replace_pair.
     Fix này CHỈ an toàn sau khi fix (1) đã có mặt.
"""

from __future__ import annotations

import difflib
import hashlib
from typing import Any, Dict, List, Optional

from src.services.models.docnode import DocNode
from src.services.diff.paragraph_diff import diff_words
from src.services.diff.image_diff import (
    images_equal,
    build_image_change,
    build_image_equal,
    _image_payload,
)
from src.services.extractor.utils import norm_text as _norm_text
from src.services.extractor.shape.shape_content import collect_diffable_nodes


# ══════════════════════════════════════════════════════════════════════════════
# Serialize helpers
# ══════════════════════════════════════════════════════════════════════════════

def _serialize_node(node: Optional[DocNode]) -> Optional[Dict[str, Any]]:
    if node is None:
        return None
    content = node.content or {}
    safe_content = {
        k: v for k, v in content.items()
        if k not in ("raw_bytes", "data_uri")
    }
    return {
        "uid":          getattr(node, "uid",   None),
        "type":         node.type,
        "display_type": node.type,
        "order":        getattr(node, "order", 0),
        "path":         getattr(node, "path",  ""),
        "content":      safe_content,
        "children":     [_serialize_node(c) for c in (node.children or [])],
    }


# ══════════════════════════════════════════════════════════════════════════════
# Node signature — dùng cho SequenceMatcher
# ══════════════════════════════════════════════════════════════════════════════

def _node_sig(node: DocNode) -> str:
    """
    Signature cho paragraph hoặc image — dùng để align bằng SequenceMatcher.

    Paragraph : hash normalized text.
                Paragraph có text + image con → sig chỉ dựa vào text.
                Image con được xử lý riêng bên trong _handle_replace_pair,
                không tham gia vào align ở tầng này.

    Image     : ưu tiên sha256 (content-based, stable cross-doc).
                Fallback uid để tránh collision khi sha256 vắng mặt —
                uid không stable cross-doc nhưng đảm bảo uniqueness
                trong cùng một diff session.

    Invariant : hai node khác nhau không được ra cùng sig trong 1 diff session.
    """
    if node.type == "image":
        sha = (node.content.get("sha256", "") or "").strip()
        if sha:
            return f"I:sha:{sha[:16]}"

        uid = (getattr(node, "uid", "") or "").strip()
        uid_hash = hashlib.md5(uid.encode("utf-8")).hexdigest()[:12] if uid else "anon"
        return f"I:uid:{uid_hash}"

    # paragraph
    txt = _norm_text(node.content.get("text", ""))
    if not txt:
        # Guard: paragraph rỗng không nên xuất hiện sau fix collect_diffable_nodes
        # nhưng nếu lọt qua thì dùng uid để tránh collision
        uid = (getattr(node, "uid", "") or "").strip()
        uid_hash = hashlib.md5(uid.encode("utf-8")).hexdigest()[:8] if uid else "anon"
        return f"P:empty:{uid_hash}"

    h = hashlib.md5(txt.encode("utf-8")).hexdigest()[:16]
    return f"P:{h}"


# ══════════════════════════════════════════════════════════════════════════════
# Image key — dùng để so sánh image con của paragraph
# ══════════════════════════════════════════════════════════════════════════════

def _img_key(img: DocNode) -> str:
    """
    Key để so sánh identity của image node.

    Dùng sha256 nếu có (content-based, stable).
    Fallback uid nếu không có sha256 — đảm bảo hai ảnh khác nhau
    không bị coi là equal do cùng thiếu sha256.
    """
    sha = (img.content.get("sha256", "") or "").strip()
    return sha or (getattr(img, "uid", "") or "")


# ══════════════════════════════════════════════════════════════════════════════
# Change builder
# ══════════════════════════════════════════════════════════════════════════════

def _build_change(
    cid:         int,
    change_type: str,
    change_kind: str,
    original:    Optional[Dict[str, Any]],
    modified:    Optional[Dict[str, Any]],
    word_diff:   Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    display = (
        change_type
        .replace("_modified", "")
        .replace("_inserted", "")
        .replace("_deleted",  "")
        .replace("_equal",    "")
    )
    return {
        "id":            cid,
        "type":          change_type,
        "display_type":  display,
        "change_kind":   change_kind,
        "original":      original,
        "modified":      modified,
        "left_context":  original,
        "right_context": modified,
        "word_diff":     word_diff,
    }


def _build_image_equal(cid: int, a_node: DocNode, b_node: DocNode) -> Dict[str, Any]:
    """
    Emit equal cho cặp image không thay đổi.

    Dùng build_image_equal từ image_diff.py (include_data=False)
    để tiết kiệm bandwidth — frontend không cần render ảnh equal.
    """
    change = build_image_equal(a_node, b_node)
    change["id"] = cid
    return change


# ══════════════════════════════════════════════════════════════════════════════
# Image children diff — dùng trong _handle_replace_pair
# ══════════════════════════════════════════════════════════════════════════════

def _diff_image_children(
    cid:    int,
    a_imgs: List[DocNode],
    b_imgs: List[DocNode],
    changes: List[Dict[str, Any]],
) -> int:
    """
    Diff image children của hai paragraph node.

    Dùng SequenceMatcher trên _img_key để align đúng thứ tự
    thay vì pair 1-1 theo index (tránh pair nhầm khi có insert/delete).

    Emit: image_modified / image_added / image_deleted / image_equal.
    """
    a_keys = [_img_key(i) for i in a_imgs]
    b_keys = [_img_key(i) for i in b_imgs]

    sm = difflib.SequenceMatcher(a=a_keys, b=b_keys, autojunk=False)

    for tag, i1, i2, j1, j2 in sm.get_opcodes():

        if tag == "equal":
            for k in range(i2 - i1):
                changes.append(_build_image_equal(cid, a_imgs[i1 + k], b_imgs[j1 + k]))
                cid += 1

        elif tag == "insert":
            for img in b_imgs[j1:j2]:
                change = build_image_change(None, img)
                change["id"] = cid
                changes.append(change)
                cid += 1

        elif tag == "delete":
            for img in a_imgs[i1:i2]:
                change = build_image_change(img, None)
                change["id"] = cid
                changes.append(change)
                cid += 1

        elif tag == "replace":
            pairs = min(i2 - i1, j2 - j1)
            for k in range(pairs):
                change = build_image_change(a_imgs[i1 + k], b_imgs[j1 + k])
                change["id"] = cid
                changes.append(change)
                cid += 1
            for img in a_imgs[i1 + pairs:i2]:
                change = build_image_change(img, None)
                change["id"] = cid
                changes.append(change)
                cid += 1
            for img in b_imgs[j1 + pairs:j2]:
                change = build_image_change(None, img)
                change["id"] = cid
                changes.append(change)
                cid += 1

    return cid


# ══════════════════════════════════════════════════════════════════════════════
# Replace handler
# ══════════════════════════════════════════════════════════════════════════════

def _handle_replace_pair(
    cid:     int,
    a_node:  DocNode,
    b_node:  DocNode,
    changes: List[Dict[str, Any]],
) -> int:

    # ── image ↔ image ────────────────────────────────────────────────────────
    if a_node.type == "image" and b_node.type == "image":
        if images_equal(a_node, b_node):
            changes.append(_build_image_equal(cid, a_node, b_node))
        else:
            change = build_image_change(a_node, b_node)
            change["id"] = cid
            changes.append(change)
        return cid + 1

    # ── paragraph ↔ paragraph ────────────────────────────────────────────────
    if a_node.type == "paragraph" and b_node.type == "paragraph":
        old = _norm_text(a_node.content.get("text", ""))
        new = _norm_text(b_node.content.get("text", ""))

        a_imgs = [c for c in (a_node.children or []) if c.type == "image"]
        b_imgs = [c for c in (b_node.children or []) if c.type == "image"]

        text_changed = old != new
        imgs_changed = [_img_key(i) for i in a_imgs] != [_img_key(i) for i in b_imgs]

        # Không có gì thay đổi
        if not text_changed and not imgs_changed:
            changes.append(_build_change(
                cid=cid,
                change_type="paragraph_equal",
                change_kind="equal",
                original=_serialize_node(a_node),
                modified=_serialize_node(b_node),
            ))
            return cid + 1

        # Text thay đổi → emit paragraph_modified
        if text_changed:
            changes.append(_build_change(
                cid=cid,
                change_type="paragraph_modified",
                change_kind="replace",
                original=_serialize_node(a_node),
                modified=_serialize_node(b_node),
                word_diff=diff_words(
                    old_text=old,
                    new_text=new,
                    old_display=a_node.content.get("text_display") or old,
                    new_display=b_node.content.get("text_display") or new,
                    old_numbering=a_node.content.get("numbering"),
                    new_numbering=b_node.content.get("numbering"),
                ),
            ))
            cid += 1

        # Image con thay đổi → diff riêng
        if imgs_changed:
            cid = _diff_image_children(cid, a_imgs, b_imgs, changes)

        return cid

    # ── image ↔ paragraph → delete image + insert paragraph ─────────────────
    if a_node.type == "image":
        change = build_image_change(a_node, None)
        change["id"] = cid
        changes.append(change)
        cid += 1
        changes.append(_build_change(
            cid=cid,
            change_type="paragraph_inserted",
            change_kind="insert",
            original=None,
            modified=_serialize_node(b_node),
        ))
        return cid + 1

    # ── paragraph ↔ image → delete paragraph + insert image ─────────────────
    changes.append(_build_change(
        cid=cid,
        change_type="paragraph_deleted",
        change_kind="delete",
        original=_serialize_node(a_node),
        modified=None,
    ))
    cid += 1
    change = build_image_change(None, b_node)
    change["id"] = cid
    changes.append(change)
    return cid + 1


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def diff_shape(a_shape: DocNode, b_shape: DocNode) -> List[Dict[str, Any]]:
    """
    So sánh nội dung 2 shape node — text và ảnh.

    Pipeline:
        1. collect_diffable_nodes → List[DocNode] theo thứ tự xuất hiện.
           Mỗi paragraph (kể cả có image con) là 1 đơn vị.
           Image direct child của shape là 1 đơn vị riêng.

        2. SequenceMatcher align trên _node_sig.
           Paragraph sig chỉ dựa vào text — image con không tham gia align.

        3. _handle_replace_pair xử lý từng cặp:
           - paragraph ↔ paragraph: diff text + diff image con độc lập.
           - image ↔ image: so sánh sha256.
           - type mismatch: delete + insert.

    Emit:
        paragraph_equal / paragraph_modified / paragraph_inserted / paragraph_deleted
        image_equal / image_modified / image_added / image_deleted

    Trả về [] nếu không có thay đổi nào.
    """
    a_nodes = collect_diffable_nodes(a_shape)
    b_nodes = collect_diffable_nodes(b_shape)

    if not a_nodes and not b_nodes:
        return []

    a_sigs = [_node_sig(n) for n in a_nodes]
    b_sigs = [_node_sig(n) for n in b_nodes]

    sm      = difflib.SequenceMatcher(a=a_sigs, b=b_sigs, autojunk=False)
    opcodes = sm.get_opcodes()

    has_real_change = any(tag != "equal" for tag, *_ in opcodes)
    if not has_real_change:
        # Còn một edge case: tất cả paragraph text giống nhau
        # nhưng image con của một paragraph thay đổi.
        # _node_sig không encode image con → SequenceMatcher báo "equal"
        # nhưng thực tế có thay đổi.
        # Phải scan thêm ở đây.
        has_real_change = _has_image_children_change(a_nodes, b_nodes)
        if not has_real_change:
            return []

    changes: List[Dict[str, Any]] = []
    cid = 1

    for tag, i1, i2, j1, j2 in opcodes:

        # ── EQUAL ─────────────────────────────────────────────────────────────
        if tag == "equal":
            for k in range(i2 - i1):
                a_node = a_nodes[i1 + k]
                b_node = b_nodes[j1 + k]

                if a_node.type == "image":
                    changes.append(_build_image_equal(cid, a_node, b_node))
                    cid += 1
                else:
                    # paragraph: vẫn phải check image con
                    a_imgs = [c for c in (a_node.children or []) if c.type == "image"]
                    b_imgs = [c for c in (b_node.children or []) if c.type == "image"]
                    imgs_changed = (
                        [_img_key(i) for i in a_imgs] != [_img_key(i) for i in b_imgs]
                    )

                    if imgs_changed:
                        # Text bằng nhau nhưng image con khác
                        # → emit paragraph_modified (không có word_diff)
                        # + diff image con
                        changes.append(_build_change(
                            cid=cid,
                            change_type="paragraph_modified",
                            change_kind="replace",
                            original=_serialize_node(a_node),
                            modified=_serialize_node(b_node),
                            word_diff=None,
                        ))
                        cid += 1
                        cid = _diff_image_children(cid, a_imgs, b_imgs, changes)
                    else:
                        changes.append(_build_change(
                            cid=cid,
                            change_type="paragraph_equal",
                            change_kind="equal",
                            original=_serialize_node(a_node),
                            modified=_serialize_node(b_node),
                        ))
                        cid += 1
            continue

        # ── INSERT ────────────────────────────────────────────────────────────
        if tag == "insert":
            for node in b_nodes[j1:j2]:
                if node.type == "image":
                    change = build_image_change(None, node)
                    change["id"] = cid
                    changes.append(change)
                else:
                    changes.append(_build_change(
                        cid=cid,
                        change_type="paragraph_inserted",
                        change_kind="insert",
                        original=None,
                        modified=_serialize_node(node),
                    ))
                cid += 1
            continue

        # ── DELETE ────────────────────────────────────────────────────────────
        if tag == "delete":
            for node in a_nodes[i1:i2]:
                if node.type == "image":
                    change = build_image_change(node, None)
                    change["id"] = cid
                    changes.append(change)
                else:
                    changes.append(_build_change(
                        cid=cid,
                        change_type="paragraph_deleted",
                        change_kind="delete",
                        original=_serialize_node(node),
                        modified=None,
                    ))
                cid += 1
            continue

        # ── REPLACE ───────────────────────────────────────────────────────────
        if tag == "replace":
            a_slice = a_nodes[i1:i2]
            b_slice = b_nodes[j1:j2]
            pairs   = min(len(a_slice), len(b_slice))

            for k in range(pairs):
                cid = _handle_replace_pair(cid, a_slice[k], b_slice[k], changes)

            for node in a_slice[pairs:]:
                if node.type == "image":
                    change = build_image_change(node, None)
                    change["id"] = cid
                    changes.append(change)
                else:
                    changes.append(_build_change(
                        cid=cid,
                        change_type="paragraph_deleted",
                        change_kind="delete",
                        original=_serialize_node(node),
                        modified=None,
                    ))
                cid += 1

            for node in b_slice[pairs:]:
                if node.type == "image":
                    change = build_image_change(None, node)
                    change["id"] = cid
                    changes.append(change)
                else:
                    changes.append(_build_change(
                        cid=cid,
                        change_type="paragraph_inserted",
                        change_kind="insert",
                        original=None,
                        modified=_serialize_node(node),
                    ))
                cid += 1

    return changes


def _has_image_children_change(
    a_nodes: List[DocNode],
    b_nodes: List[DocNode],
) -> bool:
    """
    Scan các cặp paragraph equal để phát hiện image con thay đổi.

    Cần thiết vì _node_sig của paragraph chỉ hash text,
    không encode image con → SequenceMatcher có thể báo "equal"
    dù image con đã thay đổi.

    Chỉ gọi khi SequenceMatcher không tìm thấy thay đổi nào.
    """
    if len(a_nodes) != len(b_nodes):
        return True

    for a, b in zip(a_nodes, b_nodes):
        if a.type != b.type:
            return True
        if a.type == "paragraph":
            a_imgs = [c for c in (a.children or []) if c.type == "image"]
            b_imgs = [c for c in (b.children or []) if c.type == "image"]
            if [_img_key(i) for i in a_imgs] != [_img_key(i) for i in b_imgs]:
                return True

    return False