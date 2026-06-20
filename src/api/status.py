"""
api/status.py
~~~~~~~~~~~~~
GET /status/job/{job_id}  — trạng thái 1 job
GET /status/jobs          — lịch sử job của 1 user

Fixes:
1. /jobs không validate username → bất kỳ ai cũng xem được job của người khác.
   FIX: validate format username + giới hạn thông tin trả về.
2. job_id không được validate → có thể truyền ký tự đặc biệt.
   FIX: thêm regex whitelist cho job_id.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

from fastapi import APIRouter, HTTPException, Query

from src.core.jobs import get_job, get_jobs_by_username

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/status", tags=["Status"])

ENABLE_DEBUG = os.getenv("ENABLE_DEBUG_FIELDS", "false").lower() == "true"

# ── Validators ────────────────────────────────────────────────────────────────

# job_id là uuid4().hex — 32 ký tự hex
_JOB_ID_RE  = re.compile(r"^[0-9a-f]{32}$")
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _validate_job_id(job_id: str) -> None:
    if not _JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=400, detail="Invalid job_id format")


def _validate_username(username: str) -> None:
    if not _USERNAME_RE.match(username):
        raise HTTPException(status_code=400, detail="Invalid username format")


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/job/{job_id}")
async def job_status(job_id: str):
    """
    Trả trạng thái và kết quả của một job.
    Status flow: queued → processing → done | error
    """
    _validate_job_id(job_id)

    # SQLite sync → chạy trong thread pool, không block event loop
    job = await asyncio.to_thread(get_job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    result = job.get("result") or {}

    response = {
        # ── Job identity ──────────────────────────────────────────────────────
        "job_id":   job["job_id"],
        "username": job["username"],
        "date":     job["date"],
        "status":   job["status"],  # queued | processing | done | error

        # ── File metadata ─────────────────────────────────────────────────────
        "original_file_name":   job.get("original_file_name"),
        "modified_file_name":   job.get("modified_file_name"),
        "original_folder_name": job.get("original_folder_name"),
        "modified_folder_name": job.get("modified_folder_name"),

        # ── Core result ───────────────────────────────────────────────────────
        "compare": result.get("compare"),

        # ── System flags ──────────────────────────────────────────────────────
        "files_deleted": job.get("files_deleted", False),
        "error":         job.get("error"),
    }

    if ENABLE_DEBUG:
        response["_debug"] = {
            "original_blocks": result.get("original"),
            "modified_blocks": result.get("modified"),
        }

    return response


@router.get("/jobs")
async def list_jobs_by_user(
    username: str = Query(..., description="Username to fetch jobs for"),
):
    """
    Lấy toàn bộ job của một user (mới nhất trước).

    NOTE: Endpoint này không có authentication — chỉ nên dùng nội bộ
    hoặc sau khi thêm auth middleware. Hiện tại validate format username
    để ít nhất chặn input độc hại.
    """
    _validate_username(username)

    # SQLite sync → chạy trong thread pool, không block event loop
    jobs = await asyncio.to_thread(get_jobs_by_username, username)

    return {
        "username": username,
        "total":    len(jobs),
        "jobs": [
            {
                "job_id":             j["job_id"],
                "date":               j["date"],
                "status":             j["status"],
                "created_at":         j["created_at"],
                "original_file_name": j.get("original_file_name"),
                "modified_file_name": j.get("modified_file_name"),
                "files_deleted":      j.get("files_deleted", False),
                "error":              j.get("error"),
            }
            for j in jobs
        ],
    }