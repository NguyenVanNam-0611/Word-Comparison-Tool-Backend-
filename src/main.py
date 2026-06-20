from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from src.api.upload import router as upload_router
from src.api.routes.images import router as images_router
from src.core.scheduler import start_scheduler, stop_scheduler
from src.api.status import router as status_router
from src.core.jobs import init_db
from src.core.queue import start_workers, stop_workers, requeue_pending_jobs
from src.worker.processor import process_job
from src.services.utils.image_store import ensure_tmp_dir, cleanup_old_images


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ───────────────────────────────────────────────────────────────
    print("=" * 44)
    print("[STARTUP] Document compare service starting")

    init_db()
    print("[STARTUP] DB initialised")

    ensure_tmp_dir()
    print("[STARTUP] Image tmp dir ready")

    await requeue_pending_jobs()
    print("[STARTUP] Pending jobs requeued")

    start_workers(process_job, num_workers=2)
    print("[STARTUP] Workers started")

    start_scheduler()
    print("[STARTUP] Cleanup scheduler started")

    print("[STARTUP] Service ready")
    print("=" * 44)

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    print("=" * 44)
    print("[SHUTDOWN] Stopping service")

    await stop_workers()
    print("[SHUTDOWN] Workers stopped")

    stop_scheduler()
    print("[SHUTDOWN] Cleanup scheduler stopped")

    print("[SHUTDOWN] Bye")
    print("=" * 44)


app = FastAPI(
    title="Document Compare API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount(
    "/uploads",
    StaticFiles(directory="uploads"),
    name="uploads",
)

app.include_router(upload_router)
app.include_router(status_router, prefix="/api")
app.include_router(images_router)


@app.get("/")
async def root():
    return {"service": "document-compare-api", "status": "running"}


@app.get("/health")
async def health():
    return {"status": "ok"}