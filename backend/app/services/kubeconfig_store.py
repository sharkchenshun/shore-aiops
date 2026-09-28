# ============================================================
# kubeconfig_store.py — K8s 凭证：设置页粘贴 → 入库 → 运行时只读 DB
# 不依赖宿主机 KUBECONFIG、不切换 context、不挂载本地文件
# ============================================================

from __future__ import annotations

import base64
import datetime as dt
from functools import lru_cache
from typing import Any

import yaml
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.logging import get_logger
from app.models.infra import Cluster
from app.services.environments import EnvironmentProfile, list_profiles

logger = get_logger("kubeconfig_store")


def _profile_for_cluster(cluster_name: str) -> EnvironmentProfile | None:
    for profile in list_profiles():
        if profile.cluster_name == cluster_name:
            return profile
    return None


def apply_api_server_override(data: dict, profile: EnvironmentProfile | None) -> dict:
    """将 kubeconfig server 替换为 environments.json 中的 k8s_api_server（容器可访问的内网 IP）。"""
    server = (profile.k8s_api_server if profile else "") or ""
    server = server.strip()
    if not server or not isinstance(data, dict):
        return data
    import copy
    out = copy.deepcopy(data)
    for cluster in out.get("clusters") or []:
        cluster_cfg = cluster.get("cluster")
        if isinstance(cluster_cfg, dict):
            old = cluster_cfg.get("server", "")
            if old != server:
                cluster_cfg["server"] = server
                logger.debug(
                    "kubeconfig.server.override",
                    env=getattr(profile, "id", ""),
                    old=old,
                    new=server,
                )
    return out


def _prepare_kubeconfig_dict(raw: str | dict, profile: EnvironmentProfile | None) -> dict:
    data = yaml.safe_load(raw) if isinstance(raw, str) else raw
    if not isinstance(data, dict):
        raise ValueError("kubeconfig 格式无效")
    return apply_api_server_override(data, profile)


def _prepare_kubeconfig_yaml(raw: str, profile: EnvironmentProfile | None) -> str:
    data = _prepare_kubeconfig_dict(raw, profile)
    text = yaml.safe_dump(data, default_flow_style=False)
    return text if text.endswith("\n") else f"{text}\n"


def k8s_config_from_content(content: str | None) -> dict[str, Any]:
    if settings.K8S_IN_CLUSTER:
        return {"in_cluster": True}
    if content:
        return {"kubeconfig_content": content}
    return {}


@lru_cache(maxsize=1)
def _sync_session_factory() -> sessionmaker | None:
    url = settings.sync_database_url
    if not url:
        return None
    engine = create_engine(url, pool_pre_ping=True)
    return sessionmaker(bind=engine, expire_on_commit=False)


def get_cluster_kubeconfig_sync(cluster_name: str) -> str | None:
    factory = _sync_session_factory()
    if factory is None:
        return None
    with factory() as db:
        row = db.execute(select(Cluster).where(Cluster.name == cluster_name)).scalar_one_or_none()
        return row.kubeconfig if row and row.kubeconfig else None


def resolve_k8s_config_sync(profile: EnvironmentProfile) -> dict[str, Any]:
    if settings.K8S_IN_CLUSTER:
        return {"in_cluster": True}
    content = get_cluster_kubeconfig_sync(profile.cluster_name)
    if not content:
        return {}
    try:
        data = _prepare_kubeconfig_dict(content, profile)
        return k8s_config_from_content(yaml.safe_dump(data, default_flow_style=False))
    except Exception:
        return k8s_config_from_content(content)


async def _get_or_create_cluster(db: AsyncSession, cluster_name: str) -> Cluster:
    row = (await db.execute(select(Cluster).where(Cluster.name == cluster_name))).scalar_one_or_none()
    if row is None:
        row = Cluster(name=cluster_name, provider="kubernetes")
        db.add(row)
        await db.flush()
    return row


async def get_cluster_kubeconfig(db: AsyncSession, profile: EnvironmentProfile) -> str | None:
    row = (await db.execute(select(Cluster).where(Cluster.name == profile.cluster_name))).scalar_one_or_none()
    return row.kubeconfig if row and row.kubeconfig else None


async def resolve_k8s_config(db: AsyncSession, profile: EnvironmentProfile) -> dict[str, Any]:
    """运行时 K8s 连接：只读 clusters.kubeconfig（设置页粘贴入库）。"""
    if settings.K8S_IN_CLUSTER:
        return {"in_cluster": True}
    content = await get_cluster_kubeconfig(db, profile)
    if not content:
        return {}
    try:
        data = _prepare_kubeconfig_dict(content, profile)
        return k8s_config_from_content(yaml.safe_dump(data, default_flow_style=False))
    except Exception:
        return k8s_config_from_content(content)


async def save_cluster_kubeconfig(db: AsyncSession, profile: EnvironmentProfile, content: str) -> None:
    """设置页粘贴 kubeconfig → 校验连通性 → 入库。"""
    text = (content or "").strip()
    if not text:
        raise ValueError("kubeconfig 不能为空")
    try:
        data = _prepare_kubeconfig_dict(text, profile)
    except yaml.YAMLError as exc:
        raise ValueError(f"kubeconfig 格式无效: {exc}") from exc
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    if not data.get("clusters"):
        raise ValueError("kubeconfig 缺少 clusters 配置")
    _verify_kubeconfig_connection(data)
    cluster = await _get_or_create_cluster(db, profile.cluster_name)
    cluster.kubeconfig = yaml.safe_dump(data, default_flow_style=False)
    if not cluster.kubeconfig.endswith("\n"):
        cluster.kubeconfig += "\n"
    await db.flush()
    logger.info("kubeconfig.saved", env=profile.id, cluster=profile.cluster_name)


def _client_cert_expiry_hint(data: dict) -> str:
    try:
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend

        for user_entry in data.get("users") or []:
            cert_b64 = (user_entry.get("user") or {}).get("client-certificate-data")
            if not cert_b64:
                continue
            cert = x509.load_pem_x509_certificate(
                base64.b64decode(cert_b64), default_backend()
            )
            now = dt.datetime.now(dt.timezone.utc)
            after = getattr(cert, "not_valid_after_utc", None)
            if after is None:
                after = cert.not_valid_after
                if after.tzinfo is None:
                    after = after.replace(tzinfo=dt.timezone.utc)
            if now > after:
                return f"客户端证书已于 {after.strftime('%Y-%m-%d %H:%M UTC')} 过期，需向集群管理员重新签发"
            days = (after - now).days
            if days <= 14:
                return f"客户端证书将于 {after.strftime('%Y-%m-%d %H:%M UTC')} 过期（剩余 {days} 天）"
    except Exception:
        pass
    return ""


def core_v1_from_kubeconfig_dict(data: dict):
    """独立 Configuration，避免改进程默认 kubeconfig（设置页探测和采集线程会并发）。"""
    from kubernetes import client as k8s_client
    from kubernetes import config as k8s_config

    configuration = k8s_client.Configuration()
    k8s_config.load_kube_config_from_dict(data, client_configuration=configuration)
    return k8s_client.CoreV1Api(k8s_client.ApiClient(configuration))


def _verify_kubeconfig_connection(data: dict) -> None:
    try:
        api = core_v1_from_kubeconfig_dict(data)
        api.list_namespace(limit=1, _request_timeout=10)
    except ImportError as exc:
        raise ValueError("kubernetes 包未安装，无法校验 kubeconfig") from exc
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
        cert_hint = _client_cert_expiry_hint(data)
        if "401" in err or "Unauthorized" in err:
            msg = "kubeconfig 认证失败 (401 Unauthorized)，请检查 token/证书是否过期"
            if cert_hint:
                msg = f"{msg}；{cert_hint}"
            raise ValueError(msg) from exc
        extra = f"；{cert_hint}" if cert_hint else ""
        raise ValueError(f"kubeconfig 连接测试失败: {err[:240]}{extra}") from exc


async def cluster_has_kubeconfig(db: AsyncSession, profile: EnvironmentProfile) -> bool:
    if settings.K8S_IN_CLUSTER:
        return True
    return bool(await get_cluster_kubeconfig(db, profile))


def any_cluster_kubeconfig_configured_sync() -> bool:
    """是否有任意环境已在 DB 配置 kubeconfig（供安全扫描等）。"""
    if settings.K8S_IN_CLUSTER:
        return True
    for profile in list_profiles():
        if get_cluster_kubeconfig_sync(profile.cluster_name):
            return True
    return False


def check_cluster_kubeconfig_auth_sync(cluster_name: str) -> dict[str, str]:
    if settings.K8S_IN_CLUSTER:
        return {"status": "ok", "detail": "in_cluster"}
    try:
        content = get_cluster_kubeconfig_sync(cluster_name)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "detail": str(exc)[:200]}
    if not content:
        return {"status": "missing", "detail": "未配置 kubeconfig，请在 设置 → K8s 集群凭证 粘贴"}
    try:
        data = yaml.safe_load(content)
        profile = _profile_for_cluster(cluster_name)
        data = apply_api_server_override(data, profile)
        cert_hint = _client_cert_expiry_hint(data)
        if cert_hint.startswith("客户端证书已于"):
            return {"status": "error", "detail": cert_hint}
        _verify_kubeconfig_connection(data)
        detail = "connected"
        server = (data.get("clusters") or [{}])[0].get("cluster", {}).get("server", "")
        if server:
            detail = f"connected ({server})"
        if cert_hint:
            detail = f"{detail}（{cert_hint}）"
        return {"status": "ok", "detail": detail}
    except ValueError as exc:
        return {"status": "error", "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "detail": str(exc)[:200]}


def require_kubeconfig_content(profile: EnvironmentProfile) -> str:
    """同步路径取 kubeconfig 文本；缺失时抛明确错误。"""
    if settings.K8S_IN_CLUSTER:
        raise RuntimeError("in-cluster 模式请使用集群内 ServiceAccount")
    content = get_cluster_kubeconfig_sync(profile.cluster_name)
    if not content:
        raise RuntimeError(
            f"{profile.label}({profile.id}) 未配置 kubeconfig，"
            f"请在 设置 → K8s 集群凭证 粘贴 {profile.label} 集群凭证"
        )
    return _prepare_kubeconfig_yaml(content, profile)
