"""
core/jobs.py
~~~~~~~~~~~~
SQLite persistence layer cho job queue.

Fixes:
1. reset_and_get_pending_jobs bị indent sai vào trong purge_old_jobs → dedent ra ngoài.
2. Thay vì tạo connection mỗi query → dùng thread-local connection pool
   (SQLite không thread-safe khi share 1 conn, nhưng thread-local là an toàn).
3. WAL mode để đọc/ghi không block nhau.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

DB_PATH = Path("data/jobs.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Thread-local storage — mỗi thread giữ 1 connection riêng, tránh race condition
_local = threading.local()

# Whitelist các column được phép update — tránh SQL injection qua update_job
_UPDATABLE_COLUMNS = frozenset({
    "status",
    "result",
    "error",
    "files_deleted",
})


def _get_conn() -> sqlite3.Connection:
    """
    Trả về connection của thread hiện tại.
    Tạo mới nếu chưa có hoặc đã bị đóng.
    Dùng WAL mode để read không block write và ngược lại.
    """
    conn = getattr(_local, "conn", None)
    try:
        # Ping để kiểm tra connection còn sống không
        conn.execute("SELECT 1")
    except Exception:
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


@contextmanager
def _transaction():
    """Context manager bọc 1 transaction, auto commit/rollback."""
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ── Schema ────────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Tạo bảng nếu chưa có. Gọi 1 lần khi startup."""
    with _transaction() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id               TEXT PRIMARY KEY,
                username             TEXT NOT NULL,
                original_path        TEXT NOT NULL,
                modified_path        TEXT NOT NULL,
                date                 TEXT NOT NULL,
                created_at           TEXT NOT NULL,
                original_file_name   TEXT,
                modified_file_name   TEXT,
                original_folder_name TEXT,
                modified_folder_name TEXT,
                status               TEXT NOT NULL DEFAULT 'queued',
                result               TEXT,
                error                TEXT,
                files_deleted        INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_jobs_username
            ON jobs (username)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_jobs_status
            ON jobs (status)
        """)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_job(row) -> dict:
    """Chuyển sqlite3.Row thành dict, parse result JSON và coerce files_deleted."""
    job = dict(row)
    try:
        job["result"] = json.loads(job["result"]) if job["result"] else {}
    except (json.JSONDecodeError, TypeError):
        job["result"] = {}
    job["files_deleted"] = bool(job["files_deleted"])
    return job


# ── CRUD ──────────────────────────────────────────────────────────────────────

def create_job(
    job_id: str,
    username: str,
    original_path: str,
    modified_path: str,
    date: str,
    original_file_name: str,
    modified_file_name: str,
    original_folder_name: str,
    modified_folder_name: str,
    created_at: str,
) -> None:
    default_result = json.dumps({"compare": None})

    with _transaction() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                job_id, username,
                original_path, modified_path,
                date, created_at,
                original_file_name, modified_file_name,
                original_folder_name, modified_folder_name,
                status, result, error, files_deleted
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, NULL, 0)
            """,
            (
                job_id, username,
                original_path, modified_path,
                date, created_at,
                original_file_name, modified_file_name,
                original_folder_name, modified_folder_name,
                default_result,
            ),
        )


def get_job(job_id: str) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    return _parse_job(row) if row is not None else None


def update_job(job_id: str, data: dict) -> None:
    """
    Update linh hoạt từng field.

    Chỉ cho phép update các column trong _UPDATABLE_COLUMNS —
    tránh SQL injection khi key đến từ caller bên ngoài.
    Nếu data có key 'result' là dict, tự động serialize sang JSON.
    """
    if not data:
        return

    unknown = set(data.keys()) - _UPDATABLE_COLUMNS
    if unknown:
        raise ValueError(
            f"update_job: unknown columns {unknown}. Allowed: {_UPDATABLE_COLUMNS}"
        )

    payload = data.copy()
    if "result" in payload and isinstance(payload["result"], dict):
        payload["result"] = json.dumps(payload["result"])

    # Column name đến từ whitelist — an toàn để interpolate vào SQL
    columns = ", ".join(f"{k} = ?" for k in payload)
    values  = list(payload.values()) + [job_id]

    with _transaction() as conn:
        conn.execute(
            f"UPDATE jobs SET {columns} WHERE job_id = ?",
            values,
        )


def mark_job_files_deleted(job_id: str) -> None:
    with _transaction() as conn:
        conn.execute(
            "UPDATE jobs SET files_deleted = 1 WHERE job_id = ?",
            (job_id,),
        )


def get_jobs_by_username(username: str) -> list[dict]:
    """Lấy toàn bộ job của 1 user, mới nhất trước."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE username = ? ORDER BY created_at DESC",
        (username,),
    ).fetchall()
    return [_parse_job(row) for row in rows]


def purge_old_jobs(older_than_hours: int = 24) -> int:
    """
    Xóa job cũ khỏi SQLite.

    Chỉ xóa khi thỏa cả 3 điều kiện:
    1. Job đã kết thúc: status IN ('done', 'error')
    2. Job đã cũ hơn older_than_hours
    3. File upload đã được cleanup: files_deleted = 1

    Lý do cần files_deleted = 1:
    - Tránh mất record DB trong khi file upload vẫn còn trên disk.
    - Nếu cleanup file lỗi, record vẫn còn để debug hoặc cleanup lại.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
    ).replace(microsecond=0).isoformat()

    with _transaction() as conn:
        cur = conn.execute(
            """
            DELETE FROM jobs
             WHERE status IN ('done', 'error')
               AND created_at < ?
               AND files_deleted = 1
            """,
            (cutoff,),
        )
        return cur.rowcount


# FIX: dedent ra khỏi purge_old_jobs — trước đây bị nest bên trong nên không import được
def reset_and_get_pending_jobs() -> list[str]:
    """
    Reset processing→queued, trả list job_id cần requeue sau restart.s
    Chạy trong 1 transaction để atomic: không có job nào bị bỏ sót
    giữa SELECT và UPDATE.
    """
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT job_id FROM jobs WHERE status IN ('queued', 'processing')"
        ).fetchall()
        conn.execute(
            "UPDATE jobs SET status = 'queued' WHERE status = 'processing'"
        )
    return [r["job_id"] for r in rows]