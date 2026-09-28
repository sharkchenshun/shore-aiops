# ============================================================
# app/api/settings_api.py — 集成健康检查 + 维护窗口
# ============================================================

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
import asyncio

from app.core.deps import get_request_environment, require_admin, require_user
from app.models.auth import User
from app.services.environments import (
    EnvironmentProfile,
    get_active_env_id,
    list_profiles,
    set_active_env_id,
)
from app.services.integration_checks import get_integration_checks
from app.services.kubeconfig_store import (
    check_cluster_kubeconfig_auth_sync,
    cluster_has_kubeconfig,
)
from app.services.maintenance import add_window, list_windows
from app.core.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/settings", tags=["settings"])


class MaintenanceRequest(BaseModel):
    service: str
    minutes: int = 60
    reason: str = ""


class ActiveEnvironmentRequest(BaseModel):
    id: str


@router.get("/environments")
async def list_environments(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    active = get_active_env_id()
    profiles = list_profiles()
    if profiles:
        configured = [await cluster_has_kubeconfig(db, p) for p in profiles]
        auths = await asyncio.gather(*[
            asyncio.to_thread(check_cluster_kubeconfig_auth_sync, p.cluster_name)
            for p in profiles
        ])
    else:
        configured, auths = [], []
    envs = []
    for p, ok, auth in zip(profiles, configured, auths):
        d = p.to_public_dict()
        d["kubeconfigConfigured"] = ok
        d["kubeconfigStatus"] = auth["status"]
        d["kubeconfigDetail"] = auth["detail"]
        envs.append(d)
    return {"active": active, "environments": envs}


class KubeconfigRequest(BaseModel):
    kubeconfig: str


@router.put("/environments/{env_id}/kubeconfig")
async def save_environment_kubeconfig(
    env_id: str,
    body: KubeconfigRequest,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin),
):
    """粘贴 kubeconfig 直接保存到数据库。"""
    from app.services.environments import get_profile
    from app.services.kubeconfig_store import save_cluster_kubeconfig

    profile = get_profile(env_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"unknown environment: {env_id}")
    try:
        await save_cluster_kubeconfig(db, profile, body.kubeconfig)
        await db.commit()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "environment": env_id, "clusterName": profile.cluster_name}


@router.put("/environments/active")
async def set_active_environment(
    body: ActiveEnvironmentRequest,
    _user: User = Depends(require_user),
):
    try:
        profile = set_active_env_id(body.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "active": profile.id, "environment": profile.to_public_dict()}


@router.get("/integrations")
async def integration_health(
    db: AsyncSession = Depends(get_db),
    refresh: bool = Query(False),
    env: EnvironmentProfile = Depends(get_request_environment),
    _user: User = Depends(require_user),
):
    checks = await get_integration_checks(db, refresh=refresh, profile=env)
    checks["environment"] = env.id
    checks["activeEnvironment"] = get_active_env_id()
    checks["environments"] = [p.to_public_dict() for p in list_profiles()]
    return checks


@router.get("/maintenance")
async def get_maintenance(_user: User = Depends(require_user)):
    return {"windows": list_windows()}


@router.post("/maintenance")
async def set_maintenance(body: MaintenanceRequest, _user: User = Depends(require_admin)):
    entry = add_window(body.service, body.minutes, body.reason)
    return {"status": "ok", "window": entry}
