# ============================================================
# app/services/inspection/store.py — 巡检引擎同步 DB 访问
# ============================================================

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.models.inspection import InspectionConfig, InspectionIgnore, InspectionReport
from app.services.environments import get_active_profile
from app.services.inspection.urlsafety import sanitize_http_url

_sync_engine = None
_SessionLocal = None


def _ensure_engine():
    global _sync_engine, _SessionLocal
    if _sync_engine is None:
        _sync_engine = create_engine(settings.DATABASE_URL_SYNC, pool_pre_ping=True)
        _SessionLocal = sessionmaker(bind=_sync_engine, expire_on_commit=False)


def get_session() -> Session:
    _ensure_engine()
    return _SessionLocal()


def resolve_env(env_id: str | None) -> str:
    if env_id:
        return env_id
    return get_active_profile().id


@dataclass
class ConfigData:
    environment_id: str = "test"
    prometheus_url: str | None = None
    ark_base_url: str | None = None
    ark_api_key: str | None = None
    ark_model_id: str | None = None
    ark_api_key_set: bool = False

    def redact(self) -> dict[str, Any]:
        return {
            "environment_id": self.environment_id,
            "prometheus_url": self.prometheus_url or "",
            "ark_base_url": self.ark_base_url or "",
            "ark_api_key": "",
            "ark_api_key_set": bool(self.ark_api_key_set or (self.ark_api_key or "").strip()),
            "ark_model_id": self.ark_model_id or "",
        }


@dataclass
class ReportRow:
    report_id: str
    content: dict = field(default_factory=dict)


def empty_config(env_id: str | None = None) -> ConfigData:
    eid = resolve_env(env_id)
    profile = get_active_profile(eid)
    try:
        prometheus = sanitize_http_url(profile.prometheus_url or settings.PROMETHEUS_URL or "")
    except ValueError:
        prometheus = ""
    return ConfigData(
        environment_id=eid,
        prometheus_url=prometheus,
        # 不预填平台 LLM 地址：否则会被当成自定义网关，且不会回退平台密钥。
        ark_base_url="",
        ark_api_key="",
        ark_model_id=settings.llm_model if settings.llm_configured else "",
        ark_api_key_set=False,
    )


def load_config(env_id: str | None = None) -> ConfigData:
    eid = resolve_env(env_id)
    with get_session() as db:
        row = db.scalars(
            select(InspectionConfig).where(InspectionConfig.environment_id == eid)
        ).first()
        if row is None:
            return empty_config(eid)
        prometheus = (row.prometheus_url or "").strip()
        if not prometheus:
            profile = get_active_profile(eid)
            prometheus = (profile.prometheus_url or settings.PROMETHEUS_URL or "").strip()
        try:
            prometheus = sanitize_http_url(prometheus)
        except ValueError:
            prometheus = ""
        try:
            ark_base = sanitize_http_url(row.ark_base_url)
        except ValueError:
            ark_base = ""
        return ConfigData(
            environment_id=eid,
            prometheus_url=prometheus,
            ark_base_url=ark_base,
            ark_api_key=row.ark_api_key,
            ark_model_id=row.ark_model_id,
            ark_api_key_set=bool((row.ark_api_key or "").strip()),
        )


def save_config(data: dict, env_id: str | None = None) -> ConfigData:
    eid = resolve_env(env_id)
    with get_session() as db:
        row = db.scalars(
            select(InspectionConfig).where(InspectionConfig.environment_id == eid)
        ).first()
        if row is None:
            row = InspectionConfig(environment_id=eid)
            db.add(row)
        if "prometheus_url" in data and data["prometheus_url"] is not None:
            raw = str(data["prometheus_url"]).strip()
            row.prometheus_url = sanitize_http_url(raw) if raw else ""
        if "ark_base_url" in data and data["ark_base_url"] is not None:
            raw = str(data["ark_base_url"]).strip()
            row.ark_base_url = sanitize_http_url(raw) if raw else ""
        if "ark_model_id" in data and data["ark_model_id"] is not None:
            row.ark_model_id = str(data["ark_model_id"]).strip()
        key = data.get("ark_api_key")
        if key is not None and str(key).strip():
            row.ark_api_key = str(key).strip()
        db.commit()
        db.refresh(row)
        prometheus = (row.prometheus_url or "").strip()
        if not prometheus:
            profile = get_active_profile(eid)
            prometheus = (profile.prometheus_url or settings.PROMETHEUS_URL or "").strip()
        try:
            prometheus = sanitize_http_url(prometheus)
        except ValueError:
            prometheus = ""
        try:
            ark_base = sanitize_http_url(row.ark_base_url)
        except ValueError:
            ark_base = ""
        return ConfigData(
            environment_id=eid,
            prometheus_url=prometheus,
            ark_base_url=ark_base,
            ark_api_key=row.ark_api_key,
            ark_model_id=row.ark_model_id,
            ark_api_key_set=bool((row.ark_api_key or "").strip()),
        )


def get_report(report_id: str, env_id: str | None = None) -> ReportRow | None:
    eid = resolve_env(env_id)
    with get_session() as db:
        row = db.scalars(
            select(InspectionReport).where(
                InspectionReport.environment_id == eid,
                InspectionReport.report_id == report_id,
            )
        ).first()
        if row is None:
            return None
        return ReportRow(report_id=row.report_id, content=dict(row.content or {}))


def list_reports(env_id: str | None = None) -> list[ReportRow]:
    eid = resolve_env(env_id)
    with get_session() as db:
        rows = db.scalars(
            select(InspectionReport)
            .where(InspectionReport.environment_id == eid)
            .order_by(InspectionReport.report_id.desc())
            .limit(366)
        ).all()
        return [ReportRow(report_id=r.report_id, content=dict(r.content or {})) for r in rows]


def list_reports_in_range(start_id: str, end_id: str, env_id: str | None = None) -> list[ReportRow]:
    eid = resolve_env(env_id)
    with get_session() as db:
        rows = db.scalars(
            select(InspectionReport)
            .where(
                InspectionReport.environment_id == eid,
                InspectionReport.report_id >= start_id,
                InspectionReport.report_id <= end_id,
            )
            .order_by(InspectionReport.report_id.asc())
        ).all()
        return [ReportRow(report_id=r.report_id, content=dict(r.content or {})) for r in rows]


def upsert_report(report_id: str, content: dict, env_id: str | None = None) -> None:
    eid = resolve_env(env_id)
    with get_session() as db:
        row = db.scalars(
            select(InspectionReport).where(
                InspectionReport.environment_id == eid,
                InspectionReport.report_id == report_id,
            )
        ).first()
        if row is None:
            db.add(InspectionReport(environment_id=eid, report_id=report_id, content=content))
        else:
            row.content = content
        db.commit()


def list_ignore_keys(env_id: str | None = None) -> list[str]:
    eid = resolve_env(env_id)
    with get_session() as db:
        rows = db.scalars(
            select(InspectionIgnore).where(InspectionIgnore.environment_id == eid)
        ).all()
        return [r.key for r in rows]


def list_ignores(env_id: str | None = None) -> list[dict]:
    eid = resolve_env(env_id)
    with get_session() as db:
        rows = db.scalars(
            select(InspectionIgnore)
            .where(InspectionIgnore.environment_id == eid)
            .order_by(InspectionIgnore.created_at.desc())
        ).all()
        return [_ignore_payload(r) for r in rows]


def upsert_ignore(payload: dict, env_id: str | None = None, username: str = "") -> dict:
    eid = resolve_env(env_id)
    key = str(payload.get("key") or "").strip()[:512]
    if not key:
        raise ValueError("key required")
    with get_session() as db:
        row = db.scalars(
            select(InspectionIgnore).where(
                InspectionIgnore.environment_id == eid,
                InspectionIgnore.key == key,
            )
        ).first()
        if row is None:
            row = InspectionIgnore(environment_id=eid, key=key)
            db.add(row)
        row.check_id = str(payload.get("check_id") or "")[:64]
        row.label = str(payload.get("label") or "")[:512]
        row.note = str(payload.get("note") or "")[:255]
        row.created_by = (username or "")[:128]
        db.commit()
        db.refresh(row)
        return _ignore_payload(row)


def delete_ignore(key: str, env_id: str | None = None) -> None:
    eid = resolve_env(env_id)
    with get_session() as db:
        row = db.scalars(
            select(InspectionIgnore).where(
                InspectionIgnore.environment_id == eid,
                InspectionIgnore.key == key,
            )
        ).first()
        if row is not None:
            db.delete(row)
            db.commit()


def _ignore_payload(row: InspectionIgnore) -> dict:
    return {
        "key": row.key,
        "check_id": row.check_id,
        "label": row.label,
        "note": row.note,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "environment_id": row.environment_id,
    }
