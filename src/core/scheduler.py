"""
core/scheduler.py
~~~~~~~~~~~~~~~~~
APScheduler singleton cho Python service.

Trách nhiệm:
1. Tự xóa folder upload của từng job sau 24 giờ.
2. Đánh dấu job đã xóa file bằng files_deleted = 1.
3. Dọn ảnh tạm cũ định kỳ.
4. Xóa record job cũ khỏi SQLite sau khi file đã được cleanup.

Lưu ý:
- Scheduler jobstore dùng SQLite riêng: data/scheduler.db
- Job queue DB dùng SQLite riêng: data/jobs.db
- Không xử lý business compare trong file này.
"""

from __future__ import annotations
import os
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.core.jobs import mark_job_files_deleted, purge_old_jobs
from src.services.utils.image_store import cleanup_old_images

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Scheduler singleton
# ═══════════════════════════════════════════════════════════════════════════════

_JOBSTORE_PATH = Path("data/scheduler.db")
_JOBSTORE_PATH.parent.mkdir(parents=True, exist_ok=True)

scheduler = AsyncIOScheduler(
    jobstores={
        "default": SQLAlchemyJobStore(url=f"sqlite:///{_JOBSTORE_PATH}")
    },
    timezone="UTC",
)


# ═══════════════════════════════════════════════════════════════════════════════
# Periodic tasks
# ═══════════════════════════════════════════════════════════════════════════════

def _purge_old_jobs_task() -> None:
    """
    Xóa record job cũ khỏi DB.

    Điều kiện xóa nằm trong jobs.py:
    - status IN ('done', 'error')
    - created_at cũ hơn older_than_hours
    - files_deleted = 1

    Nghĩa là:
    file upload phải được cleanup trước,
    sau đó mới xóa metadata job khỏi SQLite.
    """
    try:
        deleted = purge_old_jobs(older_than_hours=24)

        if deleted:
            logger.info("[PURGE] Deleted %s old job record(s)", deleted)
        else:
            logger.debug("[PURGE] No old job records to delete")

    except Exception:
        logger.exception("[PURGE] Failed to purge old job records")


def _cleanup_images_task() -> None:
    """
    Dọn ảnh tạm cũ.

    Ảnh này là ảnh extract từ docx để render UI/checksheet.
    Tuổi file tối đa nên được quản lý trong image_store.py.
    """
    try:
        deleted = cleanup_old_images()

        if deleted:
            logger.info("[IMAGE_CLEANUP] Deleted %s stale image file(s)", deleted)
        else:
            logger.debug("[IMAGE_CLEANUP] No stale images to delete")

    except Exception:
        logger.exception("[IMAGE_CLEANUP] Failed to cleanup old images")


# ═══════════════════════════════════════════════════════════════════════════════
# Lifecycle
# ═══════════════════════════════════════════════════════════════════════════════

def start_scheduler() -> None:
    """
    Khởi động APScheduler.

    Gọi 1 lần ở startup main.py.
    Không gọi trong api/upload.py.
    """
    if scheduler.running:
        logger.info("[SCHEDULER] Already running")
        return

    scheduler.start()

    # Xóa job record cũ mỗi 24 giờ.
    # Nếu server restart và miss lịch, cho phép chạy bù trong 1 giờ.
    scheduler.add_job(
        _purge_old_jobs_task,
        trigger="interval",
        hours=24,
        id="purge_old_jobs",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # Dọn ảnh tạm mỗi 12 giờ.
    # Không cần chạy bù quá quan trọng; lần sau sẽ dọn tiếp.
    scheduler.add_job(
        _cleanup_images_task,
        trigger="interval",
        hours=12,
        id="cleanup_images",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    logger.info("[SCHEDULER] Started with SQLite jobstore: %s", _JOBSTORE_PATH)


def stop_scheduler() -> None:
    """
    Dừng scheduler khi app shutdown.
    wait=False để không block shutdown quá lâu.
    """
    if not scheduler.running:
        return

    scheduler.shutdown(wait=False)
    logger.info("[SCHEDULER] Stopped")

def _cleanup_empty_parents_until_uploads(deleted_path: Path) -> None:
    """
    Sau khi xóa folder job, xóa ngược các thư mục cha nếu rỗng.
    Dừng lại ở thư mục uploads, không xóa uploads.
    """
    current = deleted_path.parent

    while current.name != "uploads":
        try:
            current.rmdir()
            logger.info("[CLEANUP] Deleted empty parent folder: %s", current)
        except OSError:
            break

        current = current.parent
# ═══════════════════════════════════════════════════════════════════════════════
# Upload file cleanup
# ═══════════════════════════════════════════════════════════════════════════════

def delete_upload_files(job_id: str, folders: list[str]) -> None:
    """
    Xóa folder upload của một job.

    Được APScheduler gọi sau 24 giờ kể từ lúc upload.

    Sau khi chạy xong, dù folder đã tồn tại hay đã bị xóa trước đó,
    vẫn đánh dấu files_deleted = 1 vì trạng thái cuối cùng là:
    file upload không còn cần giữ nữa.
    """
    all_handled = True

    for folder in folders:
        try:
            path = Path(folder)

            if path.exists():
                shutil.rmtree(path)
                logger.info("[CLEANUP] Deleted folder: %s", path)

                _cleanup_empty_parents_until_uploads(path)
            else:
                logger.info("[CLEANUP] Folder already gone: %s", path)

                _cleanup_empty_parents_until_uploads(path)

        except Exception:
            all_handled = False
            logger.exception("[CLEANUP] Failed to delete folder: %s", folder)

    if not all_handled:
        logger.warning(
            "[CLEANUP] Job %s cleanup had errors; files_deleted will not be marked",
            job_id,
        )
        return

    try:
        mark_job_files_deleted(job_id)
        logger.info("[CLEANUP] Marked files_deleted=1 for job %s", job_id)

    except Exception:
        logger.exception("[CLEANUP] Failed to update files_deleted for job %s", job_id)


def schedule_cleanup(job_id: str, folders: list[Path]) -> None:
    """
    Lên lịch xóa folder upload sau 24 giờ.

    Job này được persist trong data/scheduler.db.
    Nếu server restart trước thời điểm cleanup,
    APScheduler vẫn còn lịch để chạy lại.
    """
    run_at = datetime.now(timezone.utc) + timedelta(hours=24)

    scheduler.add_job(
        delete_upload_files,
        trigger="date",
        run_date=run_at,
        args=[job_id, [str(folder) for folder in folders]],
        id=f"cleanup_{job_id}",
        replace_existing=True,
        misfire_grace_time=None,
    )

    logger.info(
        "[CLEANUP] Scheduled cleanup for job %s at %s",
        job_id,
        run_at.isoformat(),
    )