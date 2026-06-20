"""
api/upload.py
~~~~~~~~~~~~~
POST /upload — nhận 2 file .doc/.docx, tạo job, enqueue, schedule cleanup.

Chỉ chịu trách nhiệm:
  - Validate input (username, filename, size)
  - Ghi file xuống disk
  - Tạo job trong DB
  - Enqueue + lên lịch cleanup

Scheduler lifecycle (start/stop) được quản lý bởi core/scheduler.py
và khởi động từ main.py — không phải trách nhiệm của file này.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from src.core.jobs import create_job
from src.core.queue import enqueue_job
from src.core.scheduler import schedule_cleanup
from src.services.word_converter import normalize_docx_for_compare

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Upload"])

# ── Constants ─────────────────────────────────────────────────────────────────

ALLOWED_EXTENSIONS = {".doc", ".docx"}

# Đọc từ env var — stable qua Docker, systemd, v.v.
UPLOAD_BASE = Path(os.environ.get("UPLOAD_DIR", "uploads")).resolve()

# 50 MB per file
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# Username: chỉ cho phép ký tự an toàn, tránh path traversal
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


# ── Validators ────────────────────────────────────────────────────────────────

def _validate_username(username: str) -> None:
    """
    Whitelist username — chỉ cho phép ký tự an toàn.
    Chặn path traversal kiểu '../', absolute path, ký tự đặc biệt.
    """
    if not _USERNAME_RE.match(username):
        raise HTTPException(
            status_code=400,
            detail="Invalid username. Only letters, digits, hyphens, and underscores are allowed (max 64 chars).",
        )


def _validate_file(file: UploadFile) -> str:
    """Kiểm tra extension và sanitize filename. Trả về safe filename."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required")

    safe_name = Path(file.filename).name  # loại bỏ path traversal
    ext = Path(safe_name).suffix.lower()

    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type '{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    return safe_name


# ── File I/O ──────────────────────────────────────────────────────────────────

async def _read_and_check_size(file: UploadFile) -> bytes:
    """
    Đọc toàn bộ file vào memory, kiểm tra size.
    Raise 413 nếu vượt quá MAX_UPLOAD_BYTES.
    """
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        mb = MAX_UPLOAD_BYTES // (1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Maximum allowed size is {mb} MB.",
        )
    return content


async def _save_file(
    content: bytes,
    safe_filename: str,
    username: str,
    mode: str,
    date: str,
) -> tuple[Path, str, str, Path]:
    """
    Ghi file xuống disk trong thread pool (không block event loop).
    Returns: (file_path, folder_name, stem, folder_path)
    """
    stem = Path(safe_filename).stem
    ts   = datetime.now(timezone.utc).strftime("%H%M%S")
    folder_name = f"{stem}_{ts}"

    folder    = UPLOAD_BASE / date / username / mode / folder_name
    file_path = folder / safe_filename

    def _write() -> None:
        folder.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(content)

    await asyncio.to_thread(_write)

    return file_path, folder_name, stem, folder


def _rollback_folders(*folders: Path | None) -> None:
    """Xóa các folder đã tạo khi upload thất bại giữa chừng."""
    for folder in folders:
        if folder is None:
            continue
        try:
            if folder.exists():
                shutil.rmtree(folder)
                logger.info(f"[UPLOAD] Rolled back folder: {folder}")
        except Exception as e:
            logger.error(f"[UPLOAD] Rollback failed for {folder}: {e}")


# ── Route ─────────────────────────────────────────────────────────────────────

@router.post("/upload")
async def upload_pair(
    username: str = Form(...),
    originalFile: UploadFile = File(...),
    modifiedFile: UploadFile = File(...),
):
    # 1. Validate username — phải làm trước khi dùng trong path
    _validate_username(username)

    # 2. Validate filename + extension (trước khi đọc nội dung)
    original_safe_name = _validate_file(originalFile)
    modified_safe_name = _validate_file(modifiedFile)

    # 3. Đọc nội dung + check size (async, không block)
    original_content = await _read_and_check_size(originalFile)
    modified_content = await _read_and_check_size(modifiedFile)

    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 4. Lưu file — rollback nếu bất kỳ bước nào thất bại
    o_folder: Path | None = None
    m_folder: Path | None = None

    try:
        o_path, o_folder_name, o_stem, o_folder = await _save_file(
            original_content, original_safe_name, username, "original", date
        )
        m_path, m_folder_name, m_stem, m_folder = await _save_file(
            modified_content, modified_safe_name, username, "modified", date
        )
    except HTTPException:
        _rollback_folders(o_folder, m_folder)
        raise
    except Exception:
        _rollback_folders(o_folder, m_folder)
        raise HTTPException(status_code=500, detail="Failed to save uploaded files")

    # 5. Convert .doc → .docx nếu cần (sync → chạy trong thread pool)
    try:
        o_path = await asyncio.to_thread(normalize_docx_for_compare, o_path)
        m_path = await asyncio.to_thread(normalize_docx_for_compare, m_path)
    except Exception as e:
        _rollback_folders(o_folder, m_folder)
        raise HTTPException(status_code=500, detail=f"Failed to convert file: {e}")

    # 6. Tạo job trong DB (sync SQLite → thread pool)
    job_id  = uuid.uuid4().hex
    now_utc = datetime.now(timezone.utc)

    try:
        await asyncio.to_thread(
            create_job,
            job_id=job_id,
            username=username,
            original_path=str(o_path),
            modified_path=str(m_path),
            date=date,
            original_file_name=original_safe_name, 
            modified_file_name=modified_safe_name,
            original_folder_name=o_folder_name,
            modified_folder_name=m_folder_name,
            created_at=now_utc.replace(microsecond=0).isoformat(),
        )
    except Exception as e:
        _rollback_folders(o_folder, m_folder)
        raise HTTPException(status_code=500, detail=f"Failed to create job: {e}")

    # 7. Enqueue + schedule cleanup
    await enqueue_job(job_id)
    schedule_cleanup(job_id, folders=[o_folder, m_folder])

    expire_at = (now_utc + timedelta(hours=24)).replace(microsecond=0).isoformat()

    return {
        "job_id":          job_id,
        "status":          "queued",
        "status_url":      f"/api/status/job/{job_id}",
        "files_expire_at": expire_at,
    }