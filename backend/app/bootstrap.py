# ============================================================
# app/bootstrap.py — 初始化数据库（建 pgvector 扩展 + 建表）
# 供 docker compose 启动时执行：python -m app.bootstrap
# ============================================================

import asyncio

from sqlalchemy import select, text

from app.core.database import AsyncSessionLocal, Base, engine
from app.core.logging import get_logger, setup_logging
from app.core.security import hash_password
import app.models  # noqa: F401  注册所有表
from app.models.auth import User

logger = get_logger("bootstrap")


async def init_db():
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        # 已有库补列（create_all 不会 alter）
        for stmt in (
            "ALTER TABLE deployments ADD COLUMN IF NOT EXISTS project VARCHAR(200) DEFAULT ''",
            "ALTER TABLE deployments ADD COLUMN IF NOT EXISTS build_status VARCHAR(20) DEFAULT ''",
            "ALTER TABLE deployments ADD COLUMN IF NOT EXISTS build_duration_sec INTEGER DEFAULT 0",
            "ALTER TABLE deployments ADD COLUMN IF NOT EXISTS is_latest BOOLEAN DEFAULT TRUE",
            "ALTER TABLE deployments ADD COLUMN IF NOT EXISTS optimization_tips TEXT DEFAULT ''",
            "ALTER TABLE monitor_tasks ADD COLUMN IF NOT EXISTS environment_id VARCHAR(32) DEFAULT 'test'",
            "ALTER TABLE clusters ADD COLUMN IF NOT EXISTS kubeconfig TEXT",
        ):
            await conn.execute(text(stmt))
    logger.info("bootstrap.tables.created")


async def seed():
    """仅创建初始管理员用户（幂等）。不含任何示例数据。"""
    import os
    async with AsyncSessionLocal() as db:
        existing = (await db.execute(select(User).limit(1))).scalar_one_or_none()
        if existing:
            logger.info("bootstrap.seed.skip", reason="already seeded")
        else:
            admin_pass = os.environ.get("ADMIN_PASSWORD", "admin123")
            db.add(User(username="admin", password_hash=hash_password(admin_pass),
                        display_name="管理员", role="admin"))
            await db.commit()
            logger.info("bootstrap.seed.done")
    await sync_dev_monitor_tasks()


async def sync_dev_monitor_tasks():
    """为每个 test 监控任务克隆一份 dev（其余配置相同）。"""
    from app.models.monitor import MonitorTask

    async with AsyncSessionLocal() as db:
        await db.execute(text(
            "UPDATE monitor_tasks SET environment_id='test' "
            "WHERE environment_id IS NULL OR environment_id=''"
        ))
        test_tasks = (await db.execute(
            select(MonitorTask).where(MonitorTask.environment_id == "test")
        )).scalars().all()
        created = 0
        for tt in test_tasks:
            exists = (await db.execute(
                select(MonitorTask).where(
                    MonitorTask.environment_id == "dev",
                    MonitorTask.name == tt.name,
                )
            )).scalar_one_or_none()
            if exists:
                continue
            clone = MonitorTask(
                name=tt.name,
                enabled=tt.enabled,
                k8s_namespace=tt.k8s_namespace,
                k8s_kubeconfig=tt.k8s_kubeconfig,
                environment_id="dev",
                s3_archive_enabled=tt.s3_archive_enabled,
                s3_bucket=tt.s3_bucket,
                s3_region=tt.s3_region,
                s3_access_key=tt.s3_access_key,
                s3_secret_key=tt.s3_secret_key,
                s3_endpoint=tt.s3_endpoint,
                retention_days=tt.retention_days,
                alert_enabled=tt.alert_enabled,
                slack_webhook_url=tt.slack_webhook_url,
                poll_interval_seconds=tt.poll_interval_seconds,
                alert_keywords=list(tt.alert_keywords or []),
                immediate_keywords=list(tt.immediate_keywords or []),
                ignore_keywords=list(tt.ignore_keywords or []),
                record_only_keywords=list(tt.record_only_keywords or []),
                alert_threshold_count=tt.alert_threshold_count,
                alert_threshold_window=tt.alert_threshold_window,
                alert_silence_minutes=tt.alert_silence_minutes,
            )
            db.add(clone)
            created += 1
        await db.commit()
        if created:
            logger.info("bootstrap.monitor.dev_cloned", count=created)


async def main():
    setup_logging()
    await init_db()
    await seed()


if __name__ == "__main__":
    asyncio.run(main())
