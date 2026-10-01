"""Pydantic 请求/响应模型。时间统一按带时区时间戳处理。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator


def as_utc(value: datetime) -> datetime:
    """无时区时间戳按 UTC 处理；有时区的统一转换到 UTC。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class CalibrationCreate(BaseModel):
    instrument: str = Field(..., min_length=1, description="仪器标识")
    valid_from: datetime
    valid_to: datetime | None = Field(None, description="为空表示开放区间（持续有效）")
    passed: bool

    _normalize_from = field_validator("valid_from")(lambda cls, v: as_utc(v))
    _normalize_to = field_validator("valid_to")(lambda cls, v: as_utc(v) if v else v)


class CalibrationCorrect(BaseModel):
    corrects_id: int = Field(..., description="被更正的任意历史版本 ID（通常传原记录/最新版 ID）")
    version: int = Field(..., ge=1, description="必须为该校准链当前最新版本号 + 1（连续版本号）")
    valid_from: datetime
    valid_to: datetime | None = None
    passed: bool

    _normalize_from = field_validator("valid_from")(lambda cls, v: as_utc(v))
    _normalize_to = field_validator("valid_to")(lambda cls, v: as_utc(v) if v else v)


class ReportSubmit(BaseModel):
    instrument: str = Field(..., min_length=1)
    tested_at: datetime = Field(..., description="检测时间，据此判断校准是否覆盖")
    reference: str | None = Field(None, description="可选业务报告编号；提供则需全局唯一")

    _normalize_tested = field_validator("tested_at")(lambda cls, v: as_utc(v))


class DisableCreate(BaseModel):
    event_no: str = Field(..., min_length=1, description="唯一禁用事件号（业务侧生成，撤销后也不可复用）")
    instrument: str = Field(..., min_length=1, description="仪器标识")
    disabled_from: datetime = Field(..., description="禁用起点（含）")
    disabled_to: datetime | None = Field(None, description="禁用终点（不含）；为空表示持续禁用")
    reason: str = Field(..., min_length=1, description="禁用原因（如故障说明）")

    _normalize_from = field_validator("disabled_from")(lambda cls, v: as_utc(v))
    _normalize_to = field_validator("disabled_to")(lambda cls, v: as_utc(v) if v else v)


class DisableRevoke(BaseModel):
    reason: str | None = Field(None, description="撤销原因（可选）")


class AvailabilityQuery(BaseModel):
    """整组仪器排班可用性查询：非空且无重复仪器标识 + 带时区的起止时间。

    列表非空/去重、起点早于终点由接口层校验为机器可读原因码；
    缺少时区在这里直接抛 422 TIMEZONE_REQUIRED，不静默按 UTC 补。
    """

    instruments: list[str] = Field(
        ...,
        description="仪器标识列表；必须非空且无重复（由接口层校验，返回机器可读原因码）",
    )
    range_from: datetime = Field(
        ..., alias="from",
        description="查询范围起点（含），必须带时区偏移，且早于终点",
    )
    range_to: datetime = Field(
        ..., alias="to",
        description="查询范围终点（不含），必须带时区偏移",
    )

    model_config = {"populate_by_name": True}

    @field_validator("range_from", "range_to")
    @classmethod
    def _require_timezone(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
            from fastapi import HTTPException

            # 与既有错误约定保持一致：422 + detail.code 机器可读
            raise HTTPException(status_code=422, detail={
                "code": "TIMEZONE_REQUIRED",
                "message": "查询时间必须显式带时区偏移（如 2026-03-01T00:00:00+08:00 或 Z）",
            })
        return v.astimezone(timezone.utc)


def serialize_disable(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "event_no": row["event_no"],
        "instrument": row["instrument"],
        "disabled_from": row["disabled_from"].isoformat(),
        "disabled_to": row["disabled_to"].isoformat() if row["disabled_to"] else None,
        "reason": row["reason"],
        "revoked": row["revoked"],
        "revoked_at": row["revoked_at"].isoformat() if row["revoked_at"] else None,
        "revoke_reason": row["revoke_reason"],
        "created_at": row["created_at"].isoformat(),
    }


def serialize_calibration(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "chain_id": row["chain_id"],
        "version": row["version"],
        "instrument": row["instrument"],
        "valid_from": row["valid_from"].isoformat(),
        "valid_to": row["valid_to"].isoformat() if row["valid_to"] else None,
        "passed": row["passed"],
        "is_current": row["is_current"],
        "corrects_id": row["corrects_id"],
        "created_at": row["created_at"].isoformat(),
    }
