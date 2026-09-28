# ============================================================
# app/schemas/monitor.py — 日志监控 API Schema
# ============================================================

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class MonitorTaskCreate(BaseModel):
    name: str = Field("New Monitor", max_length=100)
    enabled: bool = False
    k8s_namespace: str = "default"
    k8s_kubeconfig: str | None = None
    environment_id: str = "test"
    s3_archive_enabled: bool = False
    s3_bucket: str | None = None
    s3_region: str = "us-east-1"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_endpoint: str | None = None
    retention_days: int = Field(3, ge=1, le=3650)
    alert_enabled: bool = True
    slack_webhook_url: str | None = None
    poll_interval_seconds: int = Field(60, ge=10, le=86400)
    alert_keywords: list[str] = Field(default_factory=list)
    immediate_keywords: list[str] = Field(default_factory=list)
    ignore_keywords: list[str] = Field(default_factory=list)
    record_only_keywords: list[str] = Field(default_factory=list)
    alert_threshold_count: int = 1
    alert_threshold_window: int = 60
    alert_silence_minutes: int = 60


class MonitorTaskUpdate(BaseModel):
    name: str | None = Field(None, max_length=100)
    enabled: bool | None = None
    k8s_namespace: str | None = None
    k8s_kubeconfig: str | None = None
    environment_id: str | None = None
    s3_archive_enabled: bool | None = None
    s3_bucket: str | None = None
    s3_region: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_endpoint: str | None = None
    retention_days: int | None = Field(None, ge=1, le=3650)
    alert_enabled: bool | None = None
    slack_webhook_url: str | None = None
    poll_interval_seconds: int | None = Field(None, ge=10, le=86400)
    alert_keywords: list[str] | None = None
    immediate_keywords: list[str] | None = None
    ignore_keywords: list[str] | None = None
    record_only_keywords: list[str] | None = None
    alert_threshold_count: int | None = None
    alert_threshold_window: int | None = None
    alert_silence_minutes: int | None = None


class MonitorTaskOut(BaseModel):
    id: UUID
    name: str
    enabled: bool
    k8s_namespace: str
    k8s_kubeconfig: str | None = None
    k8s_kubeconfig_set: bool = False
    environment_id: str = "test"
    s3_archive_enabled: bool
    s3_bucket: str | None = None
    s3_region: str
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_secret_key_set: bool = False
    s3_endpoint: str | None = None
    retention_days: int
    alert_enabled: bool
    slack_webhook_url: str | None = None
    poll_interval_seconds: int
    alert_keywords: list[Any] = Field(default_factory=list)
    immediate_keywords: list[Any] = Field(default_factory=list)
    ignore_keywords: list[Any] = Field(default_factory=list)
    record_only_keywords: list[Any] = Field(default_factory=list)
    alert_threshold_count: int
    alert_threshold_window: int
    alert_silence_minutes: int
    last_run: datetime | None = None
    last_error: str | None = None
    alerts_sent_count: int = 0
    alert_state: dict = Field(default_factory=dict)
    threshold_state: dict = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class BatchSearchRequest(BaseModel):
    task_id: UUID
    filenames: list[str] = Field(max_length=50)
    keyword: str = Field(max_length=200)

    @field_validator("filenames")
    @classmethod
    def _cap_filenames(cls, v: list[str]) -> list[str]:
        return (v or [])[:50]
