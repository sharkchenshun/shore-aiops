# ============================================================
# app/models — 统一导出（Alembic autogenerate 需要全部 import）
# ============================================================

from app.models.auth import User
from app.models.infra import (
    Cluster, Node, Service, Middleware, ServiceDependency,
)
from app.models.incident import (
    Incident, IncidentEvent, AnalysisReport, AlertRule,
)
from app.models.deployment import Deployment
from app.models.security import ScanSession, Asset, Vulnerability
from app.models.knowledge import KnowledgeChunk
from app.models.schedule import Schedule, PhoneAlert
from app.models.monitor import MonitorTask
from app.models.inspection import InspectionConfig, InspectionReport, InspectionIgnore
from app.models.agent_run import AgentRun

__all__ = [
    "User",
    "Cluster", "Node", "Service", "Middleware", "ServiceDependency",
    "Incident", "IncidentEvent", "AnalysisReport", "AlertRule",
    "Deployment",
    "ScanSession", "Asset", "Vulnerability",
    "KnowledgeChunk",
    "Schedule", "PhoneAlert",
    "MonitorTask",
    "InspectionConfig", "InspectionReport", "InspectionIgnore",
    "AgentRun",
]
