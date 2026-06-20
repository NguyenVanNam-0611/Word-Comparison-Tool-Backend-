"""
diff/image_diff.py
~~~~~~~~~~~~~~~~~~
So sánh và serialize image nodes.

Thay đổi so với cũ:
  - _image_payload: dùng image_url (URL tĩnh /images/{sha256}.ext) thay data_uri
  - _image_payload: thêm param include_data=True/False
      True  → payload đầy đủ cho changed/added/deleted (có image_url)
      False → chỉ metadata cho equal (không có image_url, tiết kiệm bandwidth)
  - images_equal tầng 2: perceptual hash giờ hoạt động vì raw_bytes có trong content
  - _serialize_node (helper nội bộ): loại raw_bytes + image_url khỏi content dump
    để không leak vào JSON của các node khác (paragraph, cell...)
  - build_image_change: luôn include_data=True vì đây là ảnh đã thay đổi
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.services.models.docnode import DocNode
from src.services.utils.hash import images_changed


def images_equal(a: Optional[DocNode], b: Optional[DocNode]) -> bool:
    """
    Kiểm tra 2 image node có nội dung giống nhau không.
 
    Tầng 1: sha256 byte-exact — nhanh, không decode ảnh.
    Tầng 2: perceptual hash — đọc file từ image_store qua sha256.
            FIX: không còn dùng raw_bytes (đã bị bỏ khỏi DocNode.content).
    """
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if a.type != "image" or b.type != "image":
        return False
 
    # ── Tầng 1: sha256 byte-exact ─────────────────────────────
    a_hash = a.content.get("sha256") or a.content.get("hash")
    b_hash = b.content.get("sha256") or b.content.get("hash")
 
    if a_hash and b_hash:
        if a_hash == b_hash:
            return True
        # sha256 khác → thử tầng 2
 
    # ── Tầng 2: perceptual hash — đọc từ image_store ──────────
    # FIX: thay vì raw_bytes, load file đã lưu qua sha256
    a_bytes = _load_image_bytes(a_hash)
    b_bytes = _load_image_bytes(b_hash)
 
    if a_bytes and b_bytes:
        return not images_changed(a_bytes, b_bytes)
 
    # Không đủ dữ liệu → conservative: coi là đã thay đổi
    return False

def _load_image_bytes(sha256: Optional[str]) -> Optional[bytes]:
    """
    Đọc bytes ảnh từ image_store theo sha256.
    Trả None nếu file không tồn tại hoặc sha256 trống.
    """
    if not sha256:
        return None
    try:
        from src.services.utils.image_store import load_image
        return load_image(sha256)
    except Exception:
        return None
    
def _image_payload(
    node: Optional[DocNode],
    include_data: bool = True,
) -> Optional[Dict[str, Any]]:
    """
    Serialize image node thành payload cho frontend.

    FIX: image_url không còn bị duplicate.
    image.image_url là canonical — top-level image_url đọc từ đó,
    đảm bảo hai nơi luôn nhất quán.
    """
    if node is None:
        return None
    if node.type != "image":
        return None

    content = node.content or {}

    width_emu  = content.get("width_emu")  or 0
    height_emu = content.get("height_emu") or 0

    px_per_emu = 96 / 914400
    width_px   = round(width_emu  * px_per_emu) if width_emu  else content.get("width")
    height_px  = round(height_emu * px_per_emu) if height_emu else content.get("height")

    image_url = content.get("image_url") if include_data else None

    image_block = {
        "name":       content.get("name"),
        "ext":        content.get("ext"),
        "hash":       content.get("hash"),
        "sha256":     content.get("sha256"),
        "width_emu":  width_emu,
        "height_emu": height_emu,
        "width_px":   width_px,
        "height_px":  height_px,
        "image_url":  image_url,   # ← canonical location
        "mime":       content.get("mime"),
        "rid":        content.get("rid"),
        "floating":   content.get("floating", False),
        "alt_text":   content.get("alt_text", ""),
    }

    return {
        "uid":          getattr(node, "uid",   None),
        "type":         "image",
        "display_type": "image",
        "order":        getattr(node, "order", 0),
        "path":         getattr(node, "path",  ""),
        "image":        image_block,
        # FIX: top-level image_url đọc từ image_block, không hard-duplicate
        "image_url":    image_block["image_url"],
        "text":         content.get("text",    ""),
        "caption":      content.get("caption", ""),
    }


def build_image_change(
    a: Optional[DocNode],
    b: Optional[DocNode],
) -> Dict[str, Any]:
    """
    Tạo change object cho image.

    Ảnh changed/added/deleted luôn include_data=True
    → frontend nhận image_url đầy đủ để render.
    """
    # Luôn include data vì đây là ảnh đã xác định là thay đổi
    left_payload  = _image_payload(a, include_data=True)
    right_payload = _image_payload(b, include_data=True)

    # ── IMAGE DELETED ─────────────────────────────────────────────────────────
    if a is not None and b is None:
        return {
            "type":         "image_deleted",
            "display_type": "image",
            "change_kind":  "delete",
            "left":         left_payload,
            "right":        None,
            "left_img":     left_payload,
            "right_img":    None,
        }

    # ── IMAGE ADDED ───────────────────────────────────────────────────────────
    if b is not None and a is None:
        return {
            "type":         "image_added",
            "display_type": "image",
            "change_kind":  "insert",
            "left":         None,
            "right":        right_payload,
            "left_img":     None,
            "right_img":    right_payload,
        }

    # ── IMAGE MODIFIED ────────────────────────────────────────────────────────
    return {
        "type":         "image_modified",
        "display_type": "image",
        "change_kind":  "replace",
        "left":         left_payload,
        "right":        right_payload,
        "left_img":     left_payload,
        "right_img":    right_payload,
    }


def build_image_equal(
    a: Optional[DocNode],
    b: Optional[DocNode],
) -> Dict[str, Any]:
    """
    Tạo change object cho ảnh equal (không thay đổi).

    include_data=False → chỉ trả sha256 + metadata, không có image_url.
    Frontend không cần render ảnh equal → tiết kiệm bandwidth.
    """
    left_payload  = _image_payload(a, include_data=False)
    right_payload = _image_payload(b, include_data=False)

    return {
        "type":         "image_equal",
        "display_type": "image",
        "change_kind":  "equal",
        "left":         left_payload,
        "right":        right_payload,
        "left_img":     left_payload,
        "right_img":    right_payload,
    }