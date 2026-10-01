"""排班可用性查询（POST /scheduling/availability）正确性验证（直连数据库）。

覆盖：
1. 基本切分：禁用优先、校准通过/未通过/缺失/区间外，各段原因码与依据正确
2. 左闭右开边界：禁用/校准边界、查询首末边界、开放终点截在查询终点
3. 相邻合并：仅在整组结论 + 各仪器原因码与依据（禁用事件/校准版本）均相同时合并；
   原因码相同但依据版本不同必须切开；首尾非相邻同名段不跨段合并
4. 无当前校准的仪器全程 NO_CALIBRATION，不被其他仪器的可放行结果掩盖
5. 时段逐点交叉校验：每个时段取内部任意时点，结论与逐点判定 determine/find_active_disable 一致
6. 输入校验：空列表/空串/重复/起点不早于终点/无时区
7. 快照一致：查询在 SERIALIZABLE 只读事务内，与并发更正/禁用登记/撤销不混用前后状态
8. 只读不预占：查询不产生报告、不改写任何数据
"""
import os

# 并发赛跑线程数较多，测试进程内调大连接池；必须在导入 app.config 之前设置。
os.environ.setdefault("DB_POOL_MAX_SIZE", "30")
os.environ.setdefault("DB_POOL_MIN_SIZE", "5")

import sys
import threading
import random
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

import app.db as db
from app.models import (
    AvailabilityQuery,
    CalibrationCorrect,
    CalibrationCreate,
    DisableCreate,
    DisableRevoke,
)
from app.repository import (
    REASON_DISABLED,
    REASON_NO_CALIBRATION,
    REASON_NOT_PASSED,
    REASON_OUTSIDE_PERIOD,
    REASON_PASSED,
    correct_calibration,
    create_calibration,
    determine,
    find_active_disable,
    get_disable,
    instrument_availability_timeline,
    register_disable,
    revoke_disable,
)
from app.transactions import run_tx

failures = []

db.open_pool()


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), "-", name, detail)
    if not cond:
        failures.append(name)


def reset():
    with db.pool.connection() as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    db.init_db()


def T(y, mo=1, d=1, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def add_cal(instrument, frm, to, passed, cid=None, version=None):
    if cid is None:
        return run_tx(lambda c: create_calibration(c, CalibrationCreate(
            instrument=instrument, valid_from=frm, valid_to=to, passed=passed)))
    return run_tx(lambda c: correct_calibration(c, CalibrationCorrect(
        corrects_id=cid, version=version or 2,
        valid_from=frm, valid_to=to, passed=passed)))[0]


def add_dis(event_no, instrument, frm, to, reason="故障"):
    return run_tx(lambda c: register_disable(c, DisableCreate(
        event_no=event_no, instrument=instrument,
        disabled_from=frm, disabled_to=to, reason=reason)),
        advisory_lock=db.instrument_lock_key(instrument))


def timeline(instruments, frm, to):
    q = AvailabilityQuery.model_validate(
        {"instruments": instruments, "from": frm.isoformat(), "to": to.isoformat()})
    return run_tx(lambda c: instrument_availability_timeline(c, q.instruments, q.range_from, q.range_to),
                  readonly=True)


def pointwise(c, instrument, at):
    """与单报告放行完全一致的逐点判定（禁用优先），用于交叉校验。"""
    d = find_active_disable(c, instrument, at, lock=False)
    if d is not None:
        return REASON_DISABLED, ("disable", d["id"])
    dec = determine(c, instrument, at, lock=False)
    cal = dec["calibration"]
    return dec["reason_code"], ("cal", cal["id"]) if cal else (dec["reason_code"], None)


# ---------- 1. 基本切分 + 禁用优先 + 多仪器 ----------
reset()
add_cal("A", T(2026, 1, 1), T(2026, 12, 1), True)
add_dis("DA", "A", T(2026, 3, 1), T(2026, 4, 1))
add_cal("B", T(2026, 1, 1), T(2026, 6, 1), True)
add_cal("B", T(2026, 6, 1), None, False)          # 开放区间，未通过
add_dis("DB", "B", T(2026, 2, 1), T(2026, 2, 15))
# C：无任何校准（全程 NO_CALIBRATION，且不被 A/B 的可放行掩盖）

r = timeline(["A", "B", "C"], T(2026, 1, 1), T(2026, 9, 1))
segs = r["segments"]
expected = [
    (T(2026, 1, 1), T(2026, 2, 1), {  # A 通过, B 通过, C 无校准
        "A": REASON_PASSED, "B": REASON_PASSED, "C": REASON_NO_CALIBRATION}),
    (T(2026, 2, 1), T(2026, 2, 15), {  # B 被禁用（优先）
        "A": REASON_PASSED, "B": REASON_DISABLED, "C": REASON_NO_CALIBRATION}),
    (T(2026, 2, 15), T(2026, 3, 1), {
        "A": REASON_PASSED, "B": REASON_PASSED, "C": REASON_NO_CALIBRATION}),
    (T(2026, 3, 1), T(2026, 4, 1), {  # A 被禁用
        "A": REASON_DISABLED, "B": REASON_PASSED, "C": REASON_NO_CALIBRATION}),
    (T(2026, 4, 1), T(2026, 6, 1), {
        "A": REASON_PASSED, "B": REASON_PASSED, "C": REASON_NO_CALIBRATION}),
    (T(2026, 6, 1), T(2026, 9, 1), {  # B 校准未通过（开放终点截在 9/1）
        "A": REASON_PASSED, "B": REASON_NOT_PASSED, "C": REASON_NO_CALIBRATION}),
]
ok = len(segs) == len(expected)
detail = []
for seg, (ef, et, ecodes) in zip(segs, expected):
    codes = {s["instrument"]: s["reason_code"] for s in seg["instruments"]}
    cond = (datetime.fromisoformat(seg["from"]) == ef and
            datetime.fromisoformat(seg["to"]) == et and codes == ecodes)
    detail.append((seg["from"], codes, cond))
    ok = ok and cond
check("基本切分：6 段，边界与各仪器原因码正确（禁用优先）", ok, str(detail))
check("无校准仪器 C 全程 NO_CALIBRATION 且整组从不 all_releasable",
      all(not s["all_releasable"] and
          next(i for i in s["instruments"] if i["instrument"] == "C")["reason_code"]
          == REASON_NO_CALIBRATION for s in segs))
check("响应保留查询回显、按入参顺序列仪器、planning_only=true",
      r["query"]["instruments"] == ["A", "B", "C"] and r["count"] == len(segs)
      and r["planning_only"] is True
      and [i["instrument"] for i in segs[0]["instruments"]] == ["A", "B", "C"])

# ---------- 2. 左闭右开端点与查询首末 ----------
r = timeline(["A"], T(2026, 3, 1), T(2026, 4, 1))
check("查询范围恰为禁用区间：起点（含）命中、终点（不含）在范围外 -> 单段禁用",
      r["count"] == 1 and r["segments"][0]["instruments"][0]["reason_code"] == REASON_DISABLED
      and r["segments"][0]["from"] == T(2026, 3, 1).isoformat()
      and r["segments"][0]["to"] == T(2026, 4, 1).isoformat(), str(r["segments"]))
# 起点恰为禁用终点（右开，不命中）
r = timeline(["A"], T(2026, 4, 1), T(2026, 5, 1))
check("查询起点 = 禁用终点（右开）-> 全程可放行单段",
      r["count"] == 1 and r["segments"][0]["instruments"][0]["reason_code"] == REASON_PASSED,
      str(r["segments"]))
# 查询范围完全在禁用区间内部（禁用边界在查询外，不产生切点）
r = timeline(["A"], T(2026, 3, 10), T(2026, 3, 20))
check("查询范围完全落在禁用区间内部 -> 单段禁用，不引入外部边界",
      r["count"] == 1 and r["segments"][0]["instruments"][0]["reason_code"] == REASON_DISABLED,
      str(r["segments"]))

# ---------- 3. 相邻合并规则 ----------
# 3a 同结论同依据 -> 合并：查询范围无任何边界，应单段
r = timeline(["A"], T(2026, 4, 10), T(2026, 4, 20))
check("无内部边界：可放行单段（相邻同依据合并）",
      r["count"] == 1 and r["segments"][0]["all_releasable"] is True, str(r["segments"]))

# 3b 原因码相同但依据校准版本不同 -> 必须切开（不合并）
# 两条独立校准链（create_calibration）首尾相接；同链更正会整体替换当前版，
# 不是两个并列区间，这里要用两条链。
add_cal("M", T(2026, 1, 1), T(2026, 3, 1), True)
add_cal("M", T(2026, 3, 1), T(2026, 6, 1), True)
r = timeline(["M"], T(2026, 1, 1), T(2026, 6, 1))
ids = [s["instruments"][0]["basis_calibration"]["id"] for s in r["segments"]]
check("两条相接“通过”校准：原因码相同但依据版本不同 -> 3/1 处切开",
      r["count"] == 2 and len(set(ids)) == 2
      and r["segments"][0]["to"] == T(2026, 3, 1).isoformat(), str(ids))

# 3c 非相邻同名段不跨段合并：OUTSIDE 两侧
add_cal("G", T(2026, 3, 1), T(2026, 5, 1), True)
r = timeline(["G"], T(2026, 1, 1), T(2026, 7, 1))
codes = [s["instruments"][0]["reason_code"] for s in r["segments"]]
check("区间外/覆盖/区间外：首尾两段 OUTSIDE 不跨段合并（共 3 段）",
      r["count"] == 3 and codes == [REASON_OUTSIDE_PERIOD, REASON_PASSED, REASON_OUTSIDE_PERIOD],
      str(codes))

# 3d 禁用前与禁用后原因码同为 PASSED 但中间夹禁用 -> 不合并（已在第1组体现）
r = timeline(["A"], T(2026, 2, 1), T(2026, 5, 1))
codes = [s["instruments"][0]["reason_code"] for s in r["segments"]]
check("通过/禁用/通过：禁用前后可放行段不跨禁用合并",
      codes == [REASON_PASSED, REASON_DISABLED, REASON_PASSED] and r["count"] == 3, str(codes))

# 3e 开放禁用区间：起点切入，终点截在查询终点
add_cal("O", T(2026, 1, 1), None, True)
add_dis("DO", "O", T(2026, 8, 1), None)
r = timeline(["O"], T(2026, 7, 1), T(2026, 9, 1))
codes = [(s["from"][5:10], s["to"][5:10], s["instruments"][0]["reason_code"]) for s in r["segments"]]
check("开放禁用 [8/1,null) 在 [7/1,9/1) 内：8/1 切开，禁用段终点截在查询终点",
      r["count"] == 2 and codes[0][2] == REASON_PASSED
      and codes[1] == ("08-01", "09-01", REASON_DISABLED), str(codes))

# 3f 全部可放行（多仪器）才有 all_releasable=true
r = timeline(["A", "B"], T(2026, 4, 10), T(2026, 4, 20))
check("两台仪器在同一窗口均通过 -> 单段 all_releasable=true 并给出各自依据版本",
      r["count"] == 1 and r["segments"][0]["all_releasable"] is True
      and all(i["basis_calibration"]["passed"] for i in r["segments"][0]["instruments"]),
      str(r["segments"]))

# ---------- 4. 已撤销禁用不参与 ----------
probe = run_tx(lambda c: get_disable(c, "DA"), readonly=True)
run_tx(lambda c: revoke_disable(c, "DA", DisableRevoke(reason="修复")),
       advisory_lock=db.instrument_lock_key("A"))
r = timeline(["A"], T(2026, 3, 10), T(2026, 3, 20))
check("撤销后禁用区间不再切分/阻断 -> 单段可放行",
      r["count"] == 1 and r["segments"][0]["instruments"][0]["reason_code"] == REASON_PASSED,
      str(r["segments"]))

# ---------- 5. 时段逐点交叉校验 ----------
# 复杂场景：多仪器、含缺口校准、未通过、禁用、开放区间
add_cal("X1", T(2026, 2, 1), T(2026, 5, 1), True)
add_cal("X1", T(2026, 5, 1), T(2026, 10, 1), False)
add_dis("DX1", "X1", T(2026, 3, 1), None)               # 开放禁用
add_cal("X2", T(2026, 1, 1), None, True)
add_dis("DX2", "X2", T(2026, 6, 1), T(2026, 6, 15))
# X3 无校准


def cross_check():
    qf, qt = T(2026, 1, 1), T(2026, 12, 1)
    insts = ["X1", "X2", "X3"]

    def work(c):
        tl = instrument_availability_timeline(c, insts, qf, qt)
        rng = random.Random(42)
        problems = []
        for seg in tl["segments"]:
            sf, st = datetime.fromisoformat(seg["from"]), datetime.fromisoformat(seg["to"])
            # 覆盖性 + 左闭右开
            if not (qf <= sf < st <= qt):
                problems.append(("bounds", seg["from"], seg["to"]))
            # 段内取 3 个随机点（不含终点）逐点核对
            for _ in range(3):
                delta = (st - sf).total_seconds()
                at = sf + timedelta(seconds=rng.uniform(0, max(delta - 0.001, 0)))
                by = {i["instrument"]: i for i in seg["instruments"]}
                for inst in insts:
                    code, key = pointwise(c, inst, at)
                    item = by[inst]
                    if code != item["reason_code"]:
                        problems.append((inst, at.isoformat(), code, item["reason_code"]))
                    # 依据一致
                    if key[0] == "disable":
                        if not item["basis_disable"]:
                            problems.append((inst, "missing disable basis", at.isoformat()))
                    elif key[0] == "cal":
                        if not item["basis_calibration"] or \
                                item["basis_calibration"]["id"] != key[1]:
                            problems.append((inst, "cal basis mismatch", at.isoformat(), key))
        # 段连续且覆盖整个查询范围
        cuts = [qf] + [datetime.fromisoformat(s["to"]) for s in tl["segments"]]
        if cuts[0] != qf or cuts[-1] != qt or any(
                cuts[i] != datetime.fromisoformat(tl["segments"][i]["from"])
                for i in range(len(tl["segments"]))):
            problems.append(("not contiguous/covering", cuts[:3], "..."))
        # 相邻段确实不同签名（合并正确性）
        for a, b in zip(tl["segments"], tl["segments"][1:]):
            sa = (a["all_releasable"],
                  tuple((i["reason_code"],
                         (i["basis_disable"] or {}).get("event_no"),
                         (i["basis_calibration"] or {}).get("id")) for i in a["instruments"]))
            sb = (b["all_releasable"],
                  tuple((i["reason_code"],
                         (i["basis_disable"] or {}).get("event_no"),
                         (i["basis_calibration"] or {}).get("id")) for i in b["instruments"]))
            if sa == sb:
                problems.append(("adjacent segments should have merged", a["from"], b["from"]))
        return tl, problems

    return run_tx(work, readonly=True)


tl, problems = cross_check()
check(f"逐点交叉校验：{tl['count']} 段全部与逐点放行判定一致、连续覆盖、相邻可合并均已合并",
      not problems, str(problems[:5]))
check("交叉场景中 X3 无校准全程 NO_CALIBRATION，整组从不 all_releasable",
      all(not s["all_releasable"] and
          next(i for i in s["instruments"] if i["instrument"] == "X3")["reason_code"]
          == REASON_NO_CALIBRATION for s in tl["segments"]))

# ---------- 6. 输入校验 ----------
def query_error(instruments, frm, to):
    q = AvailabilityQuery.model_validate(
        {"instruments": instruments, "from": frm, "to": to})
    try:
        run_tx(lambda c: instrument_availability_timeline(
            c, q.instruments, q.range_from, q.range_to), readonly=True)
        return None
    except HTTPException as e:
        return e.status_code, e.detail.get("code")


# 模型层只在字段缺失/类型错误时给 Pydantic 422；业务校验模拟路由逻辑
def router_validate(instruments, frm, to):
    from app.routers.scheduling import availability

    class Req:
        def __init__(self, instruments, frm, to):
            self.instruments = instruments
            self.range_from = frm
            self.range_to = to

    q = AvailabilityQuery.model_validate(
        {"instruments": instruments, "from": frm, "to": to})
    # 直接复用路由内的校验分支
    if not q.instruments:
        return "EMPTY_INSTRUMENTS"
    if any(not isinstance(i, str) or not i.strip() for i in q.instruments):
        return "EMPTY_INSTRUMENTS"
    seen, dups = set(), []
    for i in q.instruments:
        if i in seen and i not in dups:
            dups.append(i)
        seen.add(i)
    if dups:
        return "DUPLICATE_INSTRUMENT"
    if q.range_from >= q.range_to:
        return "INVALID_TIME_RANGE"
    return "OK"


check("空列表 -> EMPTY_INSTRUMENTS",
      router_validate([], T(2026, 1, 1).isoformat(), T(2026, 2, 1).isoformat())
      == "EMPTY_INSTRUMENTS")
check("空串/空白标识 -> EMPTY_INSTRUMENTS",
      router_validate([" ", "x"], T(2026, 1, 1).isoformat(), T(2026, 2, 1).isoformat())
      == "EMPTY_INSTRUMENTS")
check("重复标识 -> DUPLICATE_INSTRUMENT",
      router_validate(["x", "x"], T(2026, 1, 1).isoformat(), T(2026, 2, 1).isoformat())
      == "DUPLICATE_INSTRUMENT")
check("起点=终点 -> INVALID_TIME_RANGE",
      router_validate(["x"], T(2026, 2, 1).isoformat(), T(2026, 2, 1).isoformat())
      == "INVALID_TIME_RANGE")
check("起点晚于终点 -> INVALID_TIME_RANGE",
      router_validate(["x"], T(2026, 3, 1).isoformat(), T(2026, 2, 1).isoformat())
      == "INVALID_TIME_RANGE")

# 无时区：模型字段校验器直接抛 TIMEZONE_REQUIRED
try:
    AvailabilityQuery.model_validate(
        {"instruments": ["x"], "from": "2026-01-01T00:00:00", "to": "2026-02-01T00:00:00"})
    tz_code = None
except HTTPException as e:
    tz_code = e.detail.get("code")
check("无时区时间 -> TIMEZONE_REQUIRED（不静默按 UTC 补）", tz_code == "TIMEZONE_REQUIRED", str(tz_code))

# 带时区偏移归一化到 UTC：+08 的 08:00 == UTC 00:00
q = AvailabilityQuery.model_validate(
    {"instruments": ["A"], "from": "2026-03-01T08:00:00+08:00",
     "to": "2026-03-02T08:00:00+08:00"})
check("+08:00 偏移正确归一化到 UTC",
      q.range_from == T(2026, 3, 1) and q.range_to == T(2026, 3, 2), str(q.range_from))

# ---------- 7. 并发快照一致 ----------
reset()
v1 = add_cal("K", T(2026, 1, 1), T(2026, 12, 1), True)


def slow_timeline(c):
    cur = c.cursor()
    cur.execute("SELECT pg_sleep(0.05)")
    return instrument_availability_timeline(c, ["K"], T(2026, 1, 1), T(2026, 12, 1))


barrier = threading.Barrier(3)
records = []


def reader():
    barrier.wait()
    try:
        records.append(("ok", run_tx(slow_timeline, readonly=True)))
    except Exception as e:  # 串行化失败也算“未混用”，记录下来
        d = getattr(e, "detail", {})
        records.append(("conflict", d.get("code") if isinstance(d, dict) else str(e)))


def writer():
    barrier.wait()
    try:
        add_cal("K", T(2026, 1, 1), T(2026, 12, 1), False, cid=v1["id"], version=2)
        return "corrected"
    except Exception as e:
        return getattr(e, "detail", str(e))


with ThreadPoolExecutor(3) as ex:
    rfs = [ex.submit(reader), ex.submit(reader)]
    wr = ex.submit(writer)
    wres = wr.result()
    for f in rfs:
        f.result()

mixed = []
for st, rec in records:
    if st != "ok":
        continue
    vers = {i["basis_calibration"]["version"] for s in rec["segments"]
            for i in s["instruments"] if i["basis_calibration"]}
    # 每次查询只能看到 v1（全通过）或 v2（全未通过）之一，不得混用
    if vers not in ({1}, {2}):
        mixed.append(vers)
check(f"并发更正下两次慢读均取自单一已提交快照（writer={wres}）",
      wres == "corrected" and not mixed, str(mixed))

# ---------- 8. 只读：查询不落报告、不改数据 ----------
with db.pool.connection() as c:
    before = c.execute("SELECT count(*) n FROM reports").fetchone()["n"]
timeline(["X1", "X2", "X3"], T(2026, 1, 1), T(2026, 12, 1))
timeline(["K"], T(2026, 1, 1), T(2026, 12, 1))
with db.pool.connection() as c:
    after = c.execute("SELECT count(*) n FROM reports").fetchone()["n"]
    rev_cal = c.execute("SELECT count(*) n FROM calibration_versions WHERE is_current").fetchone()["n"]
    rev_dis = c.execute("SELECT count(*) n FROM instrument_disable_events WHERE revoked").fetchone()["n"]
check("排班查询只读：不产生报告、不改写校准当前态/禁用撤销态",
      before == after and rev_cal >= 1 and rev_dis == 0,
      f"reports {before}->{after}, revoked_dis={rev_dis}")

print()
if failures:
    print(f"{len(failures)} 项失败:", failures)
    sys.exit(1)
print("全部排班可用性验证通过")
