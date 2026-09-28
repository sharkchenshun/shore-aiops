# ============================================================
# app/schemas/inspection.py — 巡检 API Schema
# ============================================================

from pydantic import BaseModel


class InspectionConfigUpdate(BaseModel):
    prometheus_url: str | None = None
    ark_base_url: str | None = None
    ark_api_key: str | None = None
    ark_model_id: str | None = None


class InspectionIgnoreCreate(BaseModel):
    key: str
    check_id: str = ""
    label: str = ""
    note: str = ""
