# ============================================================
# app/providers/kubernetes.py — K8s 自动发现 + 桥接发现
# 发现 Deployment/Service/Pod/Node，并从 Endpoints/ConfigMap/Env
# 三类信号中提取 K8s→VM 中间件桥接依赖
# ============================================================

import re
from typing import Any

from app.core.logging import get_logger
from app.providers.base import (
    DiscoveredMiddleware,
    DiscoveredNode,
    DiscoveredService,
    DiscoveryResult,
    InfrastructureProvider,
)

logger = get_logger("provider.k8s")

# 常见中间件端口 → 类型映射（用于桥接发现）
PORT_TO_MIDDLEWARE = {
    3306: "mysql", 5432: "postgresql", 6379: "redis",
    5672: "rabbitmq", 27017: "mongodb", 9200: "elasticsearch",
}

# 连接串环境变量正则（从 Pod env / ConfigMap 提取外部依赖）
CONN_ENV_PATTERN = re.compile(
    r"(?:HOST|URL|ADDR|ENDPOINT|SERVER)", re.IGNORECASE
)
HOST_PORT_PATTERN = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})(?::(\d+))?")
HOSTNAME_PORT_PATTERN = re.compile(r"([\w.-]+):(\d{2,5})\b")
JDBC_URL_PATTERN = re.compile(
    r"jdbc:(?:mysql|mariadb|postgresql)://([^:/]+):?(\d+)?", re.IGNORECASE,
)
REDIS_URL_PATTERN = re.compile(r"redis(?:s)?://([^:/]+):?(\d+)?", re.IGNORECASE)
ES_URL_PATTERN = re.compile(r"https?://([^:/]+):?(9200|9243)\b", re.IGNORECASE)
ANSI_ESCAPE_PATTERN = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


class KubernetesProvider(InfrastructureProvider):
    provider_type = "kubernetes"

    def _load_client(self):
        """延迟加载 k8s client；优先使用已入库的 kubeconfig 内容。"""
        from kubernetes import client, config as k8s_config
        import yaml

        in_cluster = self.config.get("in_cluster", False)
        kubeconfig_content = self.config.get("kubeconfig_content")
        try:
            if in_cluster:
                k8s_config.load_incluster_config()
            elif kubeconfig_content:
                cfg_dict = yaml.safe_load(kubeconfig_content)
                if isinstance(cfg_dict, dict):
                    k8s_config.load_kube_config_from_dict(cfg_dict)
                else:
                    raise ValueError("invalid kubeconfig content")
            else:
                raise ValueError(
                    "未配置 kubeconfig，请在 设置 → K8s 集群凭证 粘贴对应环境凭证"
                )
        except Exception as e:
            logger.error("k8s.config.load_failed", error=str(e))
            raise
        return client.CoreV1Api(), client.AppsV1Api()

    async def discover(self) -> DiscoveryResult:
        core, apps = self._load_client()
        result = DiscoveryResult(cluster_name=self.name, provider=self.provider_type)

        # ---- Nodes ----
        for node in core.list_node().items:
            addr = next((a.address for a in (node.status.addresses or [])
                         if a.type == "InternalIP"), "")
            is_cp = "node-role.kubernetes.io/control-plane" in (node.metadata.labels or {})
            ready = any(c.type == "Ready" and c.status == "True"
                        for c in (node.status.conditions or []))
            cap = node.status.capacity or {}
            result.nodes.append(DiscoveredNode(
                name=node.metadata.name,
                internal_ip=addr,
                role="control-plane" if is_cp else "worker",
                cpu_capacity=self._parse_cpu(cap.get("cpu")),
                mem_capacity_gb=self._parse_mem(cap.get("memory")),
                health="healthy" if ready else "critical",
            ))

        # ---- Deployments (services) ----
        namespaces = self.config.get("namespaces")  # None = 全部
        deps = (apps.list_deployment_for_all_namespaces().items if not namespaces
                else [d for ns in namespaces for d in apps.list_namespaced_deployment(ns).items])

        for d in deps:
            spec_replicas = d.spec.replicas or 0
            ready = d.status.ready_replicas or 0
            containers = d.spec.template.spec.containers or []
            image = containers[0].image if containers else ""
            version = image.split(":")[-1] if ":" in image else "latest"

            if ready == 0 and spec_replicas > 0:
                health = "critical"
            elif ready < spec_replicas:
                health = "degraded"
            else:
                health = "healthy"

            svc = DiscoveredService(
                name=d.metadata.name,
                namespace=d.metadata.namespace,
                replicas=spec_replicas,
                ready_replicas=ready,
                image=image,
                version=version,
                health=health,
                labels=d.metadata.labels or {},
            )
            # 桥接发现：从容器 env 提取外部中间件依赖
            for c in containers:
                for env in (c.env or []):
                    self._extract_bridge(svc, env, result)
            result.services.append(svc)

        # ---- Endpoints 桥接发现：Service 指向外部 IP ----
        try:
            for ep in core.list_endpoints_for_all_namespaces().items:
                for subset in (ep.subsets or []):
                    for addr in (subset.addresses or []):
                        # 无 targetRef 通常表示外部端点（如 VM 上的 DB）
                        if addr.target_ref is None and addr.ip:
                            for port in (subset.ports or []):
                                mw_type = PORT_TO_MIDDLEWARE.get(port.port)
                                if mw_type:
                                    result.middlewares.append(DiscoveredMiddleware(
                                        name=f"{ep.metadata.name}-{mw_type}",
                                        type=mw_type, host=addr.ip, port=port.port,
                                        discovered_from="endpoints",
                                    ))
                                    result.bridges.append(
                                        (ep.metadata.name, addr.ip, port.port, "endpoints"))
        except Exception as e:  # noqa: BLE001
            logger.warning("k8s.endpoints.discover_failed", error=str(e))

        logger.info("k8s.discover.done", cluster=self.name,
                    nodes=len(result.nodes), services=len(result.services),
                    middlewares=len(result.middlewares))
        return result

    def _extract_bridge(self, svc: DiscoveredService, env: Any, result: DiscoveryResult):
        """从环境变量中提取 host:port 形式的外部依赖（IP、域名、JDBC/Redis URL）。"""
        raw = (env.value or "").strip()
        if not raw:
            return
        name = env.name or ""

        host, port = "", 0
        for pattern, default_port in (
            (JDBC_URL_PATTERN, 3306),
            (REDIS_URL_PATTERN, 6379),
            (ES_URL_PATTERN, 9200),
        ):
            m = pattern.search(raw)
            if m:
                host = m.group(1)
                port = int(m.group(2)) if m.lastindex and m.group(2) else default_port
                break

        if not host and CONN_ENV_PATTERN.search(name):
            m = HOST_PORT_PATTERN.search(raw)
            if m:
                host = m.group(1)
                port = int(m.group(2)) if m.group(2) else 0
            else:
                m = HOSTNAME_PORT_PATTERN.search(raw)
                if m:
                    host, port = m.group(1), int(m.group(2))

        if not host:
            return
        mw_type = PORT_TO_MIDDLEWARE.get(port, "unknown")
        if mw_type == "unknown" and port == 9200:
            mw_type = "elasticsearch"
        result.middlewares.append(DiscoveredMiddleware(
            name=f"{svc.name}-dep-{host}", type=mw_type,
            host=host, port=port, discovered_from="env",
        ))
        result.bridges.append((svc.name, host, port, "env"))

    async def list_pods(self, namespace: str, label_selector: str = "") -> list[dict]:
        """列出指定 namespace 的 Pod（可按标签筛选）。"""
        core, _ = self._load_client()
        pods = core.list_namespaced_pod(namespace=namespace, label_selector=label_selector)
        result = []
        for p in pods.items:
            containers = []
            for c in (p.spec.containers or []):
                usage = {}
                if c.resources and c.resources.requests:
                    usage["cpu_req"] = self._parse_cpu(getattr(c.resources.requests, "cpu", None))
                    usage["mem_req_mb"] = self._parse_mem(getattr(c.resources.requests, "memory", None))
                if c.resources and c.resources.limits:
                    usage["cpu_limit"] = self._parse_cpu(getattr(c.resources.limits, "cpu", None))
                    usage["mem_limit_mb"] = self._parse_mem(getattr(c.resources.limits, "memory", None))
                containers.append({"name": c.name, "image": c.image, "ready": any(
                    s.ready for s in (p.status.container_statuses or []) if s.name == c.name
                ), **usage})
            result.append({
                "name": p.metadata.name, "namespace": p.metadata.namespace,
                "status": p.status.phase, "node": p.spec.node_name,
                "ip": p.status.pod_ip, "host_ip": p.status.host_ip,
                "restarts": sum(s.restart_count for s in (p.status.container_statuses or [])),
                "created_at": str(p.metadata.creation_timestamp),
                "containers": containers, "labels": p.metadata.labels or {},
            })
        return result

    async def get_pod_logs(self, namespace: str, pod_name: str, container: str = "", tail_lines: int = 200) -> str:
        """获取 Pod 日志，返回清洗后的纯文本。"""
        core, _ = self._load_client()
        kwargs = {"name": pod_name, "namespace": namespace, "tail_lines": tail_lines}
        if container:
            kwargs["container"] = container
        try:
            raw = core.read_namespaced_pod_log(**kwargs)
            return _clean_log_text(raw)
        except Exception:
            return "(日志获取失败)"

    async def get_pod_metrics(self, namespace: str, pod_name: str) -> dict | None:
        """从 metrics-server 获取 Pod CPU/内存实时数据。"""
        try:
            from kubernetes.client import CustomObjectsApi
            api = CustomObjectsApi()
            metrics = api.get_namespaced_custom_object(
                group="metrics.k8s.io", version="v1beta1",
                namespace=namespace, plural="pods", name=pod_name,
            )
            containers = []
            for c in metrics.get("containers", []):
                containers.append({
                    "name": c["name"],
                    "cpu_m": _parse_metric_cpu(c.get("usage", {}).get("cpu", "0")),
                    "mem_mb": _parse_metric_mem(c.get("usage", {}).get("memory", "0")),
                })
            return {"pod": pod_name, "containers": containers}
        except Exception:
            return None

    async def restart_pod(self, namespace: str, pod_name: str) -> bool:
        """重启 Pod（删除 → Deployment 自动重建）。"""
        core, _ = self._load_client()
        try:
            core.delete_namespaced_pod(name=pod_name, namespace=namespace)
            return True
        except Exception as e:
            logger.warning("pod.restart.failed", pod=pod_name, error=str(e))
            return False

    async def health_check(self) -> dict:
        try:
            core, _ = self._load_client()
            core.list_namespace(limit=1)
            return {"status": "ok", "provider": self.provider_type, "cluster": self.name}
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "error": str(e)}

    @staticmethod
    def _parse_cpu(v: str | None) -> float | None:
        if not v:
            return None
        return int(v[:-1]) / 1000 if v.endswith("m") else float(v)

    @staticmethod
    def _parse_mem(v: str | None) -> float | None:
        if not v:
            return None
        units = {"Ki": 1 / 1024 / 1024, "Mi": 1 / 1024, "Gi": 1, "Ti": 1024}
        for u, factor in units.items():
            if v.endswith(u):
                return round(float(v[:-len(u)]) * factor, 2)
        try:
            return round(float(v) / 1024 / 1024 / 1024, 2)
        except ValueError:
            return None


def _clean_log_text(raw: Any) -> str:
    """把 Kubernetes client 返回的日志清洗成前端可读纯文本。"""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = str(raw)
    # 某些 client/代理会把 bytes repr 当字符串返回：b'line\\n...'
    if (text.startswith("b'") and text.endswith("'")) or (text.startswith('b"') and text.endswith('"')):
        import ast
        try:
            value = ast.literal_eval(text)
            if isinstance(value, bytes):
                text = value.decode("utf-8", errors="replace")
        except Exception:
            text = text[2:-1]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = ANSI_ESCAPE_PATTERN.sub("", text)
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def _parse_metric_cpu(v: str) -> float:
    """解析 metrics-server CPU 值为毫核。如 '250m' → 250, '36149171n' → 36."""
    if not v:
        return 0.0
    if v.endswith("n"):
        return round(float(v[:-1]) / 1_000_000, 1)
    if v.endswith("u"):
        return round(float(v[:-1]) / 1_000, 1)
    if v.endswith("m"):
        return float(v[:-1])
    return float(v) * 1000


def _parse_metric_mem(v: str) -> float:
    """解析 metrics-server 内存值为 MB。如 '128974848' → ~123, '256Mi' → 256"""
    if not v:
        return 0.0
    if v.endswith("Ki"):
        return round(float(v[:-2]) / 1024, 1)
    if v.endswith("Mi"):
        return float(v[:-2])
    if v.endswith("Gi"):
        return float(v[:-2]) * 1024
    try:
        return round(float(v) / 1024 / 1024, 1)
    except ValueError:
        return 0.0
