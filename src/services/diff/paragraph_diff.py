# src/services/diff/paragraph_diff.py
"""
So sánh 2 đoạn text ở mức word.

Nguyên tắc:
    - Numbering KHÔNG coi là text.
    - Text diff chỉ xử lý nội dung chữ.
    - Numbering/list change trả riêng trong format_changes.
    - Không dùng char-level diff.
"""

from __future__ import annotations

import difflib
from typing import Any, Dict, List, Optional

from src.services.extractor.utils import (
    norm_for_diff,
    norm_for_align,
    _detokenize,
)


_PUNCT_NO_SPACE_BEFORE = {",", ";", ".", "!", "?", ":", ")"}
_OPEN_NO_SPACE_AFTER = {"("}


def _needs_space_before(prev_tokens: List[str], cur_token: str) -> bool:
    if not prev_tokens:
        return False
    if cur_token in _PUNCT_NO_SPACE_BEFORE:
        return False
    if prev_tokens[-1] in _OPEN_NO_SPACE_AFTER:
        return False
    return True


def numbering_changed(
    old_numbering: Optional[dict],
    new_numbering: Optional[dict],
) -> bool:
    return (old_numbering or None) != (new_numbering or None)


def _numbering_meta(
    old_numbering: Optional[dict],
    new_numbering: Optional[dict],
) -> Dict[str, Any]:
    return {
        "changed": numbering_changed(old_numbering, new_numbering),
        "old": old_numbering,
        "new": new_numbering,
    }


def _result(
    *,
    old_raw: str,
    new_raw: str,
    spans: List[Dict[str, Any]],
    old_numbering: Optional[dict] = None,
    new_numbering: Optional[dict] = None,
) -> Dict[str, Any]:
    return {
        "old_full_text": old_raw,
        "new_full_text": new_raw,
        "spans": spans,
        "format_changes": {
            "numbering": _numbering_meta(old_numbering, new_numbering),
        },
    }


def diff_words(
    old_text: str,
    new_text: str,
    old_display: Optional[str] = None,
    new_display: Optional[str] = None,
    old_numbering: Optional[dict] = None,
    new_numbering: Optional[dict] = None,
) -> Dict[str, Any]:
    old_norm = norm_for_diff(old_text or "")
    new_norm = norm_for_diff(new_text or "")

    old_raw = old_display if old_display is not None else (old_text or "")
    new_raw = new_display if new_display is not None else (new_text or "")

    if "\n" in old_raw or "\n" in new_raw:
        result = _diff_words_multiline(
            old_text=old_text,
            new_text=new_text,
            old_raw=old_raw,
            new_raw=new_raw,
        )
        result["format_changes"] = {
            "numbering": _numbering_meta(old_numbering, new_numbering),
        }
        return result

    if not old_norm and not new_norm:
        return _result(
            old_raw=old_raw,
            new_raw=new_raw,
            spans=[],
            old_numbering=old_numbering,
            new_numbering=new_numbering,
        )

    if old_norm == new_norm:
        return _result(
            old_raw=old_raw,
            new_raw=new_raw,
            spans=[{
                "type": "equal",
                "action": "equal",
                "side": "both",
                "old_text": old_raw,
                "new_text": new_raw,
                "text": old_raw,
                "space_before": False,
            }],
            old_numbering=old_numbering,
            new_numbering=new_numbering,
        )

    old_tokens = old_norm.split()
    new_tokens = new_norm.split()

    sm = difflib.SequenceMatcher(
        a=old_tokens,
        b=new_tokens,
        autojunk=False,
    )

    spans: List[Dict[str, Any]] = []

    prev_old_tokens: List[str] = []
    prev_new_tokens: List[str] = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        old_words = old_tokens[i1:i2]
        new_words = new_tokens[j1:j2]

        old_segment = _detokenize(old_words)
        new_segment = _detokenize(new_words)

        old_first = old_words[0] if old_words else ""
        new_first = new_words[0] if new_words else ""

        if tag == "equal":
            if old_segment:
                spans.append({
                    "type": "equal",
                    "action": "equal",
                    "side": "both",
                    "old_text": old_segment,
                    "new_text": old_segment,
                    "text": old_segment,
                    "space_before": _needs_space_before(prev_old_tokens, old_first),
                })
                prev_old_tokens = old_words
                prev_new_tokens = new_words
            continue

        if tag == "delete":
            if old_segment:
                spans.append({
                    "type": "delete",
                    "action": "delete",
                    "side": "left",
                    "old_text": old_segment,
                    "new_text": "",
                    "text": old_segment,
                    "space_before": _needs_space_before(prev_old_tokens, old_first),
                })
                prev_old_tokens = old_words
            continue

        if tag == "insert":
            if new_segment:
                spans.append({
                    "type": "insert",
                    "action": "insert",
                    "side": "right",
                    "old_text": "",
                    "new_text": new_segment,
                    "text": new_segment,
                    "space_before": _needs_space_before(prev_new_tokens, new_first),
                })
                prev_new_tokens = new_words
            continue

        if tag == "replace":
            space_before = _needs_space_before(
                prev_old_tokens if old_first else prev_new_tokens,
                old_first or new_first,
            )

            spans.append({
                "type": "replace",
                "action": "replace",
                "side": "both",
                "old_text": old_segment,
                "new_text": new_segment,
                "text": old_segment,
                "space_before": space_before,
            })

            if old_words:
                prev_old_tokens = old_words
            if new_words:
                prev_new_tokens = new_words

    return _result(
        old_raw=old_raw,
        new_raw=new_raw,
        spans=spans,
        old_numbering=old_numbering,
        new_numbering=new_numbering,
    )


def _diff_one_line_pair(
    ol: str,
    nl: str,
    on: str,
    nn: str,
    all_spans: List[Dict[str, Any]],
) -> None:
    if on == nn:
        all_spans.append({
            "type": "equal",
            "action": "equal",
            "side": "both",
            "old_text": ol,
            "new_text": nl,
            "text": ol,
            "space_before": False,
        })
        return

    line_wd = diff_words(
        old_text=ol,
        new_text=nl,
        old_display=ol,
        new_display=nl,
    )

    line_spans = line_wd.get("spans", [])

    if line_spans:
        all_spans.extend(line_spans)
    else:
        all_spans.append({
            "type": "replace",
            "action": "replace",
            "side": "both",
            "old_text": ol,
            "new_text": nl,
            "text": ol,
            "space_before": False,
        })


def _diff_words_multiline(
    old_text: str,
    new_text: str,
    old_raw: str,
    new_raw: str,
) -> Dict[str, Any]:
    old_lines = old_raw.split("\n")
    new_lines = new_raw.split("\n")

    old_norms = [norm_for_align(line) for line in old_lines]
    new_norms = [norm_for_align(line) for line in new_lines]

    sm = difflib.SequenceMatcher(
        a=old_norms,
        b=new_norms,
        autojunk=False,
    )

    all_spans: List[Dict[str, Any]] = []

    def _add_newline() -> None:
        if all_spans:
            all_spans.append({"type": "newline"})

    for tag, i1, i2, j1, j2 in sm.get_opcodes():

        if tag == "equal":
            for i in range(i1, i2):
                _add_newline()
                line = old_lines[i]
                all_spans.append({
                    "type": "equal",
                    "action": "equal",
                    "side": "both",
                    "old_text": line,
                    "new_text": line,
                    "text": line,
                    "space_before": False,
                })

        elif tag == "delete":
            for i in range(i1, i2):
                _add_newline()
                line = old_lines[i]
                all_spans.append({
                    "type": "delete",
                    "action": "delete",
                    "side": "left",
                    "old_text": line,
                    "new_text": "",
                    "text": line,
                    "space_before": False,
                })

        elif tag == "insert":
            for j in range(j1, j2):
                _add_newline()
                line = new_lines[j]
                all_spans.append({
                    "type": "insert",
                    "action": "insert",
                    "side": "right",
                    "old_text": "",
                    "new_text": line,
                    "text": line,
                    "space_before": False,
                })

        elif tag == "replace":
            old_block = old_lines[i1:i2]
            new_block = new_lines[j1:j2]
            old_norm_block = old_norms[i1:i2]
            new_norm_block = new_norms[j1:j2]

            inner_sm = difflib.SequenceMatcher(
                a=old_norm_block,
                b=new_norm_block,
                autojunk=False,
            )

            for itag, ii1, ii2, ij1, ij2 in inner_sm.get_opcodes():

                if itag == "equal":
                    for k in range(ii1, ii2):
                        _add_newline()
                        line = old_block[k]
                        all_spans.append({
                            "type": "equal",
                            "action": "equal",
                            "side": "both",
                            "old_text": line,
                            "new_text": line,
                            "text": line,
                            "space_before": False,
                        })

                elif itag == "delete":
                    for k in range(ii1, ii2):
                        _add_newline()
                        line = old_block[k]
                        all_spans.append({
                            "type": "delete",
                            "action": "delete",
                            "side": "left",
                            "old_text": line,
                            "new_text": "",
                            "text": line,
                            "space_before": False,
                        })

                elif itag == "insert":
                    for k in range(ij1, ij2):
                        _add_newline()
                        line = new_block[k]
                        all_spans.append({
                            "type": "insert",
                            "action": "insert",
                            "side": "right",
                            "old_text": "",
                            "new_text": line,
                            "text": line,
                            "space_before": False,
                        })

                elif itag == "replace":
                    pair_count = min(ii2 - ii1, ij2 - ij1)

                    for k in range(pair_count):
                        _add_newline()
                        _diff_one_line_pair(
                            ol=old_block[ii1 + k],
                            nl=new_block[ij1 + k],
                            on=old_norm_block[ii1 + k],
                            nn=new_norm_block[ij1 + k],
                            all_spans=all_spans,
                        )

                    for k in range(ii1 + pair_count, ii2):
                        _add_newline()
                        line = old_block[k]
                        all_spans.append({
                            "type": "delete",
                            "action": "delete",
                            "side": "left",
                            "old_text": line,
                            "new_text": "",
                            "text": line,
                            "space_before": False,
                        })

                    for k in range(ij1 + pair_count, ij2):
                        _add_newline()
                        line = new_block[k]
                        all_spans.append({
                            "type": "insert",
                            "action": "insert",
                            "side": "right",
                            "old_text": "",
                            "new_text": line,
                            "text": line,
                            "space_before": False,
                        })

    return {
        "old_full_text": old_raw,
        "new_full_text": new_raw,
        "spans": all_spans,
    }