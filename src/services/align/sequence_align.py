"""
align/sequence_align.py
~~~~~~~~~~~~~~~~~~~~~~~
Align block list A (original) với block list B (modified) thành list opcodes.

Fixes so với cũ:
1.  HEADING SHIFT BUG
2.  CROSS-MATCH BUG
3.  _match_mixed_slice INDEX BUG
4.  N-N table pairing
5.  SIMILAR BLOCK DEDUP
6.  EMPTY SHAPE CONTENT SIG
7.  SHAPE WITH TEXT CONTENT SIG
8.  MIXED SLICE OPCODE ORDER BUG
9.  SHAPE IMAGE BUG
10. SHAPE SIMILARITY IMAGE BUG
11. MIXED SLICE DELETE/INSERT J/I BOUNDARY BUG
12. DELETE J COLLISION BUG
13. EQUAL INDEX SKEW
14. N-N TABLE BRANCH RE-INTRODUCES BUG #12
15. TABLE IDENTITY PAIRING — table không dùng signature similarity để pair nữa.
    Thay bằng table_pairing.py với score formula mềm (row_text_overlap,
    token_overlap, col_similarity, row_count_similarity, position_similarity).
    _rescue_adjacent_table_pairs chạy sau _match_mixed_slice để cứu
    các table delete/insert còn sót, dùng best-score thay vì greedy blind.
"""
from __future__ import annotations

import difflib
import hashlib
from typing import List, Set, Tuple

from src.services.block.signature import _split_heading
from src.services.extractor.utils import norm_for_signature as _norm_text
from src.services.models.block import Block
from src.services.extractor.shape.shape_content import collect_shape_parts
from src.services.utils.shape_sig import stable_empty_shape_id

Opcode = Tuple[str, int, int, int, int]


# ══════════════════════════════════════════════════════════════════════════════
# Shape content helpers
# ══════════════════════════════════════════════════════════════════════════════

def _shape_combined(block: Block) -> str:
    parts = collect_shape_parts(block.node)
    return " ".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# Heading context normalizer
# ══════════════════════════════════════════════════════════════════════════════

def _heading_ctx_body(heading_ctx: str) -> str:
    if not heading_ctx:
        return ""
    parts  = heading_ctx.split(" > ")
    bodies = []
    for part in parts:
        _, body = _split_heading(part)
        bodies.append(body)
    return " > ".join(bodies)

def _text_len_for_threshold(a: Block, b: Block | None = None) -> int:
    a_text = _norm_text(a.node.content.get("text", "")) if a else ""
    b_text = _norm_text(b.node.content.get("text", "")) if b else ""
    return max(len(a_text), len(b_text))
# ══════════════════════════════════════════════════════════════════════════════
# Similarity
# ══════════════════════════════════════════════════════════════════════════════

def _block_similarity(a: Block, b: Block) -> float:
    if a.type != b.type:
        return 0.0

    same_heading = a.heading_ctx == b.heading_ctx

    if a.type == "shape":
        a_parts    = collect_shape_parts(a.node)
        b_parts    = collect_shape_parts(b.node)
        a_combined = " ".join(a_parts)
        b_combined = " ".join(b_parts)

        if not a_parts and not b_parts:
            return 0.45 if same_heading else 0.05
        if not a_parts or not b_parts:
            return 0.45 if same_heading else 0.0

        ratio         = difflib.SequenceMatcher(a=a_combined, b=b_combined, autojunk=False).ratio()
        heading_bonus = 0.2 if same_heading else 0.0
        return min(1.0, ratio + heading_bonus)

    if a.type == "table":
        # Table similarity được xử lý riêng qua table_pairing.
        # Nhánh này chỉ còn được gọi từ _match_mixed_slice (N-N slice).
        # Dùng signature_list để giữ tương thích, kết quả chỉ dùng
        # để tham khảo — quyết định cuối vẫn qua _rescue_adjacent_table_pairs.
        a_lt = getattr(a.node, "logical_table", None)
        b_lt = getattr(b.node, "logical_table", None)
        if a_lt and b_lt:
            ratio = difflib.SequenceMatcher(
                None,
                a_lt.signature_list(),
                b_lt.signature_list(),
                autojunk=False,
            ).ratio()
        else:
            a_text = (a.signature or "").strip()
            b_text = (b.signature or "").strip()
            ratio  = difflib.SequenceMatcher(None, a_text, b_text, autojunk=False).ratio()
        return max(0.5, ratio) if same_heading else min(1.0, ratio + 0.15)

    heading_bonus = 0.08 if same_heading else 0.0
    a_text = _norm_text(a.node.content.get("text", ""))
    b_text = _norm_text(b.node.content.get("text", ""))
    ratio  = difflib.SequenceMatcher(a=a_text, b=b_text, autojunk=False).ratio()
    return min(1.0, ratio + heading_bonus)


# ══════════════════════════════════════════════════════════════════════════════
# Content-only signature
# ══════════════════════════════════════════════════════════════════════════════

def _content_sig(block: Block) -> str:
    t = block.type

    if t == "shape":
        parts    = collect_shape_parts(block.node)
        combined = " ".join(parts)
        ctx_body = _heading_ctx_body(block.heading_ctx or "")

        if not parts:
            sid = stable_empty_shape_id(
                uid        = getattr(block.node, "uid", None),
                parent_uid = getattr(block.node, "parent_uid", None),
                heading_ctx= ctx_body,
            )
            return f"shape|empty|{sid}|{ctx_body}"

        h          = hashlib.md5(combined.encode("utf-8")).hexdigest()[:16]
        part_count = len(parts)
        return f"shape|{h}|n{part_count}|{ctx_body}"

    if t == "heading":
        level           = int(block.node.content.get("level", 1) or 1)
        txt             = _norm_text(block.node.content.get("text", ""))
        chapter, body   = _split_heading(txt)
        chapter_depth   = len(chapter.split(".")) if chapter else 0
        h               = hashlib.md5(body.encode("utf-8")).hexdigest()[:12]
        return f"H{level}|d{chapter_depth}|{h}"

    return f"{t}|{block.signature or ''}"


# ══════════════════════════════════════════════════════════════════════════════
# Opcode sort key
# ══════════════════════════════════════════════════════════════════════════════

def _opcode_sort_key(op: Opcode) -> tuple:
    tag, i1, i2, j1, j2 = op

    tag_rank = {
        "delete": 0,
        "insert": 1,
        "replace": 2,
        "equal": 3,
    }.get(tag, 9)

    return (i1, j1, tag_rank)


# ══════════════════════════════════════════════════════════════════════════════
# Similarity threshold
# ══════════════════════════════════════════════════════════════════════════════

def _similarity_threshold(block_type: str, text_len: int = 0) -> float:
    if block_type == "heading":   return 0.72
    if block_type == "shape":     return 0.80
    if block_type == "table":     return 0.82

    if block_type == "paragraph":
        if text_len > 300:
            return 0.55
        if text_len > 120:
            return 0.6
        return 0.65

    return 0.75

# ══════════════════════════════════════════════════════════════════════════════
# Main aligner
# ══════════════════════════════════════════════════════════════════════════════

def align_blocks(a_blocks: List[Block], b_blocks: List[Block]) -> List[Opcode]:
    """
    Pass 1 — SequenceMatcher trên content_sig.
    Pass 2 — table_pairing cho 1-1 table replace bị defer.
    Pass 3 — _rescue_adjacent_table_pairs cho table delete/insert còn sót.
    Pass 4 — sort theo (i1, j1).
    """
    from src.services.block.table_pairing import resolve_table_opcodes

    a_sigs = [_content_sig(b) for b in a_blocks]
    b_sigs = [_content_sig(b) for b in b_blocks]

    sm          = difflib.SequenceMatcher(a=a_sigs, b=b_sigs, autojunk=False)
    raw_opcodes = sm.get_opcodes()
    refined: List[Opcode] = []

    # (i_idx, j_idx, old_block, new_block)
    pending_tables: List[Tuple[int, int, Block, Block]] = []

    for tag, i1, i2, j1, j2 in raw_opcodes:
        if tag == "equal":
            refined.append((tag, i1, i2, j1, j2))
            continue

        if tag != "replace":
            refined.append((tag, i1, i2, j1, j2))
            continue

        a_slice = a_blocks[i1:i2]
        b_slice = b_blocks[j1:j2]

        # ── 1-1 replace ───────────────────────────────────────────────────
        if len(a_slice) == 1 and len(b_slice) == 1:
            if a_slice[0].type == "table" and b_slice[0].type == "table":
                # defer — không dùng signature similarity cho table
                pending_tables.append((i1, j1, a_slice[0], b_slice[0]))
                continue

            similarity = _block_similarity(a_slice[0], b_slice[0])
            threshold = _similarity_threshold(
                a_slice[0].type,
                _text_len_for_threshold(a_slice[0], b_slice[0])
            )

            if similarity >= threshold:
                refined.append(("replace", i1, i2, j1, j2))
            else:
                refined.append(("delete", i1, i2, j1, j1))
                refined.append(("insert", i2, i2, j1, j2))
            continue

        # ── N-N slice ─────────────────────────────────────────────────────
        if len(a_slice) > 0 and len(b_slice) > 0:
            _match_mixed_slice(refined, a_blocks, b_blocks, i1, i2, j1, j2)
            continue

        refined.append((tag, i1, i2, j1, j2))

    # Pass 2 — giải quyết pending 1-1 table
    if pending_tables:
        refined.extend(
            resolve_table_opcodes(pending_tables, a_blocks, b_blocks)
        )

    # Pass 3 — cứu table delete/insert còn sót từ mixed slice
    refined = _rescue_adjacent_table_pairs(refined, a_blocks, b_blocks)

    refined.sort(key=_opcode_sort_key)
    return refined


# ══════════════════════════════════════════════════════════════════════════════
# Rescue pass
# ══════════════════════════════════════════════════════════════════════════════

def _rescue_adjacent_table_pairs(
    opcodes: List[Opcode],
    a_blocks: List[Block],
    b_blocks: List[Block],
) -> List[Opcode]:
    """
    Sau _match_mixed_slice, các table delete (j1==j2) và insert (i1==i2)
    chưa được pair qua table_pairing.

    Với mỗi delete table, tìm insert table có score cao nhất.
    Chỉ pair nếu score >= MATCH_THRESHOLD.
    """
    from src.services.block.table_pairing import (
        resolve_table_opcodes,
        table_similarity,
        MATCH_THRESHOLD,
    )
    from src.services.block.table_identity import extract_table_identity

    delete_tables = [
        (idx, op) for idx, op in enumerate(opcodes)
        if op[0] == "delete"
        and op[1] < len(a_blocks)
        and a_blocks[op[1]].type == "table"
        and op[3] == op[4]          # j1 == j2 → chưa pair
    ]
    insert_tables = [
        (idx, op) for idx, op in enumerate(opcodes)
        if op[0] == "insert"
        and op[3] < len(b_blocks)
        and b_blocks[op[3]].type == "table"
        and op[1] == op[2]          # i1 == i2 → chưa pair
    ]

    if not delete_tables or not insert_tables:
        return opcodes

    total_blocks = len(a_blocks) + len(b_blocks)
    pending:        List[Tuple[int, int, Block, Block]] = []
    remove_indices: set = set()
    used_insert:    set = set()

    for d_idx, d_op in delete_tables:
        i_idx      = d_op[1]
        best_score = -1.0
        best_ins_pos  = -1
        best_ins_idx  = -1
        best_j_idx    = -1

        id_a = extract_table_identity(a_blocks[i_idx], i_idx)

        for ins_pos, (ins_idx, ins_op) in enumerate(insert_tables):
            if ins_pos in used_insert:
                continue
            j_idx = ins_op[3]
            id_b  = extract_table_identity(b_blocks[j_idx], j_idx)
            score = table_similarity(id_a, id_b, total_blocks)

            if score > best_score:
                best_score   = score
                best_ins_pos = ins_pos
                best_ins_idx = ins_idx
                best_j_idx   = j_idx

        if best_score >= MATCH_THRESHOLD:
            pending.append((i_idx, best_j_idx, a_blocks[i_idx], b_blocks[best_j_idx]))
            used_insert.add(best_ins_pos)
            remove_indices.add(d_idx)
            remove_indices.add(best_ins_idx)

    if not pending:
        return opcodes

    new_opcodes   = [op for idx, op in enumerate(opcodes) if idx not in remove_indices]
    table_opcodes = resolve_table_opcodes(pending, a_blocks, b_blocks)
    new_opcodes.extend(table_opcodes)
    return new_opcodes


# ══════════════════════════════════════════════════════════════════════════════
# Mixed slice
# ══════════════════════════════════════════════════════════════════════════════

def _match_mixed_slice(
    refined: List[Opcode],
    a_blocks: List[Block],
    b_blocks: List[Block],
    i1: int, i2: int,
    j1: int, j2: int,
) -> None:
    a_slice = a_blocks[i1:i2]
    b_slice = b_blocks[j1:j2]

    matched_a: Set[int] = set()
    matched_b: Set[int] = set()
    pairs: List[Tuple[int, int]] = []

    # ── Bước 1: exact match theo thứ tự vị trí ───────────────────────────
    a_sigs = [_content_sig(a) for a in a_slice]
    b_sigs = [_content_sig(b) for b in b_slice]

    sub_sm = difflib.SequenceMatcher(a=a_sigs, b=b_sigs, autojunk=False)
    for sub_tag, si1, si2, sj1, sj2 in sub_sm.get_opcodes():
        if sub_tag != "equal":
            continue
        for offset in range(si2 - si1):
            ki = si1 + offset
            kj = sj1 + offset
            if ki not in matched_a and kj not in matched_b:
                matched_a.add(ki)
                matched_b.add(kj)
                pairs.append((ki, kj))

    # ── Bước 2: greedy similarity cho phần còn lại ───────────────────────
    candidates: List[Tuple[float, int, int]] = []

    for ki, a in enumerate(a_slice):
        if ki in matched_a:
            continue

        for kj, b in enumerate(b_slice):
            if kj in matched_b:
                continue

            sim = _block_similarity(a, b)
            threshold = _similarity_threshold(
                a.type,
                _text_len_for_threshold(a, b)
            )

            if sim >= threshold:
                candidates.append((sim, ki, kj))

    candidates.sort(key=lambda item: (-item[0], abs(item[1] - item[2]), item[1], item[2]))

    for sim, ki, kj in candidates:
        if ki in matched_a or kj in matched_b:
            continue
        matched_a.add(ki)
        matched_b.add(kj)
        pairs.append((ki, kj))

    # ── Emit opcodes ──────────────────────────────────────────────────────
    pair_by_ki = {ki: kj for ki, kj in pairs}
    paired_kj  = set(pair_by_ki.values())

    for ki in range(len(a_slice)):
        ai = i1 + ki
        if ki in pair_by_ki:
            kj    = pair_by_ki[ki]
            bj    = j1 + kj
            a_sig = _content_sig(a_slice[ki])
            b_sig = _content_sig(b_slice[kj])
            if a_sig == b_sig:
                refined.append(("equal",   ai, ai + 1, bj, bj + 1))
            else:
                refined.append(("replace", ai, ai + 1, bj, bj + 1))
        else:
            # delete: j1 == j2 == j2_slice — fix #12
            refined.append(("delete", ai, ai + 1, j2, j2))

    pairs_sorted_by_j = sorted(pairs, key=lambda x: x[1])

    for kj in sorted(kj for kj in range(len(b_slice)) if kj not in paired_kj):
        anchor_i = i2

        for pair_ki, pair_kj in pairs_sorted_by_j:
            if kj < pair_kj:
                anchor_i = i1 + pair_ki
                break
            if pair_kj < kj:
                anchor_i = i1 + pair_ki + 1

        refined.append((
            "insert",
            anchor_i,
            anchor_i,
            j1 + kj,
            j1 + kj + 1,
        ))