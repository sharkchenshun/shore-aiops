# ============================================================
# app/api/inspection.py — 集群巡检 API（合并自 shark-Platform inspection）
# ============================================================

from __future__ import annotations

from datetime import datetime, timedelta
import asyncio
import re
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from app.core.deps import get_request_environment, require_operator, require_user
from app.models.auth import User
from app.schemas.inspection import InspectionConfigUpdate, InspectionIgnoreCreate
from app.services.environments import EnvironmentProfile
from app.services.inspection import store as insp_store
from app.services.inspection.engine import inspection_engine

router = APIRouter(prefix="/inspection", tags=["inspection"])
_REPORT_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _valid_report_id(report_id: str) -> str:
    rid = (report_id or "").strip()
    if not _REPORT_ID_RE.match(rid):
        raise HTTPException(400, "invalid report id")
    return rid


def _env_id(profile: EnvironmentProfile) -> str:
    return profile.id


def _report_summary(report_id: str, content: dict) -> dict:
    health = content.get("health_summary") or content.get("risk_summary") or {}
    health_score = health.get("score")
    if health.get("level") == "unknown":
        health_score = None
    analysis = content.get("ai_analysis") or ""
    return {
        "report_id": report_id,
        "timestamp": content.get("timestamp") or "",
        "score": health_score,
        "verdict": content.get("verdict") or "",
        "findings_count": len(content.get("findings") or []),
        "summary": (
            content.get("verdict")
            or ((analysis[:100] + ("..." if analysis else "")) if analysis else "")
            or "暂无分析"
        ),
    }


@router.get("/config")
async def get_config(
    profile: EnvironmentProfile = Depends(get_request_environment),
    _user: User = Depends(require_user),
):
    return insp_store.load_config(_env_id(profile)).redact()


@router.post("/config")
async def save_config(
    body: InspectionConfigUpdate,
    profile: EnvironmentProfile = Depends(get_request_environment),
    _user: User = Depends(require_operator),
):
    try:
        cfg = insp_store.save_config(body.model_dump(exclude_unset=True), _env_id(profile))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"msg": "saved", **cfg.redact()}


@router.post("/run")
async def run_inspection(
    body: InspectionConfigUpdate | None = Body(None),
    profile: EnvironmentProfile = Depends(get_request_environment),
    _user: User = Depends(require_operator),
):
    env_id = _env_id(profile)
    if body:
        data = body.model_dump(exclude_unset=True)
        if data:
            try:
                insp_store.save_config(data, env_id)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
    return await asyncio.to_thread(inspection_engine.run, env_id)


@router.get("/reports")
async def list_reports(
    profile: EnvironmentProfile = Depends(get_request_environment),
    _user: User = Depends(require_user),
):
    rows = insp_store.list_reports(_env_id(profile))
    return {"items": [_report_summary(r.report_id, r.content) for r in rows]}


@router.get("/reports/aggregated")
async def aggregated_report(
    type: str = Query("weekly"),
    profile: EnvironmentProfile = Depends(get_request_environment),
    _user: User = Depends(require_user),
):
    rtype = (type or "weekly").strip().lower()
    if rtype not in {"weekly", "monthly"}:
        raise HTTPException(400, "type must be weekly or monthly")
    days = 30 if rtype == "monthly" else 7
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    start_date = today - timedelta(days=days)
    reports = insp_store.list_reports_in_range(
        start_date.strftime("%Y-%m-%d"),
        today.strftime("%Y-%m-%d"),
        _env_id(profile),
    )
    if not reports:
        raise HTTPException(404, "No data available for this period")

    total_score = 0
    count = 0
    scores_trend = []
    common_issues: dict[str, int] = {}
    for r in reports:
        content = r.content
        health = content.get("health_summary") or content.get("risk_summary") or {}
        health_score = health.get("score")
        reasons = health.get("reasons") or []
        scores_trend.append({"date": r.report_id, "score": health_score})
        if not isinstance(health_score, (int, float)):
            continue
        total_score += health_score
        count += 1
        for reason in reasons:
            if reason not in ["System Healthy", "resource_max=OK"]:
                common_issues[reason] = common_issues.get(reason, 0) + 1

    avg_score = round(total_score / count, 1) if count else 0
    sorted_issues = sorted(common_issues.items(), key=lambda x: x[1], reverse=True)[:5]
    return {
        "type": rtype,
        "start_date": start_date.strftime("%Y-%m-%d"),
        "end_date": today.strftime("%Y-%m-%d"),
        "average_score": avg_score,
        "report_count": count,
        "trend": scores_trend,
        "top_issues": [{"issue": k, "count": v} for k, v in sorted_issues],
    }


@router.get("/reports/{report_id}")
async def get_report(
    report_id: str,
    profile: EnvironmentProfile = Depends(get_request_environment),
    _user: User = Depends(require_user),
):
    rid = _valid_report_id(report_id)
    row = insp_store.get_report(rid, _env_id(profile))
    if row is None:
        raise HTTPException(404, "Report not found")
    return row.content


@router.get("/ignores")
async def list_ignores(
    profile: EnvironmentProfile = Depends(get_request_environment),
    _user: User = Depends(require_user),
):
    return {"items": insp_store.list_ignores(_env_id(profile))}


@router.post("/ignores")
async def add_ignore(
    body: InspectionIgnoreCreate,
    profile: EnvironmentProfile = Depends(get_request_environment),
    user: User = Depends(require_operator),
):
    try:
        item = insp_store.upsert_ignore(
            body.model_dump(),
            _env_id(profile),
            username=user.username or "",
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"msg": "ignored", "item": item}


@router.delete("/ignores")
async def remove_ignore(
    key: str = Query(""),
    profile: EnvironmentProfile = Depends(get_request_environment),
    _user: User = Depends(require_operator),
):
    if not key.strip():
        raise HTTPException(400, "key required")
    insp_store.delete_ignore(key.strip(), _env_id(profile))
    return {"msg": "removed"}
