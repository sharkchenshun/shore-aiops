# ============================================================
# app/models/monitor.py — K8s 日志监控任务（合并自 shark-Platform monitor）
# ============================================================

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin


class MonitorTask(TimestampMixin, Base):
    __tablename__ = "monitor_tasks"

    name: Mapped[str] = mapped_column(String(100), default="New Monitor")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # K8s
    k8s_namespace: Mapped[str] = mapped_column(String(255), default="default")
    k8s_kubeconfig: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    environment_id: Mapped[str] = mapped_column(String(32), default="test")  # dev / test，对应 environments.json

    # S3
    s3_archive_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    s3_bucket: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    s3_region: Mapped[str] = mapped_column(String(50), default="us-east-1")
    s3_access_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    s3_secret_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    s3_endpoint: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    retention_days: Mapped[int] = mapped_column(Integer, default=3)

    # Alerting
    alert_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    slack_webhook_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    poll_interval_seconds: Mapped[int] = mapped_column(Integer, default=60)

    alert_keywords: Mapped[list] = mapped_column(JSONB, default=list)
    immediate_keywords: Mapped[list] = mapped_column(JSONB, default=list)
    ignore_keywords: Mapped[list] = mapped_column(JSONB, default=list)
    record_only_keywords: Mapped[list] = mapped_column(JSONB, default=list)

    alert_threshold_count: Mapped[int] = mapped_column(Integer, default=1)
    alert_threshold_window: Mapped[int] = mapped_column(Integer, default=60)
    alert_silence_minutes: Mapped[int] = mapped_column(Integer, default=60)

    # Runtime state
    last_run: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    alerts_sent_count: Mapped[int] = mapped_column(Integer, default=0)
    alert_state: Mapped[dict] = mapped_column(JSONB, default=dict)
    threshold_state: Mapped[dict] = mapped_column(JSONB, default=dict)
