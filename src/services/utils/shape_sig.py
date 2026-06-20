import hashlib
from typing import Optional

def stable_empty_shape_id(
    uid: Optional[str],
    parent_uid: Optional[str],
    heading_ctx: str = "",
) -> str:
    """
    ID ổn định cho empty shape — dùng chung cho signature.py và sequence_align.py.

    Đầu vào: uid + parent_uid (định danh vị trí trong cây) + heading_ctx.
    Không dùng order/path vì chúng thay đổi khi doc bị edit nhỏ.

    Scope: stable trong cùng 1 document version.
    Không stable cross-doc — đây là thiết kế có chủ ý:
        Empty shape không có nội dung để so sánh semantic,
        nên chỉ cần stable đủ để không tạo false positive
        với empty shape khác trong cùng doc.
    """
    raw = f"{parent_uid or ''}:{uid or ''}:{heading_ctx}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]