"""
page/bookmark_marker.py
~~~~~~~~~~~~~~~~~~~~~~~
Tạo temp .docx có bookmark tại paragraph ngoài body theo para_idx.

Dùng để lấy page paragraph bằng Word COM mà không phải scan doc.Paragraphs.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.oxml.text.paragraph import CT_P

logger = logging.getLogger(__name__)

ParaTarget = Tuple[str, int]  # (uid, para_idx)


def create_temp_docx_with_para_bookmarks(
    docx_path: str,
    para_targets: Iterable[ParaTarget],
) -> Tuple[str | None, Dict[str, str]]:
    """
    Copy docx_path sang temp docx rồi chèn bookmark vào paragraph ngoài body.

    para_idx phải cùng rule với extractor/document.py:
    - chỉ paragraph con trực tiếp của document body
    - tính cả paragraph rỗng
    - không tính paragraph trong table/shape

    Returns:
        (temp_docx_path, uid_to_bookmark)
    """
    targets = _normalize_targets(para_targets)
    if not targets:
        return None, {}

    src = Path(docx_path)
    if not src.exists():
        logger.warning("[BM] source docx không tồn tại: %s", docx_path)
        return None, {}

    temp_path = _make_temp_docx_path(src)
    shutil.copy2(str(src), temp_path)

    uid_to_bookmark: Dict[str, str] = {}

    try:
        doc = Document(temp_path)

        idx_to_uids: Dict[int, List[str]] = {}
        for uid, para_idx in targets:
            idx_to_uids.setdefault(para_idx, []).append(uid)

        body_para_idx = 0
        bookmark_id = 1
        inserted = 0

        for child in doc.element.body.iterchildren():
            if not isinstance(child, CT_P):
                continue

            body_para_idx += 1
            uids = idx_to_uids.get(body_para_idx)
            if not uids:
                continue

            for uid in uids:
                bm_name = _bookmark_name_for_uid(uid)

                _insert_bookmark_at_paragraph_start(
                    p=child,
                    bookmark_id=bookmark_id,
                    bookmark_name=bm_name,
                )

                uid_to_bookmark[uid] = bm_name
                bookmark_id += 1
                inserted += 1

        if inserted < len(targets):
            logger.warning(
                "[BM] inserted=%s/%s, body_para_count=%s",
                inserted,
                len(targets),
                body_para_idx,
            )

        doc.save(temp_path)

        logger.info(
            "[BM] created temp bookmark docx: inserted=%s/%s path=%s",
            inserted,
            len(targets),
            temp_path,
        )

        return temp_path, uid_to_bookmark

    except Exception:
        cleanup_temp_docx(temp_path)
        raise


def cleanup_temp_docx(path: str | None) -> None:
    if not path:
        return

    try:
        if os.path.exists(path):
            os.remove(path)
            logger.debug("[BM] removed temp docx: %s", path)
    except Exception as e:
        logger.warning("[BM] remove temp docx failed: %s err=%s", path, e)


def _normalize_targets(
    para_targets: Iterable[ParaTarget],
) -> List[ParaTarget]:
    result: List[ParaTarget] = []
    seen_uid: set[str] = set()

    for uid, para_idx in para_targets:
        if not uid:
            continue

        try:
            idx = int(para_idx)
        except Exception:
            continue

        if idx < 1:
            continue

        if uid in seen_uid:
            continue

        seen_uid.add(uid)
        result.append((str(uid), idx))

    return result


def _make_temp_docx_path(src: Path) -> str:
    return os.path.join(
        tempfile.gettempdir(),
        f"page_bm_{src.stem}_{uuid.uuid4().hex}.docx",
    )


def _bookmark_name_for_uid(uid: str) -> str:
    safe = []

    for ch in uid:
        if ch.isalnum() or ch == "_":
            safe.append(ch)
        else:
            safe.append("_")

    # Word bookmark name không nên dài/quái.
    return ("BM_" + "".join(safe))[:40]


def _insert_bookmark_at_paragraph_start(
    p,
    bookmark_id: int,
    bookmark_name: str,
) -> None:
    bookmark_start = OxmlElement("w:bookmarkStart")
    bookmark_start.set(qn("w:id"), str(bookmark_id))
    bookmark_start.set(qn("w:name"), bookmark_name)

    bookmark_end = OxmlElement("w:bookmarkEnd")
    bookmark_end.set(qn("w:id"), str(bookmark_id))

    # Insert vào đầu paragraph.
    # Thứ tự sau insert:
    #   bookmarkStart, bookmarkEnd, nội dung paragraph cũ...
    p.insert(0, bookmark_end)
    p.insert(0, bookmark_start)
