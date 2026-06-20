"""
core/queue.py
~~~~~~~~~~~~~
Async job queue + worker pool.

Fixes so với cũ:
1. Bỏ module-level global state → encapsulate vào JobQueue class.
2. Thay asyncio.get_event_loop() deprecated → asyncio.get_running_loop().
3. requeue_pending dùng jobs.py API — không mở SQLite connection riêng.
4. Thêm retry limit — job lỗi vĩnh viễn bị đánh dấu error sau MAX_RETRIES lần.
5. start_workers / stop_workers / enqueue_job expose ở module level — không breaking change.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Awaitable

from src.core.jobs import get_job, update_job, reset_and_get_pending_jobs

logger = logging.getLogger(__name__)

MAX_RETRIES = 2
JOB_TIMEOUT_SECONDS = 900  # 15 phút


class JobQueue:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []

    # ── Enqueue ───────────────────────────────────────────────────────────────

    async def enqueue(self, job_id: str) -> None:
        await self._queue.put(job_id)
        logger.info(f"[QUEUE] Enqueued job_id={job_id} | size={self._queue.qsize()}")

    # ── Worker ────────────────────────────────────────────────────────────────

    async def _worker(
        self,
        worker_id: int,
        process_func: Callable[[dict], Awaitable[None]],
    ) -> None:
        logger.info(f"[WORKER-{worker_id}] Started")

        while True:
            job_id = await self._queue.get()
            job: dict | None = None  # FIX: khởi tạo trước try để tránh UnboundLocalError

            try:
                job = await asyncio.to_thread(get_job, job_id)
                if not job:
                    logger.warning(f"[WORKER-{worker_id}] Job not found: {job_id}")
                    continue

                retry_count = job.get("result", {}).get("_retry_count", 0)
                if retry_count >= MAX_RETRIES:
                    logger.error(
                        f"[WORKER-{worker_id}] Job {job_id} exceeded " f"retry limit ({MAX_RETRIES}) — marking as error"
                    )
                    await asyncio.to_thread(
                        update_job,
                        job_id,
                        {
                            "status": "error",
                            "error": f"Exceeded retry limit ({MAX_RETRIES})",
                        },
                    )
                    continue

                logger.info(f"[WORKER-{worker_id}] Processing job_id={job_id} (attempt {retry_count + 1})")
                await asyncio.to_thread(update_job, job_id, {"status": "processing"})

                await asyncio.wait_for(process_func(job), timeout=JOB_TIMEOUT_SECONDS)

                logger.info(f"[WORKER-{worker_id}] Done job_id={job_id}")

            except asyncio.CancelledError:
                logger.warning(f"[WORKER-{worker_id}] Cancelled mid-job {job_id} — requeuing")
                await asyncio.to_thread(update_job, job_id, {"status": "queued", "error": None})
                raise

            except asyncio.TimeoutError:
                logger.error(f"[WORKER-{worker_id}] Job {job_id} timed out after {JOB_TIMEOUT_SECONDS}s")
                retry_count = (job.get("result", {}).get("_retry_count", 0) + 1) if job else 1

                if retry_count < MAX_RETRIES:
                    await asyncio.to_thread(
                        update_job,
                        job_id,
                        {
                            "status": "queued",
                            "result": {
                                **(job.get("result", {}) if job else {}),
                                "_retry_count": retry_count,
                            },
                            "error": f"Job timed out after {JOB_TIMEOUT_SECONDS}s",
                        },
                    )
                    await self._queue.put(job_id)
                    logger.warning(f"[WORKER-{worker_id}] Retry {retry_count}/{MAX_RETRIES} for job {job_id} (timeout)")
                else:
                    await asyncio.to_thread(
                        update_job,
                        job_id,
                        {
                            "status": "error",
                            "error": f"Job timed out after {JOB_TIMEOUT_SECONDS}s (after {MAX_RETRIES} retries)",
                        },
                    )

            except Exception as e:
                logger.error(
                    f"[WORKER-{worker_id}] Job {job_id} failed: {e}",
                    exc_info=True,
                )

                # FIX: job có thể là None nếu get_job() throw exception
                retry_count = (job.get("result", {}).get("_retry_count", 0) + 1) if job else 1

                if retry_count < MAX_RETRIES:
                    await asyncio.to_thread(
                        update_job,
                        job_id,
                        {
                            "status": "queued",
                            "result": {
                                **(job.get("result", {}) if job else {}),
                                "_retry_count": retry_count,
                            },
                            "error": str(e),
                        },
                    )
                    await self._queue.put(job_id)
                    logger.warning(f"[WORKER-{worker_id}] Retry {retry_count}/{MAX_RETRIES} for job {job_id}")
                else:
                    await asyncio.to_thread(
                        update_job,
                        job_id,
                        {
                            "status": "error",
                            "error": f"{e} (after {MAX_RETRIES} retries)",
                        },
                    )

            finally:
                self._queue.task_done()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(
        self,
        process_func: Callable[[dict], Awaitable[None]],
        num_workers: int = 1,
    ) -> None:
        loop = asyncio.get_running_loop()
        for i in range(num_workers):
            task = loop.create_task(
                self._worker(worker_id=i + 1, process_func=process_func),
                name=f"worker-{i + 1}",
            )
            self._tasks.append(task)
        logger.info(f"[QUEUE] Started {num_workers} worker(s)")

    async def stop(self, timeout: float = 30.0) -> None:
        logger.info("[QUEUE] Draining queue...")
        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("[QUEUE] Drain timeout — forcing shutdown")

        for task in self._tasks:
            task.cancel()

        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("[QUEUE] All workers stopped")

    async def requeue_pending(self) -> None:
        """
        Sau khi restart, requeue tất cả job chưa hoàn thành.
        - status = 'processing' → bị dở do crash → reset về 'queued'
        - status = 'queued'     → chưa được xử lý → enqueue lại
        """
        job_ids = await asyncio.to_thread(reset_and_get_pending_jobs)

        for job_id in job_ids:
            await self._queue.put(job_id)

        if job_ids:
            logger.info(f"[QUEUE] Requeued {len(job_ids)} pending job(s) after restart")


# ── Module-level singleton + convenience functions ────────────────────────────

_queue = JobQueue()


async def enqueue_job(job_id: str) -> None:
    await _queue.enqueue(job_id)


def start_workers(
    process_func: Callable[[dict], Awaitable[None]],
    num_workers: int = 1,
) -> None:
    _queue.start(process_func, num_workers=num_workers)


async def stop_workers() -> None:
    await _queue.stop()


async def requeue_pending_jobs() -> None:
    await _queue.requeue_pending()
