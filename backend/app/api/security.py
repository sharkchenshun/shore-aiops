# ============================================================
# app/api/security.py — 安全扫描 API（全环境漏洞 / 渗透测试）
# ============================================================

from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.security.agent import SecurityAgent
from app.core.config import settings
from app.core.database import AsyncSessionLocal, get_db
from app.core.deps import require_operator
from app.models.auth import User
from app.core.logging import get_logger
from app.models.security import Asset, ScanSession, Vulnerability
from app.schemas.security import ScanRequest
from app.tools.scanners import SCANNERS
from app.services.kubeconfig_store import any_cluster_kubeconfig_configured_sync

router = APIRouter(prefix="/security", tags=["security"])
logger = get_logger("api.security")


async def _run_scan(session_id: str):
    async with AsyncSessionLocal() as db:
        try:
            await SecurityAgent(db).scan(session_id)
            await db.commit()
        except Exception as e:  # noqa: BLE001
            logger.error("scan.background.failed", session=session_id, error=str(e))
            await db.rollback()
            async with AsyncSessionLocal() as db2:
                session = (await db2.execute(
                    select(ScanSession).where(ScanSession.id == session_id)
                )).scalar_one_or_none()
                if session and session.status not in ("completed", "failed"):
                    session.status = "failed"
                    session.report = f"扫描失败: {str(e)[:500]}"
                    session.completed_at = datetime.now(timezone.utc)
                    await db2.commit()


@router.get("/tools")
async def scanner_tools():
    """渗透/漏洞扫描工具可用性与诊断。"""
    network = {name: tool.available() for name, tool in SCANNERS.items()}
    return {
        "network": network,
        "k8sConfigured": any_cluster_kubeconfig_configured_sync(),
        "extraTargets": settings.security_scan_extra_targets(),
        "hints": _scan_hints(network),
    }


def _scan_hints(network: dict) -> list[str]:
    hints: list[str] = []
    if not any(network.values()):
        hints.append("扫描工具未安装 — 需 docker compose build api（镜像内含 nmap/nuclei/subfinder/httpx）")
    else:
        if not network.get("nuclei"):
            hints.append("nuclei 不可用 → CVE/Web 漏洞检测跳过")
        if not network.get("nmap"):
            hints.append("nmap 不可用 → 端口与服务识别跳过")
    if not any_cluster_kubeconfig_configured_sync():
        hints.append("未配置 K8s → 请在 设置 → K8s 集群凭证 粘贴各环境 kubeconfig")
    if not settings.security_scan_extra_targets():
        hints.append("可设置 SECURITY_SCAN_EXTRA_TARGETS 补充对外域名或 IP")
    if not hints:
        hints.append("工具就绪；全平台巡检 = 节点 IP + 中间件 + K8s 暴露面 + 额外目标")
    return hints


@router.post("/scans")
async def create_scan(req: ScanRequest, bg: BackgroundTasks, db: AsyncSession = Depends(get_db),
                      _user: User = Depends(require_operator)):
    """新建漏洞/渗透扫描（手动目标 或 全平台）。"""
    session = ScanSession(
        target=req.target or "full-platform",
        scope=req.scope,
        status="init",
        triggered_by=req.triggered_by or "manual",
    )
    db.add(session)
    await db.flush()
    sid = str(session.id)
    await db.commit()
    bg.add_task(_run_scan, sid)
    return {"status": "started", "session_id": sid, "scope": req.scope}


@router.get("/scans")
async def list_scans(db: AsyncSession = Depends(get_db), limit: int = Query(50, le=200)):
    rows = (await db.execute(
        select(ScanSession).order_by(ScanSession.created_at.desc()).limit(limit)
    )).scalars().all()
    return [{
        "id": str(s.id), "target": s.target, "scope": s.scope, "status": s.status,
        "assetCount": s.asset_count, "vulnCount": s.vuln_count,
        "reportPreview": (s.report or "")[:200],
        "startedAt": s.started_at, "completedAt": s.completed_at,
    } for s in rows]


@router.get("/scans/{session_id}")
async def get_scan(session_id: str, db: AsyncSession = Depends(get_db)):
    s = (await db.execute(select(ScanSession).where(ScanSession.id == session_id))).scalar_one_or_none()
    if s is None:
        return {"error": "not found"}
    vulns = (await db.execute(
        select(Vulnerability).where(Vulnerability.session_id == session_id)
    )).scalars().all()
    assets = (await db.execute(
        select(Asset).where(Asset.session_id == session_id)
    )).scalars().all()
    return {
        "id": str(s.id), "target": s.target, "scope": s.scope, "status": s.status,
        "report": s.report, "assetCount": s.asset_count, "vulnCount": s.vuln_count,
        "toolResults": s.tool_results or {},
        "vulnerabilities": [_vuln_dict(v) for v in vulns],
        "assets": [{
            "type": a.type, "value": a.value, "port": a.port,
            "service": a.service, "version": a.version,
        } for a in assets],
    }


@router.get("/vulnerabilities")
async def list_vulns(db: AsyncSession = Depends(get_db),
                     severity: str | None = None, status: str | None = None,
                     limit: int = Query(100, le=500)):
    q = select(Vulnerability).order_by(Vulnerability.created_at.desc()).limit(limit)
    if severity:
        q = q.where(Vulnerability.severity == severity)
    if status:
        q = q.where(Vulnerability.status == status)
    rows = (await db.execute(q)).scalars().all()
    return [_vuln_dict(v) for v in rows]


@router.get("/stats")
async def security_stats(db: AsyncSession = Depends(get_db)):
    by_sev = (await db.execute(
        select(Vulnerability.severity, func.count()).group_by(Vulnerability.severity)
    )).all()
    by_status = (await db.execute(
        select(Vulnerability.status, func.count()).group_by(Vulnerability.status)
    )).all()
    total_scans = (await db.execute(select(func.count()).select_from(ScanSession))).scalar()
    return {
        "bySeverity": {k: v for k, v in by_sev},
        "byStatus": {k: v for k, v in by_status},
        "totalScans": total_scans,
    }


def _vuln_dict(v: Vulnerability) -> dict:
    return {
        "id": str(v.id), "title": v.title, "severity": v.severity, "type": v.type,
        "target": v.target, "endpoint": v.endpoint, "foundBy": v.found_by,
        "cve": v.cve, "cvssScore": v.cvss_score, "description": v.description,
        "recommendation": v.recommendation, "status": v.status, "createdAt": v.created_at,
    }
