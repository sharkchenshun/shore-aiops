# ============================================================
# app/services/log_monitor/store.py — 监控引擎同步 DB 访问
# ============================================================

from __future__ import annotations

from typing import Iterable
from uuid import UUID

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.models.monitor import MonitorTask

_sync_engine = None
_SessionLocal = None


def _ensure_engine():
    global _sync_engine, _SessionLocal
    if _sync_engine is None:
        _sync_engine = create_engine(settings.sync_database_url, pool_pre_ping=True)
        _SessionLocal = sessionmaker(
            bind=_sync_engine,
            expire_on_commit=False,
        )


def get_session() -> Session:
    _ensure_engine()
    return _SessionLocal()


def get_enabled_tasks() -> list[MonitorTask]:
    with get_session() as db:
        rows = list(db.scalars(select(MonitorTask).where(MonitorTask.enabled.is_(True))).all())
        for row in rows:
            db.expunge(row)
        return rows


def get_task(task_id: UUID | str) -> MonitorTask | None:
    with get_session() as db:
        row = db.get(MonitorTask, task_id)
        if row is not None:
            db.expunge(row)
        return row


def refresh_task(task: MonitorTask, fields: Iterable[str] | None = None) -> MonitorTask:
    with get_session() as db:
        merged = db.merge(task)
        if fields:
            db.refresh(merged, attribute_names=list(fields))
        else:
            db.refresh(merged)
        db.expunge(merged)
        return merged


def save_task(task: MonitorTask, fields: Iterable[str] | None = None) -> None:
    with get_session() as db:
        names = list(fields) if fields else None
        if names:
            persistent = db.get(MonitorTask, task.id)
            if persistent is None:
                return
            for f in names:
                if hasattr(task, f):
                    setattr(persistent, f, getattr(task, f))
            db.commit()
            for f in names:
                if hasattr(persistent, f):
                    setattr(task, f, getattr(persistent, f))
            return
        merged = db.merge(task)
        db.commit()
        db.expunge(merged)
