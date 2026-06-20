"""
extractor/image.py
~~~~~~~~~~~~~~~~~~
Extract ảnh từ một Paragraph.

Xử lý:
    ✅ Ảnh inline  (<wp:inline>) — nằm trong dòng chữ
    ✅ Ảnh anchor  (<wp:anchor>) có a:blip — floating image (Win32com convert từ .doc)
    ✅ Ảnh inline trong cell (table.py gọi hàm này cho mỗi paragraph trong cell)
    ✅ VML ảnh trong w:pict > v:imagedata
    ✅ OLE object preview trong w:object > v:imagedata (CorelDraw, Visio, v.v.)

Không xử lý:
    ❌ Ảnh anchor không có a:blip — đây là shape/textbox, không phải ảnh
    ❌ Ảnh trong header / footer

Thay đổi so với cũ:
    - Bỏ data_uri khỏi content — không encode base64 tại extract time
    - image_store.save_image() lưu file ra /tmp — serve qua URL tĩnh
    - Fix EMF convert bị gọi 2 lần (lần 2 là dead code)
    - Thêm OLE object preview (w:object > v:imagedata)
    - Refactor _build_vml_image_node dùng chung cho pict và object
    - Đưa logging lên đầu file — tránh NameError nếu _convert_emf_to_png
      được gọi trước dòng khai báo _log ở giữa file
    - VML extractor luôn chạy sau drawing extractor trong cùng một run,
      tránh bỏ sót ảnh khi run vừa có w:drawing vừa có w:pict/w:object
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from docx.text.paragraph import Paragraph
from lxml import etree

from src.services.models.docnode import DocNode
from src.services.extractor.utils import OrderRef, next_order
from src.services.utils.hash import safe_sha256
from src.services.utils.image_store import save_image

# Khai báo sớm để _convert_emf_to_png() dùng được
_log = logging.getLogger(__name__)

# ── Namespaces ────────────────────────────────────────────────────────────────

_NS: Dict[str, str] = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "v": "urn:schemas-microsoft-com:vml",
    "o": "urn:schemas-microsoft-com:office:office",
    "wps": "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
    "wpg": "http://schemas.microsoft.com/office/word/2010/wordprocessingGroup",
}

_R_EMBED = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_O_TITLE = "{urn:schemas-microsoft-com:office:office}title"


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════


def _emu_to_int(v: Any) -> int:
    try:
        return int(v)
    except Exception:
        return 0


def _pure(elm):
    """
    Chuyển BaseOxmlElement về lxml element thuần.
    python-docx 1.2.0 override .xpath() và không nhận tham số 'namespaces'
    nên phải serialize → parse lại để bypass.
    """
    return etree.fromstring(etree.tostring(elm))


def _is_shape_container_drawing(drawing_elm) -> bool:
    """
    Drawing là textbox/group shape thì image.py không xử lý.
    Toàn bộ nội dung để shape.py xử lý.
    """
    return bool(drawing_elm.xpath(".//w:txbxContent | .//wpg:wgp", namespaces=_NS))


def _get_image_size(drawing_elm) -> tuple[int, int]:
    extents = drawing_elm.xpath(".//wp:inline/wp:extent", namespaces=_NS)
    if not extents:
        extents = drawing_elm.xpath(".//wp:anchor/wp:extent", namespaces=_NS)
    if not extents:
        return 0, 0
    ext = extents[0]
    return _emu_to_int(ext.get("cx")), _emu_to_int(ext.get("cy"))


def _get_alt_text(drawing_elm) -> str:
    doc_prs = drawing_elm.xpath(".//wp:inline/wp:docPr", namespaces=_NS)
    if not doc_prs:
        doc_prs = drawing_elm.xpath(".//wp:anchor/wp:docPr", namespaces=_NS)
    if not doc_prs:
        return ""
    doc_pr = doc_prs[0]
    return doc_pr.get("descr") or doc_pr.get("title") or ""


def _is_anchor(drawing_elm) -> bool:
    return bool(drawing_elm.xpath("wp:anchor", namespaces=_NS))


def _parse_vml_size(style: str) -> tuple[int, int]:
    PT_TO_EMU = 12700
    width_emu = height_emu = 0
    for part in style.split(";"):
        part = part.strip()
        if part.startswith("width:"):
            val = part[6:].replace("pt", "").strip()
            try:
                width_emu = int(float(val) * PT_TO_EMU)
            except ValueError:
                pass
        elif part.startswith("height:"):
            val = part[7:].replace("pt", "").strip()
            try:
                height_emu = int(float(val) * PT_TO_EMU)
            except ValueError:
                pass
    return width_emu, height_emu


def _convert_emf_to_png(blob: bytes) -> Optional[bytes]:
    """
    Convert EMF/WMF -> PNG bằng Win32 GDI.

    Stable cho:
        - EMF từ Office
        - CorelDraw preview
        - Visio preview
        - CAD technical drawings

    Windows only.
    """
    try:
        import ctypes
        import io
        import os
        import tempfile

        from ctypes import wintypes
        from PIL import Image

        # ── WinDLL ────────────────────────────────────────────────
        gdi32 = ctypes.WinDLL("gdi32")
        user32 = ctypes.WinDLL("user32")

        # ── Structs ───────────────────────────────────────────────

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            ]

        class ENHMETAHEADER(ctypes.Structure):
            _fields_ = [
                ("iType", wintypes.DWORD),
                ("nSize", wintypes.DWORD),
                ("rclBounds", RECT),
                ("rclFrame", RECT),
                ("dSignature", wintypes.DWORD),
                ("nVersion", wintypes.DWORD),
                ("nBytes", wintypes.DWORD),
                ("nRecords", wintypes.DWORD),
                ("nHandles", wintypes.WORD),
                ("sReserved", wintypes.WORD),
                ("nDescription", wintypes.DWORD),
                ("offDescription", wintypes.DWORD),
                ("nPalEntries", wintypes.DWORD),
                ("szlDevice_cx", wintypes.LONG),
                ("szlDevice_cy", wintypes.LONG),
                ("szlMillimeters_cx", wintypes.LONG),
                ("szlMillimeters_cy", wintypes.LONG),
            ]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD),
                ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [
                ("bmiHeader", BITMAPINFOHEADER),
                ("bmiColors", wintypes.DWORD * 3),
            ]

        # ── WinAPI signatures ─────────────────────────────────────
        gdi32.GetEnhMetaFileW.argtypes = [wintypes.LPCWSTR]
        gdi32.GetEnhMetaFileW.restype = wintypes.HANDLE
        gdi32.DeleteEnhMetaFile.argtypes = [wintypes.HANDLE]
        gdi32.DeleteEnhMetaFile.restype = wintypes.BOOL
        gdi32.GetEnhMetaFileHeader.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p]
        gdi32.GetEnhMetaFileHeader.restype = wintypes.UINT
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.DeleteDC.argtypes = [wintypes.HDC]
        gdi32.DeleteDC.restype = wintypes.BOOL
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.SelectObject.restype = wintypes.HGDIOBJ
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteObject.restype = wintypes.BOOL
        gdi32.PlayEnhMetaFile.argtypes = [wintypes.HDC, wintypes.HANDLE, ctypes.POINTER(RECT)]
        gdi32.PlayEnhMetaFile.restype = wintypes.BOOL
        gdi32.CreateDIBSection.argtypes = [
            wintypes.HDC,
            ctypes.POINTER(BITMAPINFO),
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.HANDLE,
            wintypes.DWORD,
        ]
        gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.GetDC.restype = wintypes.HDC
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.ReleaseDC.restype = wintypes.INT

        # ── Save temp EMF ─────────────────────────────────────────
        with tempfile.NamedTemporaryFile(suffix=".emf", delete=False) as f:
            f.write(blob)
            emf_path = f.name

        hemf = hdc_screen = hmdc = hbmp = old_obj = None

        try:
            hemf = gdi32.GetEnhMetaFileW(emf_path)
            if not hemf:
                raise RuntimeError("GetEnhMetaFileW failed")

            header = ENHMETAHEADER()
            if not gdi32.GetEnhMetaFileHeader(hemf, ctypes.sizeof(header), ctypes.byref(header)):
                raise RuntimeError("GetEnhMetaFileHeader failed")

            bw = header.rclBounds.right - header.rclBounds.left
            bh = header.rclBounds.bottom - header.rclBounds.top
            fw = header.rclFrame.right - header.rclFrame.left
            fh = header.rclFrame.bottom - header.rclFrame.top

            DPI = 150
            MM_PER_INCH = 25.4

            if bw > 0 and bh > 0:
                scale = DPI / 96
                w = int(bw * scale)
                h = int(bh * scale)
            else:
                w = int(fw / 100 / MM_PER_INCH * DPI)
                h = int(fh / 100 / MM_PER_INCH * DPI)

            w = max(64, min(w, 8000))
            h = max(64, min(h, 8000))

            _log.info("[EMF] render size=%dx%d", w, h)

            # ── Create DIB ────────────────────────────────────────
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = w
            bmi.bmiHeader.biHeight = -h
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = 0

            p_bits = ctypes.c_void_p()
            hdc_screen = user32.GetDC(None)
            hmdc = gdi32.CreateCompatibleDC(hdc_screen)
            if not hmdc:
                raise RuntimeError("CreateCompatibleDC failed")

            hbmp = gdi32.CreateDIBSection(
                hmdc,
                ctypes.byref(bmi),
                0,
                ctypes.byref(p_bits),
                None,
                0,
            )
            if not hbmp:
                raise RuntimeError("CreateDIBSection failed")

            ctypes.memset(p_bits.value, 255, w * h * 4)
            old_obj = gdi32.SelectObject(hmdc, hbmp)

            rect = RECT(0, 0, w, h)
            if not gdi32.PlayEnhMetaFile(hmdc, hemf, ctypes.byref(rect)):
                raise RuntimeError("PlayEnhMetaFile failed")

            gdi32.GdiFlush()

            if not p_bits.value:
                raise RuntimeError("DIB pointer null")

            raw_buf = (ctypes.c_ubyte * (w * h * 4)).from_address(p_bits.value)
            img = Image.frombuffer("RGB", (w, h), bytes(raw_buf), "raw", "BGRX", 0, 1)
            img = img.convert("RGB")

            out = io.BytesIO()
            img.save(out, format="PNG")
            return out.getvalue()

        finally:
            for action in [
                lambda: old_obj and hmdc and gdi32.SelectObject(hmdc, old_obj),
                lambda: hbmp and gdi32.DeleteObject(hbmp),
                lambda: hmdc and gdi32.DeleteDC(hmdc),
                lambda: hdc_screen and user32.ReleaseDC(None, hdc_screen),
                lambda: hemf and gdi32.DeleteEnhMetaFile(hemf),
                lambda: os.unlink(emf_path),
            ]:
                try:
                    action()
                except Exception:
                    pass

    except Exception as e:
        _log.warning("[EMF_CONVERT] fail: %s", e, exc_info=True)
        return None


def _normalize_blob(blob: bytes, mime: str) -> tuple[bytes, str]:
    if mime in ("image/x-emf", "image/emf", "image/wmf", "image/x-wmf"):
        converted = _convert_emf_to_png(blob)
        if converted:
            return converted, "image/png"
        _log.warning(
            "[image] EMF/WMF convert thất bại (mime=%s, size=%d bytes) — "
            "lưu file gốc, browser có thể không render được.",
            mime,
            len(blob),
        )
    return blob, mime


def _build_image_content(
    blob: bytes,
    mime: str,
    rid: str,
    width_emu: int,
    height_emu: int,
    alt_text: str,
    floating: bool,
    run_index: int,
    draw_index: int,
    blip_index: int,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    sha256 = safe_sha256(blob)
    image_url = save_image(sha256, blob, mime)

    content: Dict[str, Any] = {
        "rid": rid,
        "hash": sha256,
        "sha256": sha256,
        "mime": mime,
        "width_emu": width_emu,
        "height_emu": height_emu,
        "alt_text": alt_text,
        "image_url": image_url,
        "floating": floating,
        "run_index": run_index,
        "draw_index": draw_index,
        "blip_index": blip_index,
    }
    if extra:
        content.update(extra)
    return content


# ══════════════════════════════════════════════════════════════════════════════
# VML helpers
# ══════════════════════════════════════════════════════════════════════════════


def _build_vml_image_node(
    imgdata,
    container,
    part_lookup,
    uid: str,
    parent_uid: Optional[str],
    order_ref: Optional[OrderRef],
    run_idx: int,
    draw_idx: int,
    blip_idx: int,
) -> Optional[DocNode]:
    """
    Build DocNode(type="image") từ v:imagedata element.

    Dùng chung cho:
        - w:pict > v:imagedata
        - w:object > v:imagedata  (OLE preview)
    """
    rid = imgdata.get(_R_EMBED)
    if not rid:
        return None

    image_part = part_lookup.get(rid)
    if image_part is None:
        return None

    blob = image_part.blob
    if not blob:
        return None

    mime = getattr(image_part, "content_type", None) or "image/png"
    blob, mime = _normalize_blob(blob, mime)

    shapes = container.xpath(".//v:shape", namespaces=_NS)
    width_emu = height_emu = 0
    if shapes:
        width_emu, height_emu = _parse_vml_size(shapes[0].get("style", ""))

    alt_text = imgdata.get(_O_TITLE, "")

    return DocNode(
        type="image",
        uid=uid,
        parent_uid=parent_uid,
        order=next_order(order_ref),
        path=uid.replace(".", "/"),
        content=_build_image_content(
            blob=blob,
            mime=mime,
            rid=rid,
            width_emu=width_emu,
            height_emu=height_emu,
            alt_text=alt_text,
            floating=True,
            run_index=run_idx,
            draw_index=draw_idx,
            blip_index=blip_idx,
            extra={"vml": True},
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# VML extractor
# ══════════════════════════════════════════════════════════════════════════════


def _extract_vml_images(
    r_pure,
    par: Paragraph,
    uid_prefix: str,
    run_idx: int,
    parent_uid: Optional[str],
    order_ref: Optional[OrderRef],
) -> List[DocNode]:
    nodes = []
    part_lookup = par.part.related_parts

    # ── w:pict > v:imagedata ──────────────────────────────────────
    picts = [p for p in r_pure.xpath("./w:pict", namespaces=_NS) if not p.xpath(".//w:txbxContent", namespaces=_NS)]

    for pict_idx, pict in enumerate(picts, start=1):
        for img_idx, imgdata in enumerate(pict.xpath(".//v:imagedata", namespaces=_NS), start=1):
            node = _build_vml_image_node(
                imgdata=imgdata,
                container=pict,
                part_lookup=part_lookup,
                uid=f"{uid_prefix}.r{run_idx}.p{pict_idx}.{img_idx}",
                parent_uid=parent_uid,
                order_ref=order_ref,
                run_idx=run_idx,
                draw_idx=pict_idx,
                blip_idx=img_idx,
            )
            if node:
                nodes.append(node)

    # ── w:object > v:imagedata (OLE preview) ──────────────────────
    objects = [o for o in r_pure.xpath("./w:object", namespaces=_NS) if not o.xpath(".//w:txbxContent", namespaces=_NS)]

    for obj_idx, obj in enumerate(objects, start=1):
        for img_idx, imgdata in enumerate(obj.xpath(".//v:imagedata", namespaces=_NS), start=1):
            node = _build_vml_image_node(
                imgdata=imgdata,
                container=obj,
                part_lookup=part_lookup,
                uid=f"{uid_prefix}.r{run_idx}.obj{obj_idx}.{img_idx}",
                parent_uid=parent_uid,
                order_ref=order_ref,
                run_idx=run_idx,
                draw_idx=obj_idx,
                blip_idx=img_idx,
            )
            if node:
                nodes.append(node)

    return nodes


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════


def extract_inline_images(
    par: Paragraph,
    uid_prefix: str = "img",
    parent_uid: Optional[str] = None,
    order_ref: Optional[OrderRef] = None,
) -> List[DocNode]:
    """
    Trích xuất tất cả ảnh từ một paragraph.

    Lấy cả wp:inline lẫn wp:anchor miễn là có a:blip với r:embed.
    VML extractor (w:pict / w:object) luôn chạy sau drawing extractor
    trong cùng một run — tránh bỏ sót ảnh khi run vừa có w:drawing
    vừa có w:pict/w:object.

    Returns:
        List DocNode(type="image"), rỗng nếu không có ảnh.
    """
    nodes: List[DocNode] = []

    for run_idx, run in enumerate(par.runs, start=1):
        r_elm = run._r
        if r_elm is None:
            continue

        r_pure = _pure(r_elm)
        drawings = r_pure.xpath("./w:drawing", namespaces=_NS)

        # ── w:drawing (inline / anchor) ───────────────────────────
        for draw_idx, drawing in enumerate(drawings, start=1):
            # Nếu drawing là textbox/shape thì bỏ qua.
            # Ảnh/text/table bên trong textbox do extractor/shape.py xử lý.
            if _is_shape_container_drawing(drawing):
                continue

            blip_rids = drawing.xpath(".//a:blip/@r:embed", namespaces=_NS)
            if not blip_rids:
                continue

            width_emu, height_emu = _get_image_size(drawing)
            alt_text = _get_alt_text(drawing)
            floating = _is_anchor(drawing)

            for blip_idx, rid in enumerate(blip_rids, start=1):
                part = par.part.related_parts.get(rid)
                if part is None:
                    continue

                blob = part.blob
                if not blob:
                    continue

                mime = getattr(part, "content_type", None) or "image/png"
                blob, mime = _normalize_blob(blob, mime)

                img_uid = f"{uid_prefix}.r{run_idx}.d{draw_idx}.{blip_idx}"

                nodes.append(
                    DocNode(
                        type="image",
                        uid=img_uid,
                        parent_uid=parent_uid,
                        order=next_order(order_ref),
                        path=img_uid.replace(".", "/"),
                        content=_build_image_content(
                            blob=blob,
                            mime=mime,
                            rid=rid,
                            width_emu=width_emu,
                            height_emu=height_emu,
                            alt_text=alt_text,
                            floating=floating,
                            run_index=run_idx,
                            draw_index=draw_idx,
                            blip_index=blip_idx,
                        ),
                    )
                )

        # ── VML (w:pict / w:object) — luôn chạy sau drawing ──────
        # Không dùng else để tránh bỏ sót khi run có cả hai loại
        nodes.extend(_extract_vml_images(r_pure, par, uid_prefix, run_idx, parent_uid, order_ref))

    return nodes
