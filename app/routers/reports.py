"""检测报告接口。"""
from __future__ import annotations

from fastapi import APIRouter

from .. import db
from ..errors import not_found
from ..models import ReportSubmit
from ..repository import compute_drift
from ..transactions import run_tx
from ..repository import publish_report

router = APIRouter(prefix="/reports", tags=["reports"])


def _serialize_report(row: dict) -> dict:
    return {
        "id": row["id"],
        "reference": row["reference"],
        "instrument": row["instrument"],
        "tested_at": row["tested_at"].isoformat(),
        "released": row["released"],
        "basis_calibration_id": row["basis_calibration_id"],
        "snapshot": row["snapshot"],
        "created_at": row["created_at"].isoformat(),
    }


@router.post("", status_code=201)
def submit_report(payload: ReportSubmit):
    """提交检测报告并尝试放行。

    判定顺序：先检查仪器禁用区间（命中未撤销区间即 422 INSTRUMENT_DISABLED
    并返回禁用依据，即使校准通过）；再检查校准——检测时间被该仪器一条“结论
    为通过”的当前校准版本覆盖才放行（左闭右开，仅最新版参与判定）。

    - 放行：201，返回所据校准版本及不可变判定快照
    - 命中禁用 / 缺失校准 / 区间外 / 校准未通过：422，detail 内含阻断原因码与说明
    """
    return run_tx(
        lambda conn: publish_report(conn, payload),
        advisory_lock=db.instrument_lock_key(payload.instrument),
    )


# 注意：静态路由需在 /{report_id} 之前声明
@router.get("/drift/recompute")
def report_drift(instrument: str | None = None):
    items = run_tx(lambda conn: compute_drift(conn, instrument), readonly=True)
    return {"items": items, "count": len(items)}


@router.get("")
def list_reports(instrument: str | None = None):
    assert db.pool is not None
    with db.pool.connection() as conn:
        if instrument:
            rows = conn.execute(
                "SELECT * FROM reports WHERE instrument = %s ORDER BY id",
                (instrument,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM reports ORDER BY id").fetchall()
    return {"items": [_serialize_report(r) for r in rows]}


@router.get("/{report_id}")
def get_report(report_id: int):
    assert db.pool is not None
    with db.pool.connection() as conn:
        row = conn.execute(
            "SELECT * FROM reports WHERE id = %s", (report_id,)
        ).fetchone()
    if row is None:
        raise not_found("报告", report_id)
    return {"report": _serialize_report(row)}
