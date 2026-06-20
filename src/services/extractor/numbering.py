from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
from docx.document import Document as _Document
from docx.oxml.ns import qn

NumberingMap = Dict[str, Dict[int, Dict[str, Any]]]
CounterState = Dict[Tuple[str, int], int]


def build_numbering_map(doc: _Document) -> NumberingMap:
    result: NumberingMap = {}
    try:
        numbering_part = doc.part.numbering_part
    except Exception:
        return result

    if numbering_part is None:
        return result

    root = numbering_part.element
    abstract_map: Dict[str, Dict[int, Dict[str, Any]]] = {}

    for abs_num in root.findall(qn("w:abstractNum")):
        abs_id = abs_num.get(qn("w:abstractNumId"))
        if abs_id is None:
            continue
        levels: Dict[int, Dict[str, Any]] = {}
        for lvl in abs_num.findall(qn("w:lvl")):
            ilvl_raw = lvl.get(qn("w:ilvl"))
            if ilvl_raw is None:
                continue
            try:
                ilvl = int(ilvl_raw)
            except ValueError:
                continue

            num_fmt_el  = lvl.find(qn("w:numFmt"))
            lvl_text_el = lvl.find(qn("w:lvlText"))
            start_el    = lvl.find(qn("w:start"))

            num_fmt  = num_fmt_el.get(qn("w:val"))  if num_fmt_el  is not None else None
            lvl_text = lvl_text_el.get(qn("w:val")) if lvl_text_el is not None else None
            start    = int(start_el.get(qn("w:val")) or 1) if start_el is not None else 1

            levels[ilvl] = {"num_fmt": num_fmt, "lvl_text": lvl_text, "start": start}
        abstract_map[str(abs_id)] = levels

    for num in root.findall(qn("w:num")):
        num_id = num.get(qn("w:numId"))
        if num_id is None:
            continue
        abs_ref = num.find(qn("w:abstractNumId"))
        if abs_ref is None:
            continue
        abs_id = abs_ref.get(qn("w:val"))
        if abs_id is None:
            continue
        result[str(num_id)] = abstract_map.get(str(abs_id), {})

    return result


def resolve_numbering(
    numbering_map: NumberingMap,
    num_id: Optional[str],
    level: Optional[int],
) -> Dict[str, Any]:
    if num_id is None:
        return {}
    lvl = int(level or 0)
    return numbering_map.get(str(num_id), {}).get(lvl, {}) or {}


# ── Counter helpers ────────────────────────────────────────────────────────

def _to_alpha(n: int, upper: bool = False) -> str:
    result = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        result = chr(ord("A" if upper else "a") + r) + result
    return result


def _to_roman(n: int, upper: bool = True) -> str:
    vals = [
        (1000,"M"),(900,"CM"),(500,"D"),(400,"CD"),
        (100,"C"),(90,"XC"),(50,"L"),(40,"XL"),
        (10,"X"),(9,"IX"),(5,"V"),(4,"IV"),(1,"I"),
    ]
    s = ""
    for v, sym in vals:
        while n >= v:
            s += sym
            n -= v
    return s if upper else s.lower()


def _render_counter(count: int, num_fmt: Optional[str]) -> str:
    fmt = (num_fmt or "decimal").lower()
    if fmt == "decimal":           return str(count)
    if fmt == "decimalzero":       return f"{count:02d}"
    if fmt == "upperletter":       return _to_alpha(count, upper=True)
    if fmt == "lowerletter":       return _to_alpha(count, upper=False)
    if fmt == "upperroman":        return _to_roman(count, upper=True)
    if fmt == "lowerroman":        return _to_roman(count, upper=False)
    return str(count)


def _expand_lvl_text(
    lvl_text: Optional[str],
    current_ilvl: int,
    counters: Dict[int, int],
    level_fmts: Dict[int, str],
) -> str:
    if not lvl_text:
        return ""
    result = lvl_text
    for ref in range(current_ilvl + 1, 0, -1):
        placeholder = f"%{ref}"
        if placeholder in result:
            ilvl_ref = ref - 1
            result = result.replace(
                placeholder,
                _render_counter(counters.get(ilvl_ref, 1), level_fmts.get(ilvl_ref, "decimal")),
            )
    return result


def compute_numbering_label(
    numbering_map: NumberingMap,
    counter_state: CounterState,
    num_id: Optional[str],
    level: Optional[int],
) -> str:
    """
    Tính label thực tế ("1.", "2.", "a)", "I.", "•" ...) và cập nhật counter_state.
    Gọi theo đúng thứ tự paragraph trong document.
    """
    if num_id is None:
        return ""

    ilvl       = int(level or 0)
    level_meta = numbering_map.get(str(num_id), {})
    if not level_meta:
        return ""

    meta = level_meta.get(ilvl, {})
    if not meta:
        return ""

    num_fmt  = meta.get("num_fmt", "decimal")
    lvl_text = meta.get("lvl_text", "%1.")
    start    = meta.get("start", 1)

    # Reset các level sâu hơn khi level hiện tại tiến lên
    for deeper in range(ilvl + 1, 9):
        counter_state.pop((num_id, deeper), None)

    key = (num_id, ilvl)
    if key not in counter_state:
        counter_state[key] = start
    else:
        counter_state[key] += 1

    if num_fmt in ("bullet", "none"):
        return lvl_text or "•"

    counters    = {l: counter_state.get((num_id, l), m.get("start", 1))
                   for l, m in level_meta.items()}
    level_fmts  = {l: (m.get("num_fmt") or "decimal")
                   for l, m in level_meta.items()}

    return _expand_lvl_text(lvl_text, ilvl, counters, level_fmts)