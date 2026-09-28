# ============================================================
# architecture_overview.py — 业务架构蓝图（配置 + 实时健康）
# ============================================================

from __future__ import annotations

import json
import os
from functools import lru_cache

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.environments import EnvironmentProfile, ensure_cluster_discovered, get_active_profile, get_cluster_id
from app.models.incident import Incident
from app.models.infra import Cluster, Middleware, Service


def _config_path() -> str:
    custom = os.environ.get("ARCHITECTURE_CONFIG_PATH", "").strip()
    if custom and os.path.isfile(custom):
        return custom
    return os.path.join(os.path.dirname(__file__), "..", "..", "config", "environment-architecture.json")


@lru_cache
def load_architecture_blueprint() -> dict:
    path = _config_path()
    if not os.path.isfile(path):
        return {"title": "业务环境", "tagline": "", "layers": [], "flows": [], "infra": [], "namespaces": []}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _match_health(
    name: str,
    svc_map: dict,
    mw_by_type: dict,
    *,
    component_type: str = "",
) -> dict:
    """蓝图组件与发现数据对齐：服务用精确/前缀匹配，中间件按 type 匹配。"""
    key = (name or "").lower().strip()
    if not key:
        return {"health": "unknown", "found": False}

    is_middleware = component_type == "middleware" or key in mw_by_type
    if is_middleware:
        m = mw_by_type.get(key)
        if m:
            return {"health": m["health"], "host": m.get("host"), "found": True}
        return {"health": "unknown", "found": False}

    if key in svc_map:
        s = svc_map[key]
        return {
            "health": s["health"],
            "replicas": s.get("replicas"),
            "readyReplicas": s.get("readyReplicas"),
            "found": True,
        }

    # 蓝图名略短于 Deployment 名：exchange-match → exchange-match-service
    prefix = f"{key}-"
    candidates = [(k, s) for k, s in svc_map.items() if k.startswith(prefix)]
    if len(candidates) == 1:
        k, s = candidates[0]
        return {
            "health": s["health"],
            "replicas": s.get("replicas"),
            "readyReplicas": s.get("readyReplicas"),
            "found": True,
            "matchedName": k,
        }
    if len(candidates) > 1:
        # 多个前缀命中时取 ready 最低的健康状态（更保守）
        k, s = min(candidates, key=lambda x: (x[1].get("readyReplicas") or 0, x[0]))
        return {
            "health": s["health"],
            "replicas": s.get("replicas"),
            "readyReplicas": s.get("readyReplicas"),
            "found": True,
            "matchedName": k,
        }

    return {"health": "unknown", "found": False}


async def build_home_overview(db: AsyncSession, profile: EnvironmentProfile | None = None) -> dict:
    profile = profile or get_active_profile()
    cluster_id = await ensure_cluster_discovered(db, profile)
    blueprint = load_architecture_blueprint()

    svc_q = select(Service)
    if cluster_id:
        svc_q = svc_q.where(Service.cluster_id == cluster_id)
        services = (await db.execute(svc_q)).scalars().all()
    else:
        services = []
    middlewares = (await db.execute(select(Middleware))).scalars().all()
    clusters = (await db.execute(select(Cluster).where(Cluster.name == profile.cluster_name))).scalars().all()

    svc_map = {s.name.lower(): {
        "health": s.health, "replicas": s.replicas, "readyReplicas": s.ready_replicas, "namespace": s.namespace,
    } for s in services}
    mw_by_type: dict[str, dict] = {}
    for m in middlewares:
        t = (m.type or "").lower().strip()
        if not t:
            continue
        # 同类型取 health 最差的一条（critical > degraded > healthy）
        rank = {"critical": 0, "degraded": 1, "unknown": 2, "healthy": 3}.get(m.health or "unknown", 2)
        prev = mw_by_type.get(t)
        if prev is None or rank < prev.get("_rank", 99):
            mw_by_type[t] = {"health": m.health, "host": m.host, "_rank": rank}

    layers_out = []
    blueprint_found = 0
    blueprint_total = 0
    for layer in blueprint.get("layers", []):
        comps = []
        for c in layer.get("components", []):
            blueprint_total += 1
            live = _match_health(
                c.get("name", ""),
                svc_map,
                mw_by_type,
                component_type=c.get("type") or "",
            )
            if live.get("found"):
                blueprint_found += 1
            comps.append({**c, **{k: v for k, v in live.items() if not k.startswith("_")}})
        layers_out.append({**layer, "components": comps})

    active_incidents = (
        await db.execute(
            select(func.count()).select_from(Incident).where(Incident.status != "resolved")
        )
    ).scalar() or 0

    recent = (
        await db.execute(
            select(Incident).order_by(Incident.created_at.desc()).limit(10)
        )
    ).scalars().all()

    healthy = sum(1 for s in services if s.health == "healthy")
    degraded = sum(1 for s in services if s.health == "degraded")
    critical = sum(1 for s in services if s.health == "critical")

    from app.services.kubeconfig_store import check_cluster_kubeconfig_auth_sync
    k8s_auth = check_cluster_kubeconfig_auth_sync(profile.cluster_name)

    return {
        "environment": profile.id,
        "environmentLabel": profile.label,
        "clusterName": profile.cluster_name,
        "discoveryPending": cluster_id is None,
        "k8sAuthStatus": k8s_auth["status"],
        "k8sAuthDetail": k8s_auth["detail"],
        "discoveryStale": k8s_auth["status"] != "ok" and len(services) > 0,
        "stats": {
            "services": len(services),
            "middlewares": len(middlewares),
            "clusters": len(clusters),
            "nodes": sum(c.node_count or 0 for c in clusters),
            "healthy": healthy,
            "degraded": degraded,
            "critical": critical,
            "blueprintTotal": blueprint_total,
            "blueprintFound": blueprint_found,
            "blueprintMissing": blueprint_total - blueprint_found,
            "activeIncidents": active_incidents,
        },
        "architecture": {
            "title": blueprint.get("title", ""),
            "tagline": blueprint.get("tagline", ""),
            "layers": layers_out,
            "flows": blueprint.get("flows", []),
            "infra": blueprint.get("infra", []),
            "namespaces": blueprint.get("namespaces", []),
        },
        "incidents": [{
            "id": str(i.id),
            "title": i.title,
            "severity": i.severity,
            "status": i.status,
            "affectedServices": i.affected_services or [],
            "createdAt": i.created_at,
        } for i in recent],
    }
