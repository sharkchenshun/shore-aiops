# ============================================================
# app/services/environments.py — 多环境配置与当前环境切换
# 配置：backend/config/environments.json
# 当前环境：Redis platform:active_environment（默认读 JSON default）
# K8s 凭证：设置页粘贴 → clusters.kubeconfig（运行时只读 DB）
# ============================================================

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.models.infra import Cluster
from app.services.redis_cache import _get_client

logger = get_logger("environments")

REDIS_ACTIVE_KEY = "platform:active_environment"


@dataclass
class EnvironmentProfile:
    id: str
    label: str
    cluster_name: str
    prometheus_url: str = ""
    prometheus_ssl_verify: bool = False
    prometheus_username: str = ""
    es_host: str = ""
    es_port: int = 9200
    gitlab_ci_branches: list[str] = field(default_factory=list)
    argocd_server: str = ""
    k8s_api_server: str = ""  # 可选：覆盖 kubeconfig 内 server（内网 IP，容器可访问）
    enabled: bool = True

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "clusterName": self.cluster_name,
            "prometheusUrl": self.prometheus_url,
            "esHost": self.es_host,
            "gitlabCiBranches": self.gitlab_ci_branches,
            "argocdServer": self.argocd_server,
            "k8sApiServer": self.k8s_api_server,
            "enabled": self.enabled,
        }


def _config_path() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "config", "environments.json")


def _default_from_settings() -> tuple[str, list[EnvironmentProfile]]:
    """无 JSON 时从 .env 合成单环境。"""
    env_id = settings.ENVIRONMENT
    profile = EnvironmentProfile(
        id=env_id,
        label=env_id.upper(),
        cluster_name=settings.CLUSTER_NAME,
        prometheus_url=settings.PROMETHEUS_URL,
        prometheus_ssl_verify=settings.PROMETHEUS_SSL_VERIFY,
        prometheus_username=settings.PROMETHEUS_USERNAME,
        es_host=settings.ES_HOST,
        es_port=settings.ES_PORT,
        gitlab_ci_branches=settings.gitlab_ci_branches(),
    )
    return env_id, [profile]


def load_environments_config() -> tuple[str, list[EnvironmentProfile]]:
    path = _config_path()
    if not os.path.isfile(path):
        return _default_from_settings()
    try:
        raw = json.loads(open(path, encoding="utf-8").read())
    except Exception as exc:  # noqa: BLE001
        logger.warning("environments.load.failed", error=str(exc)[:120])
        return _default_from_settings()

    default_id = raw.get("default") or settings.ENVIRONMENT
    profiles: list[EnvironmentProfile] = []
    for item in raw.get("environments") or []:
        if not item.get("id"):
            continue
        branches = item.get("gitlab_ci_branches")
        if isinstance(branches, str):
            branches = [b.strip() for b in branches.split(",") if b.strip()]
        profiles.append(EnvironmentProfile(
            id=str(item["id"]),
            label=str(item.get("label") or item["id"]),
            cluster_name=str(item.get("cluster_name") or item["id"]),
            prometheus_url=str(item.get("prometheus_url") or ""),
            prometheus_ssl_verify=bool(item.get("prometheus_ssl_verify", False)),
            prometheus_username=str(item.get("prometheus_username") or ""),
            es_host=str(item.get("es_host") or ""),
            es_port=int(item.get("es_port") or 9200),
            gitlab_ci_branches=list(branches or []),
            argocd_server=str(item.get("argocd_server") or ""),
            k8s_api_server=str(item.get("k8s_api_server") or "").strip(),
            enabled=bool(item.get("enabled", True)),
        ))
    if not profiles:
        return _default_from_settings()
    if default_id not in {p.id for p in profiles}:
        default_id = profiles[0].id
    return default_id, profiles


def list_profiles() -> list[EnvironmentProfile]:
    _, profiles = load_environments_config()
    return [p for p in profiles if p.enabled]


def get_profile(env_id: str | None) -> EnvironmentProfile | None:
    if not env_id:
        return None
    for p in list_profiles():
        if p.id == env_id:
            return p
    return None


def get_default_env_id() -> str:
    default_id, _ = load_environments_config()
    return default_id


def get_active_env_id() -> str:
    client = _get_client()
    if client:
        try:
            val = client.get(REDIS_ACTIVE_KEY)
            if val and get_profile(val.decode() if isinstance(val, bytes) else str(val)):
                return val.decode() if isinstance(val, bytes) else str(val)
        except Exception as exc:  # noqa: BLE001
            logger.debug("environments.active.get.failed", error=str(exc)[:80])
    return get_default_env_id()


def set_active_env_id(env_id: str) -> EnvironmentProfile:
    profile = get_profile(env_id)
    if profile is None:
        raise ValueError(f"unknown environment: {env_id}")
    client = _get_client()
    if client:
        try:
            client.set(REDIS_ACTIVE_KEY, env_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("environments.active.set.failed", error=str(exc)[:80])
    return profile


def resolve_env_id(header_value: str | None = None) -> str:
    """请求头 X-Shore-Environment 优先，否则 Redis 当前环境。"""
    if header_value:
        hid = header_value.strip()
        if get_profile(hid):
            return hid
    return get_active_env_id()


def get_active_profile(header_value: str | None = None) -> EnvironmentProfile:
    env_id = resolve_env_id(header_value)
    profile = get_profile(env_id)
    if profile:
        return profile
    _, profiles = load_environments_config()
    return profiles[0]


async def get_cluster_id(db: AsyncSession, profile: EnvironmentProfile) -> str | None:
    row = (await db.execute(select(Cluster).where(Cluster.name == profile.cluster_name))).scalar_one_or_none()
    return str(row.id) if row else None


async def ensure_cluster_discovered(db: AsyncSession, profile: EnvironmentProfile) -> str | None:
    """当前环境集群未入库时触发一次 K8s Discovery。"""
    cid = await get_cluster_id(db, profile)
    if cid:
        return cid
    from app.services.kubeconfig_store import resolve_k8s_config
    k8s_cfg = await resolve_k8s_config(db, profile)
    if not k8s_cfg.get("kubeconfig_content") and not k8s_cfg.get("in_cluster"):
        logger.debug("discovery.ensure.skip", env=profile.id, reason="no kubeconfig")
        return None
    try:
        from app.engine.discovery import DiscoveryEngine
        await DiscoveryEngine(db).run("kubernetes", profile.cluster_name, k8s_cfg)
        await db.commit()
        logger.info("discovery.ensure.done", env=profile.id, cluster=profile.cluster_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("discovery.ensure.failed", env=profile.id, error=str(exc)[:120])
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
    return await get_cluster_id(db, profile)
