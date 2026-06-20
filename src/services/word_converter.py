import os
import shutil
import tempfile
import uuid
import gc
import pythoncom
import win32com.client


WD_FORMAT_XML_DOCUMENT = 16


def convert_doc_to_docx_if_needed(file_path: str) -> str:
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".docx":
        return file_path

    if ext != ".doc":
        raise ValueError(f"Unsupported file type: {ext}")

    abs_path = os.path.abspath(file_path)

    temp_dir = tempfile.gettempdir()
    base_name = os.path.splitext(os.path.basename(abs_path))[0]

    out_path = os.path.join(
        temp_dir,
        f"{base_name}_{uuid.uuid4().hex}_converted.docx"
    )

    word = None
    doc = None

    pythoncom.CoInitialize()

    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0

        doc = word.Documents.Open(
            abs_path,
            ReadOnly=True,
            AddToRecentFiles=False,
            ConfirmConversions=False,
            NoEncodingDialog=True,
        )

        doc.SaveAs2(
            out_path,
            FileFormat=WD_FORMAT_XML_DOCUMENT
        )

        doc.Close(False)
        doc = None

        word.Quit()
        word = None

        gc.collect()

        return out_path

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

        doc = None
        word = None

        gc.collect()
        pythoncom.CoUninitialize()


def normalize_docx_for_compare(file_path: str) -> str:
    converted_path = convert_doc_to_docx_if_needed(file_path)

    temp_dir = tempfile.gettempdir()
    base_name = os.path.splitext(os.path.basename(converted_path))[0]

    normalized_path = os.path.join(
        temp_dir,
        f"normalized_{base_name}_{uuid.uuid4().hex}.docx"
    )

    shutil.copy2(converted_path, normalized_path)

    return normalized_path