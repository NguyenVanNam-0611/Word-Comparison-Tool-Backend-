"""
page/com_pager.py
~~~~~~~~~~~~~~~~~
Dùng Word COM để lấy page thật.

Chiến lược:
- paragraph/heading : chèn bookmark vào temp docx theo para_idx rồi lấy page từ Bookmark.Range
- table             : doc.Tables(idx).Range -> page_start/page_end
- shape             : doc.Shapes(...).Anchor -> page
- image             : không lấy riêng, inherit từ parent ở page_injector

Không lấy page row/cell/nested table để tránh sai với merged/nested table
và tránh chậm/crash COM.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any, Dict, List, Tuple

from src.services.page.bookmark_marker import (
    cleanup_temp_docx,
    create_temp_docx_with_para_bookmarks,
)

logger = logging.getLogger(__name__)

_WD_ACTIVE_END_PAGE_NUMBER = 3
_WD_COLLAPSE_START = 1
_WD_COLLAPSE_END = 0

PageInfo = Dict[str, int]
PageMap = Dict[str, PageInfo]


def get_pages_via_com_index(
    docx_path: str,
    uid_index_pairs: List[Tuple[str, str, Any]],
) -> PageMap:
    """
    Lấy page number bằng COM.

    Input:
        uid_index_pairs: List[(uid, obj_type, idx_or_id)]
            obj_type = "para"  -> idx là body para_idx từ extractor/document.py
            obj_type = "table" -> idx là int, dùng doc.Tables(idx)
            obj_type = "shape" -> idx/id/name, dùng doc.Shapes map

    Output:
        {
            uid_para:  {"page": 1, "page_start": 1, "page_end": 1},
            uid_table: {"page": 2, "page_start": 2, "page_end": 3},
            uid_shape: {"page": 4, "page_start": 4, "page_end": 4},
        }
    """
    if sys.platform != "win32":
        logger.warning("[COM] Không phải Windows — bỏ qua COM pager.")
        return {}

    if not uid_index_pairs:
        return {}

    try:
        import pythoncom
        import win32com.client  # noqa: F401
    except ImportError:
        logger.warning("[COM] pywin32 chưa cài — bỏ qua COM pager.")
        return {}

    abs_path = os.path.normpath(os.path.abspath(str(docx_path)))

    if not os.path.exists(abs_path):
        logger.error("[COM] File không tồn tại: %s", abs_path)
        return {}

    word = None
    doc = None
    result: PageMap = {}
    com_initialized = False

    marked_docx_path: str | None = None
    uid_to_bookmark: Dict[str, str] = {}

    try:
        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        com_initialized = True

        import win32com.client

        para_targets: List[Tuple[str, int]] = []
        need_shape_map = False

        for uid, obj_type, idx_or_id in uid_index_pairs:
            if obj_type == "para":
                try:
                    para_targets.append((uid, int(idx_or_id)))
                except Exception:
                    pass
            elif obj_type == "shape":
                need_shape_map = True

        # Nếu có paragraph cần page, tạo temp docx có bookmark.
        # Nếu không có paragraph, mở file gốc như cũ.
        open_path = abs_path

        if para_targets:
            marked_docx_path, uid_to_bookmark = create_temp_docx_with_para_bookmarks(
                abs_path,
                para_targets,
            )
            if marked_docx_path:
                open_path = os.path.normpath(os.path.abspath(marked_docx_path))
            else:
                logger.warning("[COM] Không tạo được bookmark temp docx cho para targets.")

        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0

        doc = word.Documents.Open(
            open_path,
            ReadOnly=True,
            AddToRecentFiles=False,
            ConfirmConversions=False,
        )

        # Không ép Repaginate ở đầu vì file lớn dễ treo COM.
        # Word vẫn có thể trả page khi gọi Range.Information.
        # try:
        #     doc.Repaginate()
        # except Exception:
        #     pass

        table_count = int(doc.Tables.Count)
        shape_count = int(doc.Shapes.Count)

        shape_id_map = _build_shape_id_map(doc, shape_count) if need_shape_map else {}

        logger.info(
            "[COM] counts: table=%s, shape=%s, pairs=%s, para_bookmarks=%s",
            table_count,
            shape_count,
            len(uid_index_pairs),
            len(uid_to_bookmark),
        )

        page_loop_start = time.time()
        MAX_PAGE_SECONDS = 90

        for uid, obj_type, idx_or_id in uid_index_pairs:
            if time.time() - page_loop_start > MAX_PAGE_SECONDS:
                logger.warning(
                    "[COM] timeout mềm khi lấy page, dừng tại result=%s/%s",
                    len(result),
                    len(uid_index_pairs),
                )
                break

            try:
                if obj_type == "para":
                    bm_name = uid_to_bookmark.get(uid)
                    if not bm_name:
                        logger.debug("[COM] para uid=%s không có bookmark", uid)
                        continue

                    try:
                        bm = doc.Bookmarks(bm_name)
                    except Exception:
                        logger.debug(
                            "[COM] bookmark không tồn tại: uid=%s name=%s",
                            uid,
                            bm_name,
                        )
                        continue

                    rng = bm.Range
                    page = _page_of_range_start(rng)

                    logger.info(
                        "[COM_PARA_BM] uid=%s bookmark=%s page=%s",
                        uid,
                        bm_name,
                        page,
                    )

                    if page:
                        result[uid] = {
                            "page": page,
                            "page_start": page,
                            "page_end": page,
                        }

                elif obj_type == "table":
                    idx = int(idx_or_id)
                    if idx < 1 or idx > table_count:
                        logger.debug(
                            "[COM] table idx=%s out of range max=%s",
                            idx,
                            table_count,
                        )
                        continue

                    tbl = doc.Tables(idx)
                    page_start, page_end = _page_range_of_table(tbl)

                    if page_start:
                        if not page_end:
                            page_end = page_start

                        if page_end < page_start:
                            page_end = page_start

                        result[uid] = {
                            "page": page_start,
                            "page_start": page_start,
                            "page_end": page_end,
                        }

                elif obj_type == "shape":
                    com_idx = shape_id_map.get(str(idx_or_id))
                    if com_idx is None:
                        logger.debug("[COM] shape id/name=%s không tìm thấy", idx_or_id)
                        continue

                    shp = doc.Shapes(com_idx)
                    rng = shp.Anchor
                    page = _page_of_range_start(rng)

                    if page:
                        result[uid] = {
                            "page": page,
                            "page_start": page,
                            "page_end": page,
                        }

                else:
                    continue

            except Exception as e:
                logger.debug(
                    "[COM] uid=%s obj_type=%s idx=%s failed: %s",
                    uid,
                    obj_type,
                    idx_or_id,
                    e,
                )
                continue

    except Exception as e:
        logger.error("[COM] Word COM error: %s", e)

    finally:
        try:
            if doc is not None:
                doc.Close(False)
        except Exception:
            pass

        try:
            if word is not None:
                word.Quit()
        except Exception:
            pass

        if com_initialized:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

        cleanup_temp_docx(marked_docx_path)

    logger.info(
        "[COM] Bookmark/index-based: lấy page cho %s/%s blocks.",
        len(result),
        len(uid_index_pairs),
    )
    return result


def _build_shape_id_map(doc, shape_count: int) -> Dict[str, int]:
    """
    Build map để tìm shape theo ID hoặc Name.
    """
    shape_id_map: Dict[str, int] = {}

    for si in range(1, shape_count + 1):
        try:
            shp = doc.Shapes(si)

            sid_num = str(shp.ID)
            sid_name = str(shp.Name)

            if sid_num:
                shape_id_map[sid_num] = si
            if sid_name:
                shape_id_map[sid_name] = si

        except Exception:
            continue

    return shape_id_map


def _page_of_range_start(rng) -> int | None:
    """
    Lấy page tại đầu range.
    """
    if rng is None:
        return None

    try:
        dup = rng.Duplicate
        dup.Collapse(_WD_COLLAPSE_START)
        return int(dup.Information(_WD_ACTIVE_END_PAGE_NUMBER))
    except Exception:
        try:
            return int(rng.Information(_WD_ACTIVE_END_PAGE_NUMBER))
        except Exception:
            return None


def _page_of_range_end(rng) -> int | None:
    """
    Lấy page tại cuối range.

    Với Word table.Range, CollapseEnd có thể rơi ra sau bảng.
    Vì vậy lùi End lại 1 character trước khi collapse để lấy page
    của nội dung cuối cùng thuộc bảng.
    """
    if rng is None:
        return None

    try:
        dup = rng.Duplicate

        # Tránh lấy page của paragraph ngay sau bảng.
        if dup.End > dup.Start:
            dup.End = dup.End - 1

        dup.Collapse(_WD_COLLAPSE_END)
        return int(dup.Information(_WD_ACTIVE_END_PAGE_NUMBER))

    except Exception:
        try:
            return int(rng.Information(_WD_ACTIVE_END_PAGE_NUMBER))
        except Exception:
            return None


def _page_range_of_table(tbl) -> Tuple[int | None, int | None]:
    """
    Table chỉ lấy page_start/page_end của Range bảng.
    Không gọi Row.Range/Cell.Range.
    """
    try:
        rng = tbl.Range
    except Exception:
        return None, None

    page_start = _page_of_range_start(rng)
    page_end = _page_of_range_end(rng)

    return page_start, page_end
