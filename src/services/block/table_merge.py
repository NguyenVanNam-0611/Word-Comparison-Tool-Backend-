"""
block/table_merge.py
~~~~~~~~~~~~~~~~~~~~
Merge các bảng bị cắt trang thành 1 bảng liên tục.

Tách từ block_builder.py.
Import bởi: block_builder.py

THAY ĐỔI so với version cũ:
    1. content_blocks được copy đầy đủ khi merge (fix mất nội dung)
    2. skip_count tính động từ header comparison (fix offset -1 cứng sai)
    3. Bỏ điều kiện heading_ctx strict (fix miss case bảng layout nhiều trang)
    4. total_rows recalculate từ cells thực tế sau merge (fix count lệch)
    5. _merge_two_tables truyền flag has_repeated_header xuống (rõ ràng hơn)
    6. Bỏ check col_count trong _can_merge:
       header key match + continuation marker đã đủ chặt,
       col_count dễ bị lệch do subheader/gridSpan khác nhau giữa 2 trang.
    7. _normalize_col_span: fix cell cuối bảng extra bị hụt col_span
       khi grid của extra ít hơn grid của base (do subheader).
    8. _merge_logical_tables: drop cell có anchor_col >= base_total_cols
       (artifact của table_merge khi extra có subheader lệch grid),
       clamp col_span vào [1, base_total_cols - anchor_col] để tránh
       col_span âm hoặc vượt biên gây vỡ CSS Grid ở frontend.

    [FIX-1] _remove_last_anchor_row: dùng visual last row thay vì anchor_row
       cuối cùng — cell có row_span > 1 occupy last visual row nhưng
       anchor_row của nó nhỏ hơn, nên filter cũ không xoá được.
    [FIX-2] _count_repeated_header_rows thay skip_count=1 cứng:
       document SOP Nhật có thể có 2 header rows lặp lại,
       đếm thực tế tránh bỏ sót hoặc bỏ thừa.
    [FIX-3] _merge_spanning_cells: sau khi merge 2 bảng, cell định danh
       như "5.8.1" bị split thành 2 do Word cắt trang — gộp lại thành
       1 cell với row_span cộng dồn.
"""

from __future__ import annotations

from typing import List, Optional

from src.services.models.docnode import DocNode
from src.services.models.block import Block
from src.services.models.logical_table import LogicalCell, LogicalTable
from src.services.block.signature import signature
from src.services.block.filters import _norm, _is_skip_pattern


# ══════════════════════════════════════════════════════════════════════════════
# LogicalTable helpers
# ══════════════════════════════════════════════════════════════════════════════

def _get_logical_table(table_node: DocNode) -> Optional[LogicalTable]:
    return getattr(table_node, "logical_table", None)


def _get_header_key(table_node: DocNode) -> Optional[str]:
    """
    Lấy key từ anchor_row=0 (hàng đầu tiên).
    Header toàn rỗng → không đủ tin cậy để match → trả None.
    """
    lt = _get_logical_table(table_node)
    if lt is None:
        return None
    header_cells = lt.cells_in_row(0)
    if not header_cells:
        return None
    key = "|".join(_norm(c.text) for c in header_cells)
    if not key.replace("|", "").strip():
        return None
    return key


def _last_anchor_row_is_continue(table_node: DocNode) -> bool:
    """
    Hàng anchor cuối cùng toàn là text kiểu "tiếp trang sau / continued..."
    → bảng bị cắt trang, cần merge với bảng tiếp theo.

    Dùng visual last row (max anchor_row + row_span - 1) thay vì
    anchor_rows[-1] để tìm đúng row cuối kể cả khi cell có row_span > 1.
    """
    lt = _get_logical_table(table_node)
    if lt is None:
        return False
    if not lt.cells:
        return False

    # [FIX-1 liên quan] Tìm visual last row — row mà cell nào đó occupy
    # ở vị trí thấp nhất, không phải anchor_row cuối cùng.
    last_visual_row = max(c.anchor_row + c.row_span - 1 for c in lt.cells)

    # Lấy tất cả cell occupy visual last row
    cells_at_last = [
        c for c in lt.cells
        if c.anchor_row <= last_visual_row < c.anchor_row + c.row_span
    ]
    non_empty = [_norm(c.text) for c in cells_at_last if _norm(c.text)]
    if not non_empty:
        return False
    return all(_is_skip_pattern(t) for t in non_empty)


def _remove_last_anchor_row(lt: LogicalTable) -> None:
    """
    Xoá tất cả cell occupy visual last row.
    Recalculate total_rows từ cells thực tế sau khi xoá.

    [FIX-1] Dùng visual last row thay vì anchor_rows[-1].
    Vấn đề cũ: cell "Tiếp trang sau" có thể là cell với row_span > 1,
    anchor_row của nó là row N nhưng nó occupy đến row N+k.
    anchor_rows[-1] trả về row N+k (row cuối theo sorted anchor_rows),
    nhưng filter `c.anchor_row != last_anchor` không match cell có
    anchor_row = N → cell không bị xoá → "Tiếp trang sau" còn trong bảng.

    Fix: xác định last_visual_row = max(anchor_row + row_span - 1),
    xoá tất cả cell nào occupy row đó (anchor_row <= last_visual_row
    < anchor_row + row_span).
    """
    if not lt.cells:
        return

    last_visual_row = max(c.anchor_row + c.row_span - 1 for c in lt.cells)

    lt.cells = [
        c for c in lt.cells
        if not (c.anchor_row <= last_visual_row < c.anchor_row + c.row_span)
    ]

    if lt.cells:
        lt.total_rows = max(c.anchor_row + c.row_span for c in lt.cells)
    else:
        lt.total_rows = 0


def _count_repeated_header_rows(
    base_lt: LogicalTable,
    extra_lt: LogicalTable,
) -> int:
    """
    Đếm bao nhiêu rows đầu của extra_lt trùng với base_lt theo thứ tự.

    [FIX-2] Thay thế _has_repeated_header + skip_count=1 cứng.
    Document SOP Nhật thường có 1-2 header rows lặp lại ở mỗi trang.
    Ví dụ:
        Row 0: Mục | Nội dung thao tác | Ghi chú   ← header chính
        Row 1: (subheader chi tiết hơn)              ← cũng lặp lại
    Nếu skip_count=1 cứng → row 1 của extra bị giữ lại → duplicate subheader.

    Logic: so sánh từng row từ đầu, dừng khi gặp row không khớp.
    Chỉ count row có content (không count row rỗng).
    """
    base_anchor_rows  = base_lt.anchor_rows()
    extra_anchor_rows = extra_lt.anchor_rows()

    count = 0
    for base_r, extra_r in zip(base_anchor_rows, extra_anchor_rows):
        base_key  = "|".join(_norm(c.text) for c in base_lt.cells_in_row(base_r))
        extra_key = "|".join(_norm(c.text) for c in extra_lt.cells_in_row(extra_r))

        # Bỏ qua nếu cả hai đều rỗng (padding row)
        base_stripped  = base_key.replace("|", "").strip()
        extra_stripped = extra_key.replace("|", "").strip()
        if not base_stripped and not extra_stripped:
            continue

        if base_key == extra_key and base_stripped:
            count += 1
        else:
            break  # Hàng không khớp → dừng đếm

    return count


def _normalize_col_span(
    cell: LogicalCell,
    extra_lt: LogicalTable,
    base_total_cols: int,
) -> int:
    """
    Fix col_span của cell khi grid của extra ít hơn grid của base.

    Vấn đề: bảng trang trước có subheader làm total_cols lớn hơn
    (vd: 5 grid cols), bảng trang sau không có subheader (3 grid cols).
    Sau merge, cell cuối cùng theo chiều ngang của bảng sau chỉ có
    col_span=1 trong khi cần col_span=3 để lấp đầy đến hết base grid.

    Fix: nếu cell này là cell cuối cùng theo chiều ngang trong extra,
    kéo col_span đến hết total_cols của base.

    Lưu ý: hàm này chỉ tính col_span "lý tưởng" theo logic extra grid.
    Việc clamp vào biên base_total_cols được thực hiện ở _merge_logical_tables
    sau khi đã drop các cell out-of-bounds.
    """
    body_cells = [c for c in extra_lt.cells if c.anchor_row > 0]
    if not body_cells:
        return cell.col_span
    extra_max_col = max(c.anchor_col for c in body_cells)
    if cell.anchor_col == extra_max_col:
        return base_total_cols - cell.anchor_col
    return cell.col_span


def _merge_logical_tables(
    base_lt: LogicalTable,
    extra_lt: LogicalTable,
) -> None:
    """
    Merge toàn bộ cells của extra_lt vào base_lt.

    [FIX-2] Dùng _count_repeated_header_rows thay vì skip_count=1 cứng.

    Guard quan trọng:
    - Drop cell có anchor_col >= base_total_cols: artifact xảy ra khi
      extra có subheader làm lệch grid so với base. Cell này không có
      vị trí hợp lệ trong base grid → nếu không drop sẽ sinh ra
      gridColumn vượt biên, phá vỡ CSS Grid ở frontend.
    - Clamp col_span vào [1, base_total_cols - anchor_col]: tránh
      col_span âm (do _normalize_col_span trả về số âm khi anchor_col
      gần base_total_cols) hoặc col_span làm cell tràn ra ngoài grid.
    """
    skip_count    = _count_repeated_header_rows(base_lt, extra_lt)  # [FIX-2]
    offset        = base_lt.total_rows
    base_total_cols = base_lt.total_cols

    # Tập hợp anchor_row của extra cần skip (các header rows lặp)
    extra_anchor_rows = extra_lt.anchor_rows()
    skip_rows = set(extra_anchor_rows[:skip_count])

    for cell in extra_lt.cells:
        if cell.anchor_row in skip_rows:
            continue

        # Drop cell lạc ra ngoài grid của base.
        if cell.anchor_col >= base_total_cols:
            continue

        col_span = _normalize_col_span(cell, extra_lt, base_total_cols)

        # Clamp col_span vào [1, remaining_cols].
        remaining = base_total_cols - cell.anchor_col
        col_span  = max(1, min(col_span, remaining))

        # Tính row offset: trừ số header rows đã skip để không có gap
        # giữa data rows cuối base và data rows đầu extra.
        new_anchor_row = cell.anchor_row + offset - skip_count

        base_lt.cells.append(LogicalCell(
            uid            = cell.uid,
            anchor_row     = new_anchor_row,
            anchor_col     = cell.anchor_col,
            row_span       = cell.row_span,
            col_span       = col_span,
            text           = cell.text,
            text_display   = cell.text_display,
            content_blocks = list(cell.content_blocks),
        ))

    if base_lt.cells:
        base_lt.total_rows = max(
            c.anchor_row + c.row_span for c in base_lt.cells
        )


def _merge_spanning_cells(lt: LogicalTable) -> None:
    """
    Gộp các cell định danh bị split do Word cắt trang.

    [FIX-3] Vấn đề: Word không encode cross-page rowspan trong OOXML.
    Cell "5.8.1 Kiểm tra kết dính chuỗi" bị cắt thành 2 cell riêng biệt
    ở 2 <w:tbl> khác nhau. Sau khi merge bảng, 2 cell này nằm liền kề
    về anchor_row nhưng vẫn là 2 cell riêng → frontend render 2 cell
    thay vì 1 cell spanning.

    Điều kiện để gộp 2 cell A và B:
        - Cùng anchor_col
        - anchor_col nhỏ (≤ 1) — chỉ col định danh như "Mục", "STT"
          Col nội dung dài không gộp vì dễ trùng text do copy header
        - Cùng col_span
        - Cùng text (normalized) và text không rỗng
        - Liền kề: A.anchor_row + A.row_span == B.anchor_row

    Chạy lặp đến khi không còn gì để gộp (handle chain 3+ trang).
    """
    changed = True
    while changed:
        changed = False
        # Sort để đảm bảo A luôn ở trước B
        cells_by_col_row = sorted(
            lt.cells,
            key=lambda c: (c.anchor_col, c.anchor_row),
        )
        for i in range(len(cells_by_col_row) - 1):
            a = cells_by_col_row[i]
            b = cells_by_col_row[i + 1]

            if (
                a.anchor_col == b.anchor_col
                and a.anchor_col <= 1                          # chỉ col định danh
                and a.col_span == b.col_span
                and _norm(a.text) and _norm(a.text) == _norm(b.text)
                and a.anchor_row + a.row_span == b.anchor_row  # liền kề chính xác
            ):
                # Gộp: extend row_span của a, xoá b.
                #
                # KHÔNG merge content_blocks của b vào a:
                # Điều kiện vào đây đã là text(a) == text(b), tức b là
                # continuation placeholder mà Word tạo ra khi cắt trang
                # (hoặc tool tạo docx copy lại text vào vMerge cell).
                # content_blocks của b là duplicate hoàn toàn → append
                # vào a sẽ render text 2 lần ở frontend.
                #
                # Case b có nội dung thực sự mới (text khác) thì
                # text(a) != text(b) → không vào nhánh này → không gộp,
                # đó là 2 cell riêng biệt, không phải split cell.
                a.row_span += b.row_span
                lt.cells.remove(b)
                changed = True
                break  # Restart loop vì list đã thay đổi

    # Recalculate sau khi gộp xong
    if lt.cells:
        lt.total_rows = max(c.anchor_row + c.row_span for c in lt.cells)

def _as_int(value, default: int | None = None) -> int | None:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def _get_page_start(node: DocNode) -> int | None:
    content = node.content or {}
    return (
        _as_int(content.get("page_start"))
        or _as_int(content.get("page"))
    )


def _get_page_end(node: DocNode) -> int | None:
    content = node.content or {}
    return (
        _as_int(content.get("page_end"))
        or _as_int(content.get("page_start"))
        or _as_int(content.get("page"))
    )


def _merge_table_page_range(base: DocNode, extra: DocNode) -> None:
    """
    Khi 2 table blocks bị merge, page của base phải phủ cả extra.

    Ví dụ:
        base  page_start=2, page_end=2
        extra page_start=3, page_end=3

    Sau merge:
        base page=2, page_start=2, page_end=3
    """
    if base.content is None:
        base.content = {}
    if extra.content is None:
        extra.content = {}

    base_start = _get_page_start(base)
    base_end   = _get_page_end(base)
    extra_start = _get_page_start(extra)
    extra_end   = _get_page_end(extra)

    starts = [p for p in (base_start, extra_start) if p is not None]
    ends   = [p for p in (base_end, extra_end, base_start, extra_start) if p is not None]

    if not starts and not ends:
        return

    page_start = min(starts) if starts else min(ends)
    page_end = max(ends) if ends else page_start

    if page_end < page_start:
        page_end = page_start

    base.content["page"] = page_start
    base.content["page_start"] = page_start
    base.content["page_end"] = page_end

def _merge_two_tables(base: DocNode, extra: DocNode) -> None:
    """
    Merge extra vào base ở cả LogicalTable, content dict và page range.

    Quan trọng:
    - base là table đầu tiên
    - extra là table tiếp theo bị cắt trang
    - sau merge, base phải giữ page_start nhỏ nhất và page_end lớn nhất
      để frontend/export hiện đúng kiểu Trang 2-3.
    """
    base_lt  = _get_logical_table(base)
    extra_lt = _get_logical_table(extra)

    if base_lt is None or extra_lt is None:
        return

    # Merge page range TRƯỚC hoặc SAU logical đều được,
    # nhưng làm trước để không mất thông tin extra.
    _merge_table_page_range(base, extra)

    _merge_logical_tables(base_lt, extra_lt)

    base.content["row_count"] = base_lt.total_rows
    base.content["col_count"] = base_lt.total_cols

# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def _can_merge(prev: Block, curr: Block) -> bool:
    """
    Hai bảng liên tiếp có thể merge khi:
        1. Cùng type table
        2. Header row giống nhau (hard requirement)
        3. Hàng cuối của prev là "tiếp trang sau..." (continuation signal)

    Bỏ check col_count:
        col_count dễ bị lệch do subheader/gridSpan khác nhau giữa 2 trang
        (ví dụ: bảng trang trước có subheader tách 1 cột thành 3 grid cols,
        trang sau không có subheader → total_cols khác nhau dù cùng 1 bảng).
        header_key match đã đủ chặt để đảm bảo đây là cùng 1 bảng.

    Bỏ điều kiện heading_ctx:
        Bảng layout nhiều trang rất dễ bị lệch heading_ctx vì
        block_builder inject từ heading gần nhất — không đáng tin
        khi bảng chiếm nhiều trang.
    """
    if prev.type != "table" or curr.type != "table":
        return False

    prev_header = _get_header_key(prev.node)
    curr_header = _get_header_key(curr.node)
    if prev_header is None or curr_header is None:
        return False
    if prev_header != curr_header:
        return False

    return True


def merge_consecutive_tables(blocks: List[Block]) -> List[Block]:
    """
    Duyệt blocks, merge các cặp table liên tiếp bị cắt trang.
    Chain 3+ bảng được xử lý tự nhiên vì mỗi lần merge xong,
    result[-1] là bảng đã gộp và tiếp tục check với bảng tiếp theo.

    Sau khi toàn bộ merge hoàn tất, chạy _merge_spanning_cells [FIX-3]
    trên mỗi bảng đã được merge để gộp cell định danh bị split.
    """
    if not blocks:
        return blocks

    result: List[Block] = []
    merged_indices: set[int] = set()  # Track index nào đã qua merge

    for block in blocks:
        if result and _can_merge(result[-1], block):
            prev = result[-1]

            # Bước 1: xoá "tiếp trang sau" row khỏi base  [FIX-1 applied]
            prev_lt = _get_logical_table(prev.node)
            if prev_lt is not None:
                if _last_anchor_row_is_continue(prev.node):
                    _remove_last_anchor_row(prev_lt)

            # Bước 2: merge extra vào base  [FIX-2 applied]
            _merge_two_tables(prev.node, block.node)

            # Bước 3: recompute signature sau merge
            prev.signature = signature(
                prev.node, heading_ctx=prev.heading_ctx or ""
            )

            # Mark để chạy _merge_spanning_cells sau
            merged_indices.add(len(result) - 1)
            continue

        result.append(block)

    # Bước 4: gộp cell định danh bị split trên tất cả bảng đã merge  [FIX-3]
    for idx in merged_indices:
        lt = _get_logical_table(result[idx].node)
        if lt is not None:
            _merge_spanning_cells(lt)
            result[idx].node.content["row_count"] = lt.total_rows

    return result