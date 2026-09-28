# ============================================================
# app/schemas/inspection.py — 巡检 API Schema
# ============================================================

from pydantic import BaseModel, Field


class InspectionConfigUpdate(BaseModel):
    prometheus_url: str | None = Field(None, max_length=1024)
    ark_base_url: str | None = Field(None, max_length=1024)
    ark_api_key: str | None = Field(None, max_length=255)
    ark_model_id: str | None = Field(None, max_length=255)


class InspectionIgnoreCreate(BaseModel):
    key: str = Field(min_length=1, max_length=512)
    check_id: str = Field("", max_length=64)
    label: str = Field("", max_length=512)
    note: str = Field("", max_length=255)
