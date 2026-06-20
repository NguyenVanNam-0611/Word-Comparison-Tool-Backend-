"""
block/table_pairing.py
~~~~~~~~~~~~~~~~~~~~~~
Tính similarity score giữa 2 table, quyết định matched hay delete/insert.

Score formula:
    0.45 * row_text_overlap
    0.20 * token_overlap
    0.15 * col_similarity
    0.10 * row_count_similarity
    0.10 * position_similarity
    +0.10 heading bonus (nếu cùng heading_ctx)

Threshold: 0.45
"""
from __future__ import annotations

from typing import List, Tuple

from src.services.models.block import Block
from src.services.block.table_identity import TableIdentity, extract_table_identity

Opcode = Tuple[str, int, int, int, int]

MATCH_THRESHOLD = 0.45


# ══════════════════════════════════════════════════════════════════════════════
# Similarity components
# ══════════════════════════════════════════════════════════════════════════════

def _row_text_overlap(a: TableIdentity, b: TableIdentity) -> float:
    """
    Tỉ lệ row text của a xuất hiện trong b theo nội dung, không theo vị trí.
    Dùng set để không bị ảnh hưởng bởi thứ tự row.
    """
    a_set = {t for t in a.row_texts if t.strip()}
    b_set = {t for t in b.row_texts if t.strip()}
    if not a_set:
        return 0.0
    return len(a_set & b_set) / max(len(a_set), len(b_set))


def _token_overlap(a: TableIdentity, b: TableIdentity) -> float:
    if not a.token_set or not b.token_set:
        return 0.0
    union = len(a.token_set | b.token_set)
    return len(a.token_set & b.token_set) / union if union else 0.0


def _col_similarity(a: TableIdentity, b: TableIdentity) -> float:
    if a.total_cols == 0 and b.total_cols == 0:
        return 1.0
    if a.total_cols == 0 or b.total_cols == 0:
        return 0.0
    # lệch 0 → 1.0 | lệch 1 → 0.75 | lệch 2 → 0.5 | lệch 3+ → 0.0
    return max(0.0, 1.0 - abs(a.total_cols - b.total_cols) * 0.25)


def _row_count_similarity(a: TableIdentity, b: TableIdentity) -> float:
    max_rows = max(a.total_rows, b.total_rows)
    if max_rows == 0:
        return 1.0
    return max(0.0, 1.0 - abs(a.total_rows - b.total_rows) / max_rows)


def _position_similarity(a: TableIdentity, b: TableIdentity, total_blocks: int) -> float:
    if total_blocks <= 1:
        return 1.0
    return max(0.0, 1.0 - abs(a.section_index - b.section_index) / total_blocks)


def _heading_bonus(a: TableIdentity, b: TableIdentity) -> float:
    if not a.heading_ctx or not b.heading_ctx:
        return 0.0
    return 0.1 if a.heading_ctx == b.heading_ctx else 0.0


# ══════════════════════════════════════════════════════════════════════════════
# Main scorer
# ══════════════════════════════════════════════════════════════════════════════

def table_similarity(
    a: TableIdentity,
    b: TableIdentity,
    total_blocks: int = 100,
) -> float:
    score = (
        0.45 * _row_text_overlap(a, b)
        + 0.20 * _token_overlap(a, b)
        + 0.15 * _col_similarity(a, b)
        + 0.10 * _row_count_similarity(a, b)
        + 0.10 * _position_similarity(a, b, total_blocks)
    )
    return min(1.0, score + _heading_bonus(a, b))


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def resolve_table_opcodes(
    pending: List[Tuple[int, int, Block, Block]],
    original_blocks: List[Block],
    modified_blocks: List[Block],
) -> List[Opcode]:
    """
    Nhận danh sách pending table pairs, trả về opcodes đã giải quyết.

    - score >= MATCH_THRESHOLD → replace (table_diff xử lý tiếp)
    - score <  MATCH_THRESHOLD → delete + insert riêng
    """
    total_blocks = len(original_blocks) + len(modified_blocks)
    result: List[Opcode] = []

    for i_idx, j_idx, old_block, new_block in pending:
        id_a  = extract_table_identity(old_block, i_idx)
        id_b  = extract_table_identity(new_block, j_idx)
        score = table_similarity(id_a, id_b, total_blocks)

        if score >= MATCH_THRESHOLD:
            result.append(("replace", i_idx, i_idx + 1, j_idx, j_idx + 1))
        else:
            result.append(("delete", i_idx, i_idx + 1, j_idx,     j_idx    ))
            result.append(("insert", i_idx + 1, i_idx + 1, j_idx, j_idx + 1))

    return result