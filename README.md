# 实验室检测报告放行服务

基于 **Python + FastAPI + PostgreSQL** 的 HTTP API：管理仪器校准记录（含有效期、通过/未通过结论、更正版本链）、
仪器禁用区间（故障停用与撤销追溯），并据此对检测报告做放行判定。

## 核心规则

- 校准区间为 **左闭右开** `[valid_from, valid_to)`；`valid_to` 为 `null` 表示开放区间（持续有效）。
- 同一仪器**当前版本**的有效区间**不得重叠**，由 PostgreSQL GiST `EXCLUDE` 约束在数据库层强制。
- 更正必须**引用原记录**（任意历史版本 ID）并提交**连续版本号**（当前最新版 + 1）。
- 旧版本全部保留可查，`is_current=true` 的唯一最新版才参与判定。
- 版本冲突、区间冲突一律拒绝（`409`），错误体含机器可读原因码。
- 仪器校准合格后仍可能因故障被禁用：按**唯一事件号**登记禁用区间
  `[disabled_from, disabled_to)`（终点可空表示持续禁用），同仪器**未撤销**区间不得重叠
  （边界相接允许），同样由 GiST `EXCLUDE` 约束强制。
- 可按事件号**撤销**禁用；撤销仅置标记并记录时间/原因，原记录保留供追溯；事件号撤销后也不可复用。
- 报告放行条件：提交的检测时间**先**检查禁用区间——命中任一**未撤销**禁用区间即拒绝放行（`422`）
  并返回禁用依据（事件号、区间、原因），**即使当前校准通过**；未命中禁用时再沿用校准判定
  （被一条结论为通过的当前校准版本覆盖才放行）。
- 成功放行时返回所据校准版本，并保存发布时判定快照。
- 校准缺失 / 时间在区间外 / 校准未通过：拒绝放行（`422`），返回阻断原因码与说明。
- 已发布报告的快照**永不改写**；更正校准后可按当前有效性重算，列出结论与快照不同的报告。
- 可按**当前禁用状态**查询受影响的已发布报告及对应禁用依据；追溯登记或撤销禁用后结果随之变化，
  但原放行记录与快照不改写。
- **排班可用性查询（本轮新增）**：给定一组仪器与带时区的时间范围，按当前未撤销禁用区间
  （优先）与当前校准版本，把范围切分成左闭右开时段，逐段返回整组是否可放行及各仪器原因码、
  所据禁用事件/校准版本；仅在整组结论与各仪器依据均相同时合并相邻时段。结果**仅供规划**、
  不预占未来放行资格，整次查询依据同一已提交状态（只读 SERIALIZABLE）。
- 禁用变更、校准更正与报告发布事务均运行在 **SERIALIZABLE** 隔离级别并对冲突自动重试，
  配合唯一部分索引、GiST 排他约束与行锁，杜绝“双最新版”、“禁用区间重叠”和
  “在已生效禁用区间内误放行”。

## 一键启动

需要 Docker 与 Docker Compose（v2）。

```bash
docker compose up --build
```

启动后：

- API 服务地址与端口：**http://localhost:8000**
- 交互式 API 文档（Swagger UI）：**http://localhost:8000/docs**
- OpenAPI JSON：http://localhost:8000/openapi.json
- 健康检查：http://localhost:8000/health
- PostgreSQL：`localhost:5432`（库/用户/口令均为 `lab`）

数据库（扩展 `btree_gist`、全部表、唯一/排除约束与索引）在 API 首次启动时**幂等自动初始化**，
无需手工执行 SQL；`db` 容器带健康检查，就绪后 API 才启动。

停止并清理：

```bash
docker compose down          # 保留数据卷
docker compose down -v       # 同时删除数据库数据卷
```

## 配置

通过环境变量配置（见 `.env.example`）：

| 变量 | 默认值（容器内） | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://lab:lab@db:5432/lab` | PostgreSQL 连接串 |
| `DB_POOL_MIN_SIZE` | `1` | 连接池最小连接数 |
| `DB_POOL_MAX_SIZE` | `10` | 连接池最大连接数 |
| `DB_CONNECT_TIMEOUT` | `10` | 建连超时（秒） |
| `DB_ACQUIRE_TIMEOUT` | `10` | 从连接池获取连接的超时（秒），超时返回 `503 SERVICE_BUSY` |
| `TX_MAX_RETRIES` | `5` | 串行化/死锁冲突时事务最大重试次数 |
| `TX_RETRY_BACKOFF` | `0.05` | 重试退避基数（秒，指数退避） |

本地不用容器运行时：准备好 PostgreSQL，复制 `.env.example` 为 `.env` 后执行

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## API 一览

### 校准

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/calibrations` | 登记原始校准（版本链 v1） |
| POST | `/calibrations/corrections` | 提交更正版本（引用原记录 + 连续版本号） |
| GET | `/calibrations?instrument=&current_only=` | 列表（旧版保留可查） |
| GET | `/calibrations/{id}` | 单条校准（任意版本） |
| GET | `/calibrations/drift/recompute?instrument=` | 按当前有效性重算，返回与快照结论不同的报告 |

### 报告

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/reports` | 提交报告并判定放行（禁用判定优先于校准判定） |
| GET | `/reports?instrument=` | 已发布报告列表（含不可变快照） |
| GET | `/reports/{id}` | 单条报告 |
| GET | `/reports/drift/recompute` | 同上，重算漂移 |

### 仪器禁用（本轮新增）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/disables` | 按唯一事件号登记禁用区间（左闭右开，终点可空） |
| POST | `/disables/{event_no}/revoke` | 按事件号撤销禁用（记录保留） |
| GET | `/disables?instrument=&include_revoked=` | 禁用事件列表（默认含已撤销，供追溯） |
| GET | `/disables/{event_no}` | 按事件号查看单条禁用事件（含已撤销） |
| GET | `/disables/affected-reports?instrument=` | 当前受禁用影响的已发布报告及禁用依据 |

### 排班可用性（本轮新增）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/scheduling/availability` | 切分时间范围，逐段返回整组是否可放行及各仪器原因码/依据 |

## 调用示例

### 1. 登记校准

```bash
curl -sS -X POST http://localhost:8000/calibrations \
  -H 'Content-Type: application/json' \
  -d '{
        "instrument": "HPLC-01",
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to":   "2026-07-01T00:00:00Z",
        "passed": true
      }'
```

返回：

```json
{
  "calibration": {
    "id": 1, "chain_id": 1, "version": 1, "instrument": "HPLC-01",
    "valid_from": "2026-01-01T00:00:00+00:00",
    "valid_to":   "2026-07-01T00:00:00+00:00",
    "passed": true, "is_current": true, "corrects_id": null,
    "created_at": "2026-09-30T00:00:00+00:00"
  }
}
```

区间重叠的登记会被拒绝：`409 {"detail": {"code": "INTERVAL_OVERLAP", ...}}`。

### 2. 提交报告（放行成功）

```bash
curl -sS -X POST http://localhost:8000/reports \
  -H 'Content-Type: application/json' \
  -d '{
        "instrument": "HPLC-01",
        "tested_at": "2026-03-15T08:00:00Z",
        "reference": "R-2026-0001"
      }'
```

返回（`201`，含所据校准版本与判定快照）：

```json
{
  "released": true,
  "reason_code": "PASSED_CALIBRATION_COVERAGE",
  "basis_calibration": {"id": 1, "chain_id": 1, "version": 1, "passed": true, "...": "..."},
  "report_id": 1,
  "reference": "R-2026-0001",
  "snapshot": {
    "released": true,
    "reason_code": "PASSED_CALIBRATION_COVERAGE",
    "instrument": "HPLC-01",
    "tested_at": "2026-03-15T08:00:00+00:00",
    "basis_calibration": {"id": 1, "chain_id": 1, "version": 1, "passed": true}
  }
}
```

阻断场景（`422`，不落库）的 `detail.code`：

- `INSTRUMENT_DISABLED` —— 检测时间命中该仪器**未撤销**禁用区间（优先于一切校准判定），
  `detail.disable` 给出禁用依据（`event_no`/区间/`reason`）；
- `NO_CALIBRATION` —— 未命中禁用，且该仪器当前没有任何校准；
- `OUTSIDE_VALID_PERIOD` —— 未命中禁用，有校准但检测时间不在任何左闭右开区间内（如恰等于 `valid_to`）；
- `CALIBRATION_NOT_PASSED` —— 未命中禁用，覆盖该时点的校准结论为“未通过”。

### 3. 更正校准（连续版本号 + 引用原记录）

```bash
curl -sS -X POST http://localhost:8000/calibrations/corrections \
  -H 'Content-Type: application/json' \
  -d '{
        "corrects_id": 1,
        "version": 2,
        "valid_from": "2026-02-01T00:00:00Z",
        "valid_to":   "2026-08-01T00:00:00Z",
        "passed": false
      }'
```

- 版本号必须是当前最新版 + 1，否则 `409 VERSION_CONFLICT`（响应中给出 `current_version`/`expected_version`）；
- 新区间与同仪器其他当前版本重叠时 `409 INTERVAL_OVERLAP`；
- 响应同时含 `affected_report_drift`：因本次更正导致“当前结论与发布快照不同”的报告列表。

更正后再发布同一检测时间：`422 CALIBRATION_NOT_PASSED`；而此前已发布报告的快照保持原值。

### 4. 重算漂移（不改写快照）

```bash
curl -sS 'http://localhost:8000/calibrations/drift/recompute?instrument=HPLC-01'
```

仅返回当前结论（放行/阻断或所据版本）与快照不同的报告，字段含 `change`：

- `NOW_BLOCKED` —— 快照放行，按当前校准已阻断；
- `NOW_COVERED_BY_OTHER_VERSION` —— 仍放行，但所据校准版本已变化；
- `NOW_RELEASEABLE` —— 快照阻断、当前可放行（历史阻断报告原本不落库，属防御性取值）。

### 5. 登记禁用区间

```bash
curl -sS -X POST http://localhost:8000/disables \
  -H 'Content-Type: application/json' \
  -d '{
        "event_no": "DIS-2026-001",
        "instrument": "HPLC-01",
        "disabled_from": "2026-03-01T00:00:00Z",
        "disabled_to":   "2026-05-01T00:00:00Z",
        "reason": "进样阀故障待修"
      }'
```

返回（`201`，`disabled_to` 为 `null` 表示持续禁用）：

```json
{
  "disable_event": {
    "id": 1,
    "event_no": "DIS-2026-001",
    "instrument": "HPLC-01",
    "disabled_from": "2026-03-01T00:00:00+00:00",
    "disabled_to": "2026-05-01T00:00:00+00:00",
    "reason": "进样阀故障待修",
    "revoked": false,
    "revoked_at": null,
    "revoke_reason": null,
    "created_at": "2026-09-30T00:00:00+00:00"
  }
}
```

拒绝约定：

- 区间无效（`disabled_to <= disabled_from`）：`422 {"detail": {"code": "INVALID_INTERVAL"}}`；
- 事件号重复（**含已撤销事件**，事件号不可复用）：
  `409 {"detail": {"code": "DISABLE_EVENT_EXISTS"}}`；
- 与同仪器另一条**未撤销**区间重叠：
  `409 {"detail": {"code": "DISABLE_INTERVAL_OVERLAP"}}`；边界相接（前区间终点 = 后区间起点）允许。

### 6. 命中禁用区间时提交报告（即使校准通过也拒绝）

```bash
curl -sS -X POST http://localhost:8000/reports \
  -H 'Content-Type: application/json' \
  -d '{
        "instrument": "HPLC-01",
        "tested_at": "2026-03-15T08:00:00Z",
        "reference": "R-2026-0009"
      }'
```

`422` 响应（阻断报告不落库，`disable` 即禁用依据；左闭右开，恰为 `disabled_to` 时不命中）：

```json
{
  "detail": {
    "code": "INSTRUMENT_DISABLED",
    "message": "检测时间 2026-03-15T08:00:00+00:00 命中仪器 HPLC-01 的未撤销禁用区间（事件 DIS-2026-001）：进样阀故障待修",
    "instrument": "HPLC-01",
    "tested_at": "2026-03-15T08:00:00+00:00",
    "disable": {
      "event_no": "DIS-2026-001",
      "instrument": "HPLC-01",
      "disabled_from": "2026-03-01T00:00:00+00:00",
      "disabled_to": "2026-05-01T00:00:00+00:00",
      "reason": "进样阀故障待修"
    }
  }
}
```

未命中禁用区间时，请求体、成功响应与原有 `NO_CALIBRATION` / `OUTSIDE_VALID_PERIOD` /
`CALIBRATION_NOT_PASSED` 错误约定完全不变（校准判定与快照格式保持兼容）。

### 7. 撤销禁用与追溯查询

```bash
# 按事件号撤销（未知事件 404 NOT_FOUND；重复撤销 409 DISABLE_ALREADY_REVOKED）
curl -sS -X POST http://localhost:8000/disables/DIS-2026-001/revoke \
  -H 'Content-Type: application/json' -d '{"reason": "进样阀更换完成"}'

# 事件记录保留，仍可按事件号/列表追溯（列表默认含已撤销）
curl -sS http://localhost:8000/disables/DIS-2026-001
curl -sS 'http://localhost:8000/disables?instrument=HPLC-01&include_revoked=true'
```

撤销后该区间不再参与放行判定，同一检测时间即可按校准规则正常放行。

### 8. 按当前禁用状态查询受影响的已发布报告

```bash
curl -sS 'http://localhost:8000/disables/affected-reports?instrument=HPLC-01'
```

返回当前检测时间命中未撤销禁用区间的**已发布**报告及对应禁用依据；
追溯登记一条禁用区间后，历史已放行报告可立即出现在结果中；撤销后随即消失。
原放行记录与发布时快照永不改写（结果中的 `snapshot` 仍记录当时的放行结论）：

```json
{
  "items": [
    {
      "report_id": 1,
      "reference": "R-2026-0001",
      "instrument": "HPLC-01",
      "tested_at": "2026-03-15T08:00:00+00:00",
      "basis_calibration_id": 1,
      "snapshot": {"released": true, "reason_code": "PASSED_CALIBRATION_COVERAGE", "...": "..."},
      "disable": {
        "event_no": "DIS-2026-001",
        "instrument": "HPLC-01",
        "disabled_from": "2026-03-01T00:00:00+00:00",
        "disabled_to": "2026-05-01T00:00:00+00:00",
        "reason": "进样阀故障待修"
      }
    }
  ],
  "count": 1
}
```

### 9. 排班可用性查询（整组仪器何时都能放行）

查询一组仪器在指定时间范围内何时**全部**具备报告放行条件。服务端按“当前未撤销禁用区间优先、
当前校准版本覆盖且通过”的既有放行规则，对查询范围切分并返回**按时间排序的左闭右开时段**。

```bash
curl -sS -X POST http://localhost:8000/scheduling/availability \
  -H 'Content-Type: application/json' \
  -d '{
        "instruments": ["HPLC-01", "GC-02", "MS-03"],
        "from": "2026-01-01T00:00:00Z",
        "to":   "2026-09-01T00:00:00Z"
      }'
```

请求体：

- `instruments`：仪器标识列表，**非空且无重复**（按原始标识判定，不做 trim）；
- `from` / `to`：查询范围**起点（含）/终点（不含）**，必须**带时区偏移**（`Z` 或 `+08:00`，
  服务端统一换算到 UTC）；要求 `from < to`。

响应（节选）：每个时段给出 `from`/`to`、`all_releasable`（整组是否都可放行）以及按入参顺序
排列的各仪器结论 `instruments[]`：`releasable`、`reason_code`、所据 `basis_disable` /
`basis_calibration`（无依据时为 `null`）。

```json
{
  "query": {"instruments": ["HPLC-01", "GC-02", "MS-03"],
            "from": "2026-01-01T00:00:00+00:00", "to": "2026-09-01T00:00:00+00:00"},
  "segments": [
    {
      "from": "2026-01-01T00:00:00+00:00",
      "to":   "2026-03-01T00:00:00+00:00",
      "all_releasable": false,
      "instruments": [
        {"instrument": "HPLC-01", "releasable": true,
         "reason_code": "PASSED_CALIBRATION_COVERAGE",
         "basis_disable": null,
         "basis_calibration": {"id": 1, "version": 1, "passed": true, "...": "..."}},
        {"instrument": "GC-02", "releasable": true,
         "reason_code": "PASSED_CALIBRATION_COVERAGE",
         "basis_disable": null, "basis_calibration": {"...": "..."}},
        {"instrument": "MS-03", "releasable": false,
         "reason_code": "NO_CALIBRATION",
         "basis_disable": null, "basis_calibration": null}
      ]
    },
    {"from": "2026-03-01T00:00:00+00:00", "to": "2026-04-01T00:00:00+00:00",
     "all_releasable": false, "instruments": [ "...HPLC-01 命中 INSTRUMENT_DISABLED，给出 basis_disable..." ]}
  ],
  "count": 6,
  "planning_only": true
}
```

切分与合并语义：

- **禁用优先**：任一时点命中未撤销禁用区间即为 `INSTRUMENT_DISABLED` 并给出禁用事件依据，
  即使该仪器校准通过；未命中时再按当前校准判定
  （`PASSED_CALIBRATION_COVERAGE` / `CALIBRATION_NOT_PASSED` /
  `OUTSIDE_VALID_PERIOD` / `NO_CALIBRATION`）。
- **切点 = 查询首尾 + 各禁用/校准区间边界（裁剪到查询范围内）**；区间均为左闭右开，
  恰为终点不命中；开放区间（终点 `null`）的开放终点统一**截在查询终点**。
- **相邻合并**：仅当整组结论（`all_releasable`）与各仪器原因码及所据禁用事件/校准版本
  **全部相同**才合并；原因码相同但依据版本不同（如两条首尾相接的“通过”校准）仍在边界切开；
  首尾两段即使同名也不跨中间段合并。
- **无当前校准的仪器**在每段都显示 `NO_CALIBRATION`，其他仪器可放行不会把它掩盖——
  因此只要有一台仪器不可放行，`all_releasable` 即为 `false`。

输入错误（均为 `422`，`detail.code` 机器可读）：

- `EMPTY_INSTRUMENTS`：列表为空或含空串标识；
- `DUPLICATE_INSTRUMENT`：标识重复（`detail.duplicates` 列出重复值）；
- `TIMEZONE_REQUIRED`：`from`/`to` 未带时区偏移（不静默按 UTC 补）；
- `INVALID_TIME_RANGE`：`from >= to`。

> 该接口为**只读**：`planning_only=true` 表示结果仅供规划，**不预占未来放行资格**、
> 不写任何数据；正式报告仍以提交 `POST /reports` 时的已提交状态判定。整次查询在**同一个
> SERIALIZABLE 只读事务**内取齐全部仪器的禁用与校准状态，依据同一已提交快照，与并发的
> 校准更正、禁用登记/撤销不会混用前后状态。既有的报告提交与错误约定保持不变。

### 脚本化完整演示

```bash
bash examples/curl-demo.sh                     # curl 版全流程（含禁用登记/撤销）
bash examples/curl-scheduling.sh               # 本轮新增：排班可用性查询
BASE_URL=http://localhost:8000 python3 examples/demo.py   # 含并发更正与禁用流程的 Python 版
```

## 并发正确性如何保证

1. **不会产生双最新版**
   - 唯一部分索引 `uq_calibration_one_current ON (chain_id) WHERE is_current`；
   - 更正先锁版本链行（`SELECT … FOR UPDATE`），再“旧版下线 + 新版插入”，同链并发更正天然串行；
   - 版本号非连续（基于过期版本提交）返回 `409 VERSION_CONFLICT`。
2. **当前校准区间不会重叠**：GiST 排他约束
   `EXCLUDE (instrument WITH =, tstzrange(valid_from, valid_to, '[)') WITH &&) WHERE (is_current)`，
   对并发插入同样生效，冲突映射为 `409 INTERVAL_OVERLAP`。
3. **不会依据过期校准放行**：发布与更正均为 SERIALIZABLE 事务；
   发布时锁定该仪器当前校准行（覆盖行 `FOR UPDATE`，无覆盖时锁全部当前行），
   与“下线旧版”的更正事务互斥；若重试后仍冲突，返回 `409 TRANSACTION_CONFLICT` 而非脏放行。
4. **未撤销禁用区间不会重叠**：事件号有全局唯一索引（撤销后也不可复用）；
   GiST 排他约束
   `EXCLUDE (instrument WITH =, tstzrange(disabled_from, disabled_to, '[)') WITH &&) WHERE (NOT revoked)`
   对并发登记生效，冲突映射为 `409 DISABLE_INTERVAL_OVERLAP`，边界相接允许。
5. **不会在已生效禁用区间内误放行**：发布、登记禁用、撤销禁用均为 SERIALIZABLE 事务。
   发布时先锁定命中的未撤销禁用行（未命中则锁该仪器全部未撤销禁用行），
   与登记/撤销事务互斥；任一事务以串行化失败告终都会整体重试，
   重试后仍冲突返回 `409 TRANSACTION_CONFLICT`，绝不放行。撤销按事件号行锁串行，
   未知事件 `404`、重复撤销 `409 DISABLE_ALREADY_REVOKED`。
6. **追溯一致**：受影响报告查询为 SERIALIZABLE 只读事务，按当前未撤销禁用区间实时计算，
   不物化、不改写报告与快照；登记/撤销的结果对之后的查询立即可见。
7. **排班查询整段快照一致**：`/scheduling/availability` 在单个 SERIALIZABLE 只读事务内
   一次性取齐整组仪器的当前校准与未撤销禁用区间，再做切分；与并发的更正、禁用登记/撤销
   冲突时按 SERIALIZABLE 语义读到的必然是某一个已提交状态（必要时事务重试），
   绝不会在同一次结果里混用前后状态。只读、不预占、不改写任何数据。
