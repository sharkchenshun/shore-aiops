# ============================================================
# app/agents/security/agent.py — 安全 Agent（全环境漏洞扫描 / 渗透测试编排）
# 流程：目标解析 → subfinder → httpx → nmap → nuclei
#       → AI 汇总分析 → 漏洞入库 → 高危自动建 Incident
# 两种触发：full_platform（全平台巡检）/ manual（手动输入域名/IP）
# 注意：这是网络层渗透与 CVE 检测，不是源代码 SAST。
# ============================================================

from datetime import datetime, timezone
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.llm import get_llm
from app.core.logging import get_logger
from app.models.security import Asset, ScanSession, Vulnerability
from app.services.scan_targets import collect_platform_targets
from app.tools.scanners import SCANNERS

logger = get_logger("agent.security")


class SecurityAgent:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def scan(self, session_id: str) -> dict:
        """执行一次扫描会话（由 API 预创建 ScanSession 后调用）。"""
        session = (await self.db.execute(
            select(ScanSession).where(ScanSession.id == session_id)
        )).scalar_one_or_none()
        if session is None:
            return {"error": "scan session not found"}

        tools_ok = {name: tool.available() for name, tool in SCANNERS.items()}
        session.tool_results = {"tools": tools_ok}
        session.status = "recon"
        session.started_at = datetime.now(timezone.utc)
        await self.db.flush()

        if not any(tools_ok.values()):
            session.status = "failed"
            session.report = (
                "扫描工具未就绪（nmap/nuclei/subfinder/httpx）。"
                "请在 Docker 镜像内运行 API：docker compose build api && docker compose up -d api"
            )
            session.completed_at = datetime.now(timezone.utc)
            await self.db.flush()
            return {"error": "scanners missing", "tools": tools_ok}

        target_meta: dict = {}
        if session.scope == "full_platform":
            targets, target_meta = await collect_platform_targets(self.db)
        else:
            targets = [t.strip() for t in session.target.split(",") if t.strip()]

        logger.info("security.scan.start", session=session_id, targets=len(targets), meta=target_meta)

        if not targets:
            session.status = "completed"
            session.completed_at = datetime.now(timezone.utc)
            session.report = (
                "全平台扫描：未发现可扫描目标。\n"
                "1) 在 设置 → K8s 集群凭证 粘贴各环境 kubeconfig，等待 discovery 写入节点/中间件\n"
                "2) 或在 .env 设置 SECURITY_SCAN_EXTRA_TARGETS=域名或IP\n"
                "3) 或使用「手动扫描」输入对外域名/Ingress"
            )
            session.tool_results = {**(session.tool_results or {}), "targetMeta": target_meta}
            await self.db.flush()
            return {"session_id": session_id, "assets": 0, "vulns": 0, "report": session.report}

        all_assets: list[dict] = []
        all_vulns: list[dict] = []

        for target in targets:
            session.status = "discovery"
            await self.db.flush()
            assets = await self._discover_assets(target)
            all_assets.extend(assets)

            session.status = "vuln_scan"
            await self.db.flush()
            scan_targets = [target] + [a["value"] for a in assets if a["type"] in ("subdomain", "url")]
            vulns, port_assets = await self._scan_vulns(scan_targets[:20])
            all_vulns.extend(vulns)
            all_assets.extend(port_assets)

        for a in all_assets:
            self.db.add(Asset(
                session_id=session_id, type=a["type"], value=a["value"],
                port=a.get("port"), service=a.get("service", ""),
                version=a.get("version", ""), extra=a.get("extra", {}),
            ))
        for v in all_vulns:
            self.db.add(Vulnerability(
                session_id=session_id, title=v["title"], severity=v["severity"],
                type=v.get("type", ""), target=v.get("target", ""),
                endpoint=v.get("endpoint", ""), found_by=v.get("found_by", "nuclei"),
                cve=v.get("cve") or "", cvss_score=v.get("cvss_score"),
                description=v.get("description", ""), recommendation=v.get("recommendation", ""),
            ))

        session.status = "analyzing"
        await self.db.flush()
        report = await self._ai_report(session.target, targets, all_assets, all_vulns, target_meta)

        critical = [v for v in all_vulns if v.get("severity") in ("critical", "high")]
        if critical:
            from app.services.incident_service import upsert_incident
            await upsert_incident(
                self.db, title=f"安全扫描发现 {len(critical)} 个高危漏洞", severity="critical",
                source="security_scan", affected_services=[session.target[:50]],
                detail={"session_id": session_id, "vuln_count": len(critical), "top": critical[:5]},
                fingerprint=f"security:{session_id}", actor="security_agent",
            )

        session.status = "completed"
        session.completed_at = datetime.now(timezone.utc)
        session.asset_count = len(all_assets)
        session.vuln_count = len(all_vulns)
        session.report = report
        session.tool_results = {
            **(session.tool_results or {}),
            "targetMeta": target_meta,
            "targetCount": len(targets),
        }
        await self.db.flush()

        logger.info("security.scan.done", session=session_id, assets=len(all_assets), vulns=len(all_vulns))
        return {"session_id": session_id, "assets": len(all_assets), "vulns": len(all_vulns), "report": report}

    async def _discover_assets(self, target: str) -> list[dict]:
        assets: list[dict] = []
        host = _scan_host(target)
        if not _is_ip(host):
            subs = await SCANNERS["subfinder"].run(host)
            assets.extend(subs)
            probe_targets = [host] + [s["value"] for s in subs][:30]
        else:
            probe_targets = [host]
        for t in probe_targets:
            live = await SCANNERS["httpx"].run(t)
            assets.extend(live)
        return assets

    async def _scan_vulns(self, targets: list[str]) -> tuple[list[dict], list[dict]]:
        vulns: list[dict] = []
        port_assets: list[dict] = []
        seen_nuclei: set[str] = set()

        for t in targets:
            nmap_host = _scan_host(t)
            ports = await SCANNERS["nmap"].run(nmap_host, options={"ports": "1-10000"})
            port_assets.extend(ports)

            for url in _nuclei_urls(t):
                if url in seen_nuclei:
                    continue
                seen_nuclei.add(url)
                found = await SCANNERS["nuclei"].run(url)
                vulns.extend(found)
        return vulns, port_assets

    async def _ai_report(
        self, target: str, targets: list[str], assets: list[dict], vulns: list[dict], meta: dict,
    ) -> str:
        if not vulns and not assets:
            hint = (
                f"已对 {len(targets)} 个目标执行 nmap/nuclei，未发现开放漏洞。\n"
                "可能原因：目标为内网 IP、nuclei 模板未更新、或目标不可达。"
            )
            if meta:
                hint += f"\n目标来源：{meta.get('sources', {})}"
            return hint

        sev_count: dict[str, int] = {}
        for v in vulns:
            sev_count[v["severity"]] = sev_count.get(v["severity"], 0) + 1
        summary = (
            f"扫描范围：{target}\n"
            f"目标数：{len(targets)}  资产数：{len(assets)}\n"
            f"漏洞分布：{sev_count}\n"
        )
        if meta:
            summary += f"目标来源：{meta.get('sources', {})}\n"

        top_vulns = "\n".join(
            f"- [{v['severity']}] {v['title']} @ {v.get('endpoint', v.get('target', ''))}"
            for v in sorted(vulns, key=lambda x: x.get("severity", "z"))[:15]
        )
        prompt = f"""你是渗透测试专家。基于以下全环境漏洞扫描结果生成简洁中文报告，
包含：暴露面概述、重点 CVE/配置问题、修复优先级。

{summary}
重点发现：
{top_vulns or '（无 nuclei 命中，请结合 nmap 开放端口人工复核）'}"""
        try:
            from langchain_core.messages import HumanMessage
            llm = get_llm(temperature=0.3)
            resp = await llm.ainvoke([HumanMessage(content=prompt)])
            return resp.content
        except Exception as e:  # noqa: BLE001
            logger.warning("security.report.failed", error=str(e))
            return summary + (top_vulns or "")


def _is_ip(s: str) -> bool:
    host = s.split(":")[0] if ":" in s and s.count(".") == 3 else s
    parts = host.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def _scan_host(target: str) -> str:
    if target.startswith("http://") or target.startswith("https://"):
        return urlparse(target).hostname or target
    if ":" in target and target.count(":") == 1:
        return target.split(":", 1)[0]
    return target


def _nuclei_urls(target: str) -> list[str]:
    if target.startswith("http://") or target.startswith("https://"):
        return [target]
    if ":" in target and target.count(":") == 1:
        host, port_s = target.rsplit(":", 1)
        if port_s.isdigit():
            port = int(port_s)
            if port == 443:
                return [f"https://{host}"]
            if port == 80:
                return [f"http://{host}"]
            return [f"http://{host}:{port}", f"https://{host}:{port}"]
    return [f"https://{target}", f"http://{target}"]
