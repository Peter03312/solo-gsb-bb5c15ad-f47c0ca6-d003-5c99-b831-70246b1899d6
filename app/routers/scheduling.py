"""整组仪器排班可用性查询接口（本轮新增）。"""
from __future__ import annotations

from fastapi import APIRouter

from ..errors import blocked
from ..models import AvailabilityQuery
from ..repository import instrument_availability_timeline
from ..transactions import run_tx

router = APIRouter(prefix="/scheduling", tags=["scheduling"])


@router.post("/availability")
def availability(payload: AvailabilityQuery):
    """查询一组仪器在指定时间范围内何时**全部**具备报告放行条件。

    返回按时间排序的左闭右开时段；每段给出整组结论与各仪器原因码及所据
    禁用事件/校准版本。仅在整组结论与各仪器依据均相同时合并相邻时段。

    输入约定（均为 422 + detail.code 机器可读原因码）：

    - 仪器列表必须非空（EMPTY_INSTRUMENTS）、标识不能为空串；不做 trim
    - 列表无重复（DUPLICATE_INSTRUMENT，按原始标识判定）
    - 起止时间必须带时区（TIMEZONE_REQUIRED），起点须早于终点
      （INVALID_TIME_RANGE）

    语义：

    - 结果仅供规划，不预占未来放行资格：不写任何数据，正式报告仍按提交时
      状态判定；既有报告提交与错误约定完全不变
    - 整次查询在同一个 SERIALIZABLE 只读事务内完成，依据同一已提交状态，
      不会与并发的校准更正、禁用登记/撤销混用前后状态
    """
    instruments = payload.instruments
    if not instruments:
        raise blocked("EMPTY_INSTRUMENTS", "仪器标识列表不能为空")
    if any(not isinstance(i, str) or not i.strip() for i in instruments):
        raise blocked("EMPTY_INSTRUMENTS", "仪器标识不能为空串")

    seen: set[str] = set()
    duplicates: list[str] = []
    for inst in instruments:
        if inst in seen and inst not in duplicates:
            duplicates.append(inst)
        seen.add(inst)
    if duplicates:
        raise blocked(
            "DUPLICATE_INSTRUMENT",
            f"仪器标识列表存在重复: {duplicates}",
            duplicates=duplicates,
        )

    if payload.range_from >= payload.range_to:
        raise blocked(
            "INVALID_TIME_RANGE",
            "查询范围必须满足 from < to（左闭右开，端点为带时区时间戳）",
            range_from=payload.range_from.isoformat(),
            range_to=payload.range_to.isoformat(),
        )

    return run_tx(
        lambda conn: instrument_availability_timeline(
            conn, instruments, payload.range_from, payload.range_to
        ),
        readonly=True,
    )
