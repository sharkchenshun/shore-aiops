# ============================================================
# app/scheduler.py — 定时任务（APScheduler）
# 周期自动发现 + 每天凌晨全平台安全巡检
# ============================================================

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.logging import get_logger

logger = get_logger("scheduler")

_scheduler = None


async def _periodic_discovery():
    """周期自动发现（K8s）。kubeconfig 已入库后逐个环境发现。"""
    from app.engine.discovery import DiscoveryEngine
    from app.services.environments import list_profiles
    from app.services.kubeconfig_store import resolve_k8s_config

    profiles = list_profiles()
    if not profiles:
        return
    async with AsyncSessionLocal() as db:
        runnable = []
        for profile in profiles:
            cfg = await resolve_k8s_config(db, profile)
            if cfg.get("kubeconfig_content") or cfg.get("in_cluster"):
                runnable.append((profile, cfg))
        if not runnable:
            return
        for profile, cfg in runnable:
            try:
                await DiscoveryEngine(db).run(
                    "kubernetes",
                    profile.cluster_name,
                    cfg,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("scheduler.discovery.failed", env=profile.id, error=str(e))
        try:
            await db.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("scheduler.discovery.commit.failed", error=str(e))


async def _full_platform_scan():
    """每天凌晨全平台安全巡检。"""
    from app.agents.security.agent import SecurityAgent
    from app.models.security import ScanSession
    async with AsyncSessionLocal() as db:
        try:
            session = ScanSession(target="full-platform", scope="full_platform",
                                  status="init", triggered_by="scheduler")
            db.add(session)
            await db.flush()
            sid = str(session.id)
            await db.commit()
            await SecurityAgent(db).scan(sid)
            await db.commit()
            logger.info("scheduler.scan.done", session=sid)
        except Exception as e:  # noqa: BLE001
            logger.warning("scheduler.scan.failed", error=str(e))


async def _gitlab_ci_scan():
    """周期扫描 GitLab CI pipeline（只读 Token，无需改 .gitlab-ci.yml）。"""
    from app.services.gitlab_ci_scanner import run_gitlab_ci_scan
    try:
        result = await run_gitlab_ci_scan()
        if result.get("ingested"):
            logger.info("scheduler.gitlab_ci.done", ingested=result["ingested"])
    except Exception as e:  # noqa: BLE001
        logger.warning("scheduler.gitlab_ci.failed", error=str(e))


async def _monitor_patrol():
    if not settings.MONITOR_PATROL_ENABLED:
        return
    from app.agents.monitor.agent import MonitorAgent
    async with AsyncSessionLocal() as db:
        try:
            await MonitorAgent(db).patrol(triggered_by="scheduler")
            await db.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("scheduler.patrol.failed", error=str(e))


async def _cost_analysis():
    if not settings.COST_ANALYSIS_ENABLED:
        return
    from app.agents.cost.agent import CostAgent
    async with AsyncSessionLocal() as db:
        try:
            await CostAgent(db).analyze(triggered_by="scheduler")
            await db.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("scheduler.cost.failed", error=str(e))


async def _architecture_analysis():
    from app.agents.architecture.agent import ArchitectureAgent
    async with AsyncSessionLocal() as db:
        try:
            await ArchitectureAgent(db).analyze(triggered_by="scheduler")
            await db.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("scheduler.architecture.failed", error=str(e))


async def _daily_inspection():
    """每日 Prometheus 集群巡检（合并自 shark-Platform inspection）。"""
    if not settings.INSPECTION_ENABLED:
        return
    import asyncio
    from app.services.environments import list_profiles
    from app.services.inspection.engine import inspection_engine

    for profile in list_profiles():
        if not profile.enabled:
            continue
        try:
            await asyncio.to_thread(inspection_engine.run, profile.id)
            logger.info("scheduler.inspection.done", env=profile.id)
        except Exception as e:  # noqa: BLE001
            logger.warning("scheduler.inspection.failed", env=profile.id, error=str(e)[:160])


def start_scheduler():
    global _scheduler
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        _scheduler = AsyncIOScheduler(timezone="Asia/Tokyo")
        _scheduler.add_job(_periodic_discovery, IntervalTrigger(seconds=settings.DISCOVERY_INTERVAL),
                           id="discovery", replace_existing=True)
        _scheduler.add_job(_full_platform_scan, CronTrigger.from_crontab(settings.AUTO_SCAN_CRON),
                           id="full_scan", replace_existing=True)
        if settings.GITLAB_CI_SCAN_ENABLED and settings.GITLAB_URL and settings.GITLAB_TOKEN:
            _scheduler.add_job(
                _gitlab_ci_scan,
                IntervalTrigger(seconds=settings.GITLAB_CI_SCAN_INTERVAL),
                id="gitlab_ci_scan",
                replace_existing=True,
            )
        if settings.MONITOR_PATROL_ENABLED:
            _scheduler.add_job(
                _monitor_patrol,
                IntervalTrigger(seconds=settings.MONITOR_PATROL_INTERVAL),
                id="monitor_patrol",
                replace_existing=True,
            )
        if settings.COST_ANALYSIS_ENABLED:
            _scheduler.add_job(
                _cost_analysis,
                CronTrigger.from_crontab("0 6 * * *"),
                id="cost_analysis",
                replace_existing=True,
            )
        _scheduler.add_job(
            _architecture_analysis,
            CronTrigger.from_crontab(settings.ARCHITECTURE_CRON),
            id="architecture_analysis",
            replace_existing=True,
        )
        if settings.INSPECTION_ENABLED:
            _scheduler.add_job(
                _daily_inspection,
                CronTrigger.from_crontab(settings.INSPECTION_CRON, timezone="Asia/Shanghai"),
                id="daily_inspection",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=6 * 60 * 60,
            )
        _scheduler.start()
        logger.info("scheduler.started", discovery_interval=settings.DISCOVERY_INTERVAL,
                    scan_cron=settings.AUTO_SCAN_CRON,
                    gitlab_ci=bool(settings.GITLAB_CI_SCAN_ENABLED and settings.GITLAB_TOKEN),
                    monitor_patrol=settings.MONITOR_PATROL_ENABLED,
                    inspection=settings.INSPECTION_ENABLED)
    except Exception as e:  # noqa: BLE001
        logger.warning("scheduler.start.failed", error=str(e))


def stop_scheduler():
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
