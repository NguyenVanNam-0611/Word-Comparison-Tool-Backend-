from pathlib import Path
import asyncio
import logging
import traceback
import time

from src.core.jobs import update_job
from src.services.diff_service import DiffService

logger = logging.getLogger(__name__)


async def process_job(job: dict) -> None:
    job_id = job["job_id"]
    original_path = Path(job["original_path"])
    modified_path = Path(job["modified_path"])

    t0 = time.perf_counter()

    logger.info(f"[PROCESS] START job_id={job_id} user={job.get('username')}")
    logger.info(f"[PROCESS] original={original_path} | modified={modified_path}")

    try:
        # ── Validate ──────────────────────────────────────────────────────────
        if original_path.suffix.lower() != ".docx":
            raise ValueError(f"Original file must be .docx, got: {original_path.suffix}")
        if modified_path.suffix.lower() != ".docx":
            raise ValueError(f"Modified file must be .docx, got: {modified_path.suffix}")

        if not original_path.exists():
            raise FileNotFoundError(f"Original file not found: {original_path}")
        if not modified_path.exists():
            raise FileNotFoundError(f"Modified file not found: {modified_path}")

        logger.info(
            f"[PROCESS] file_size "
            f"original={original_path.stat().st_size / 1024 / 1024:.2f}MB "
            f"modified={modified_path.stat().st_size / 1024 / 1024:.2f}MB"
        )

        # ── Compare (chạy trong thread pool, không block event loop) ──────────
        service = DiffService()
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            service.compare,
            str(original_path),
            str(modified_path),
        )

        # ── Lưu kết quả ───────────────────────────────────────────────────────
        sections = result.get("sections", [])
        total_sections = result.get("total_sections", len(sections))
        total_changes = result.get("total_changes", 0)

        update_job(
            job_id,
            {
                "status": "done",
                "result": {
                    "job_id": job_id,
                    "original_file": original_path.name,
                    "modified_file": modified_path.name,
                    "summary": {
                        "total_sections": total_sections,
                        "total_changes": total_changes,
                    },
                    "compare": {
                        "sections": sections,
                    },
                },
            },
        )

        logger.info(
            f"[PROCESS] DONE job_id={job_id} "
            f"sections={total_sections} changes={total_changes} "
            f"elapsed={time.perf_counter() - t0:.3f}s"
        )

    except Exception as e:
        error_detail = f"{e}\n\n{traceback.format_exc()}"
        logger.error(
            f"[PROCESS] ERROR job_id={job_id}: {e} " f"elapsed={time.perf_counter() - t0:.3f}s",
            exc_info=True,
        )

        update_job(
            job_id,
            {
                "status": "error",
                "error": error_detail,
            },
        )
