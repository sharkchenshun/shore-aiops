# ============================================================
# app/models/inspection.py — Prometheus 集群巡检（合并自 shark-Platform inspection）
# ============================================================

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, utcnow


class InspectionConfig(TimestampMixin, Base):
    __tablename__ = "inspection_configs"
    __table_args__ = (UniqueConstraint("environment_id", name="uq_inspection_config_env"),)

    environment_id: Mapped[str] = mapped_column(String(32), default="test")
    prometheus_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    ark_base_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    ark_api_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    ark_model_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class InspectionReport(TimestampMixin, Base):
    __tablename__ = "inspection_reports"
    __table_args__ = (
        UniqueConstraint("environment_id", "report_id", name="uq_inspection_report_env_id"),
    )

    environment_id: Mapped[str] = mapped_column(String(32), default="test")
    report_id: Mapped[str] = mapped_column(String(50))
    content: Mapped[dict] = mapped_column(JSONB, default=dict)


class InspectionIgnore(Base):
    __tablename__ = "inspection_ignores"
    __table_args__ = (
        UniqueConstraint("environment_id", "key", name="uq_inspection_ignore_env_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    environment_id: Mapped[str] = mapped_column(String(32), default="test")
    key: Mapped[str] = mapped_column(String(512))
    check_id: Mapped[str] = mapped_column(String(64), default="")
    label: Mapped[str] = mapped_column(String(512), default="")
    note: Mapped[str] = mapped_column(String(255), default="")
    created_by: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
