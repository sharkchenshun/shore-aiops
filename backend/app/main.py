# ============================================================
# app/main.py — FastAPI 应用入口
# ============================================================

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import auth, deployments, health, home, incidents, inspection, knowledge, monitor, ops, security, settings_api, topology
from app.core.config import settings
from app.core.logging import get_logger, setup_logging
from app.scheduler import start_scheduler, stop_scheduler

# 触发工具注册（import 副作用）
import app.tools.observability  # noqa: F401
import app.tools.ops  # noqa: F401

setup_logging()
logger = get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("shore.startup", environment=settings.ENVIRONMENT, version=settings.APP_VERSION)
    if settings.is_production and settings.SECRET_KEY.startswith("dev-only"):
        logger.error("startup.insecure_secret_key")
        raise RuntimeError("生产环境必须设置 SECRET_KEY")
    start_scheduler()
    try:
        from app.bootstrap import sync_dev_monitor_tasks
        await sync_dev_monitor_tasks()
    except Exception as exc:  # noqa: BLE001
        logger.warning("monitor.dev_sync.failed", error=str(exc)[:120])
    if settings.LOG_MONITOR_ENABLED:
        from app.services.log_monitor.engine import monitor_engine
        monitor_engine.start()
        logger.info("monitor_engine.started")
    yield
    if settings.LOG_MONITOR_ENABLED:
        from app.services.log_monitor.engine import monitor_engine
        monitor_engine.stop()
    stop_scheduler()
    logger.info("shore.shutdown")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Shore AIOps — 告警、发布、日志、巡检、拓扑",
    lifespan=lifespan,
    docs_url="/docs",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- Routers ----
app.include_router(health.router, prefix="/api")
app.include_router(home.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(topology.router, prefix="/api")
app.include_router(incidents.router, prefix="/api")
app.include_router(knowledge.router, prefix="/api")
app.include_router(settings_api.router, prefix="/api")
app.include_router(security.router, prefix="/api")
app.include_router(deployments.router, prefix="/api")
app.include_router(monitor.router, prefix="/api")
app.include_router(inspection.router, prefix="/api")
app.include_router(ops.router, prefix="/api")


@app.get("/")
async def root():
    return {"app": settings.APP_NAME, "version": settings.APP_VERSION, "docs": "/docs"}
