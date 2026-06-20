from __future__ import annotations

import logging
import time
from typing import Any, Dict

from src.services.align.sequence_align import align_blocks
from src.services.block.block_builder import build_blocks
from src.services.extractor.document import extract_doc_tree
from src.services.page import (
    collect_uid_index_pairs,
    get_pages_via_com_index,
    inject_pages,
)
from src.services.serializer.json_builder import build_ui_json

logger = logging.getLogger(__name__)


_DISPLAY_TYPE_MAP = {
    "paragraph_modified": "paragraph",
    "paragraph_inserted": "paragraph",
    "paragraph_deleted": "paragraph",
    "heading_modified": "heading",
    "heading_inserted": "heading",
    "heading_deleted": "heading",
    "table_modified": "table",
    "table_inserted": "table",
    "table_deleted": "table",
    "image_modified": "image",
    "image_inserted": "image",
    "image_deleted": "image",
    "image_added": "image",
    "shape_modified": "shape",
    "shape_inserted": "shape",
    "shape_deleted": "shape",
}


class DiffService:
    """
    DiffService
    ===========
    Quy ước quan trọng:
    - Thứ tự hiển thị MUST theo seq_index do json_builder sinh ra.
    - Không sort lại theo order/id trong service layer.
    """

    # ─────────────────────────────────────────────────────────────
    # Normalize helpers
    # ─────────────────────────────────────────────────────────────

    def _normalize_change(
        self,
        ch: Dict[str, Any],
        section_heading: str,
    ) -> Dict[str, Any]:
        ch_type = ch.get("type", "")

        display_type = (
            _DISPLAY_TYPE_MAP.get(ch_type)
            or ch.get("display_type")
            or ch_type.replace("_modified", "").replace("_inserted", "").replace("_deleted", "")
        )

        order = ch.get("order")
        if order is None:
            left = ch.get("left") or {}
            right = ch.get("right") or {}

            candidates = []
            if isinstance(left, dict):
                candidates.append(left.get("order"))
            if isinstance(right, dict):
                candidates.append(right.get("order"))

            order = min((o for o in candidates if o is not None), default=0)

        return {
            **ch,
            "heading": ch.get("heading") if ch.get("heading") is not None else section_heading,
            "left_heading": ch.get("left_heading"),
            "right_heading": ch.get("right_heading"),
            "heading_changed": ch.get("heading_changed", False),
            "order": order,
            "display_type": display_type,
        }

    def _normalize_section(self, section: Dict[str, Any]) -> Dict[str, Any]:
        heading = section.get("heading") or "(No heading)"
        changes = section.get("changes", []) or []

        return {
            "heading": section.get("heading"),
            "left_heading": section.get("left_heading"),
            "right_heading": section.get("right_heading"),
            "heading_changed": section.get("heading_changed", False),
            "changes": [self._normalize_change(ch, heading) for ch in changes],
        }

    # ─────────────────────────────────────────────────────────────
    # COM page injection
    # ─────────────────────────────────────────────────────────────

    @staticmethod
    def _inject_com_pages(docx_path: str, root: Any) -> None:
        """
        Lấy page thật bằng Word COM.

        Rule:
        - paragraph/heading ngoài table: page riêng theo para_idx
        - table top-level: page_start/page_end theo table_idx
        - row/cell/nested table/image: inherit từ parent
        - nếu COM lỗi: giữ page fallback từ extractor
        """
        try:
            pairs = collect_uid_index_pairs(root)
            if not pairs:
                return

            page_map = get_pages_via_com_index(docx_path, pairs)
            if not page_map:
                return

            inject_pages(root, page_map)

        except Exception as e:
            logger.warning(
                "[PAGE] COM page inject failed, fallback extractor page. " "file=%s, err=%s",
                docx_path,
                e,
            )

    # ─────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────

    def compare(self, original_path: str, modified_path: str) -> Dict[str, Any]:
        total_t0 = time.perf_counter()

        # 1. Extract DocNode trees
        t0 = time.perf_counter()
        original_root = extract_doc_tree(original_path)
        logger.info("[PERF] extract_original=%.3fs", time.perf_counter() - t0)

        t0 = time.perf_counter()
        modified_root = extract_doc_tree(modified_path)
        logger.info("[PERF] extract_modified=%.3fs", time.perf_counter() - t0)

        # 2. Inject COM page before build_blocks
        t0 = time.perf_counter()
        self._inject_com_pages(original_path, original_root)
        logger.info("[PERF] page_original=%.3fs", time.perf_counter() - t0)

        t0 = time.perf_counter()
        self._inject_com_pages(modified_path, modified_root)
        logger.info("[PERF] page_modified=%.3fs", time.perf_counter() - t0)

        # 3. Build blocks
        t0 = time.perf_counter()
        original_blocks = build_blocks(original_root)
        modified_blocks = build_blocks(modified_root)
        logger.info(
            "[PERF] build_blocks=%.3fs old_blocks=%s new_blocks=%s",
            time.perf_counter() - t0,
            len(original_blocks),
            len(modified_blocks),
        )

        # 4. Align blocks
        t0 = time.perf_counter()
        opcodes = align_blocks(original_blocks, modified_blocks)
        logger.info(
            "[PERF] align_blocks=%.3fs opcodes=%s",
            time.perf_counter() - t0,
            len(opcodes),
        )

        # 5. Build UI JSON
        t0 = time.perf_counter()
        result = build_ui_json(original_blocks, modified_blocks, opcodes)
        logger.info("[PERF] build_ui_json=%.3fs", time.perf_counter() - t0)

        sections = result.get("sections", []) or []

        # 6. Normalize sections
        t0 = time.perf_counter()
        normalized_sections = [self._normalize_section(section) for section in sections]
        logger.info("[PERF] normalize_sections=%.3fs", time.perf_counter() - t0)

        total_changes = sum(len(section["changes"]) for section in normalized_sections)

        logger.info(
            "[PERF] total_compare=%.3fs sections=%s changes=%s",
            time.perf_counter() - total_t0,
            len(normalized_sections),
            total_changes,
        )

        return {
            "original_file": original_path,
            "modified_file": modified_path,
            "total_sections": len(normalized_sections),
            "total_changes": total_changes,
            "sections": normalized_sections,
        }
