# LogHarvest Service

M0 核心引擎 + M1a 控制面 API + M1b Worker/队列：规则编译/匹配、压缩包发现与流式读取、Worker 任务处理器、七张核心表 SQLModel 模型；数据源、规则集版本、作业规划、文件任务、结果与系统状态 REST API；PostgreSQL `SKIP LOCKED` 任务队列、Worker 常驻领取循环、SSE 作业进度与 Worker drain/resume 管理。

Worker 与队列需要 PostgreSQL；控制面 API 测试仍使用内存 SQLite，无需外部服务。

## 环境要求

- Python 3.12（仓库内 `service/.python-version` 已固定为 `3.12`，uv 会优先使用该版本）
- [uv](https://docs.astral.sh/uv/)

## 开发

```bash
cd service
uv sync --python 3.12
uv run --python 3.12 pytest
uv run --python 3.12 ruff check src tests
uv run --python 3.12 uvicorn src.main:app --reload --port 8000
```

`pyproject.toml` 的 `addopts` 已包含 `-q`；再追加 `-q` 会连汇总行一起隐藏，故命令中不重复传入。

健康检查（无需认证）：

```bash
curl http://127.0.0.1:8000/api/health
# {"status":"ok","service":"logharvest"}
```

依赖锁定：`service/uv.lock` 已提交，`uv sync --python 3.12` 会按锁文件安装可复现的环境。该锁文件使用阿里云 PyPI 镜像（`https://mirrors.aliyun.com/pypi/simple/`）生成；使用默认 PyPI 的贡献者如需切换索引，可重新执行 `uv lock` 并提交更新后的锁文件。

## 配置

复制示例配置后按需修改：

```bash
cd service
cp ../.env.example .env
```

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LOGHARVEST_ENVIRONMENT` | `dev` | 运行环境标识 |
| `LOGHARVEST_ADMIN_TOKEN` | `dev-admin-token` | 管理员 Bearer token；默认值仅允许在 `dev`/`test` 环境使用，其他环境必须显式替换，空值会被拒绝 |
| `LOGHARVEST_DATABASE_URL` | `sqlite:///./logharvest.db` | 相对路径基于启动目录；控制面 API 测试使用内存 SQLite。**启动 Worker 必须改为 PostgreSQL 连接串**，队列 SQL 依赖 PostgreSQL 语法 |
| `LOGHARVEST_AUTO_CREATE_TABLES` | `true` | 仅保留开关，尚未接入应用启动（与 Alembic 一并在后续里程碑） |
| `LOGHARVEST_WORKER_ID` | 主机名 | Worker 标识；同机启动多个 Worker 进程时必须逐个设为唯一值，否则会共用 `workers` 表的同一行 |
| `LOGHARVEST_WORKER_VERSION` | `0.1.0` | Worker 版本号，注册与任务尝试记录都会写入 |
| `LOGHARVEST_WORK_DIR` | 系统临时目录 | 本地临时盘根目录，压缩包复制到 `<work_dir>/logharvest/<worker_id>/<task_id>/` |
| `LOGHARVEST_WORKER_POLL_SECONDS` | `1.0` | 队列为空时的轮询间隔 |
| `LOGHARVEST_MATCHES_BATCH_SIZE` | `1000` | 命中结果批量写入条数，已接线到 `TaskProcessor.batch_size` |
| `LOGHARVEST_MAX_CONCURRENT_COPIES` | `4` | 单机并发预算，仅作运维声明与展示；M1b 的并发由启动的 Worker 进程数实现（一个进程一次处理一个任务），进程内并发池未实现 |
| `LOGHARVEST_MAX_MATCHES_PER_TASK` | `200_000` | 单任务命中上限，尚未强制执行 |

`src/infrastructure/database.py` 提供 `create_db_and_tables()`；API 测试夹具会自动建表，但 `src/main.py` 启动时仍不建库。直接启动 `uvicorn` 前需在 `service/` 下显式建表：

```bash
cd service
uv run --python 3.12 python -c "from src.infrastructure.database import create_db_and_tables; create_db_and_tables()"
```

该命令按 `LOGHARVEST_DATABASE_URL` 建表（默认在启动目录生成 `logharvest.db`）。M1a 尚无 Alembic 迁移，启动建表与迁移留待 M1b。

## 认证

除 `GET /api/health` 外，所有 `/api` 接口（含查询接口）都要求：

```http
Authorization: Bearer <LOGHARVEST_ADMIN_TOKEN>
```

Token 缺失、为空或不匹配返回 `401`，响应带 `WWW-Authenticate: Bearer`，响应体 `{"detail":"invalid admin token"}`。Token 比较使用 `secrets.compare_digest`。`LOGHARVEST_ENVIRONMENT` 不是 `dev`/`test` 时使用默认 `dev-admin-token` 会在启动配置校验阶段报错。当前只有管理员单一角色，未接入 JWT/RBAC。

## API（M1a 控制面 + M1b 队列/进度）

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/health` | 健康检查，无需认证 |
| `GET` | `/api/data-sources` | 数据源列表，`limit`/`offset` 分页 |
| `POST` | `/api/data-sources` | 创建数据源；名称重复返回 `409`；`read_only` 只接受 `true` |
| `POST` | `/api/data-sources/{id}/test` | 检查 `root_path` 的 `exists` / `is_dir` / `readable` |
| `GET` | `/api/rule-sets` | 规则集轻量列表（不含 `config`/`created_by`），按名称、版本排序，`limit`/`offset` 分页 |
| `GET` | `/api/rule-sets/{id}` | 规则集详情，返回完整 `config` 与 `created_by`；不存在返回 `404` |
| `POST` | `/api/rule-sets` | 创建规则集新版本（同名 `max(version)+1`），不覆盖旧版本；配置校验失败返回 `422` |
| `GET` | `/api/jobs` | 作业列表，`limit`/`offset` 分页 |
| `POST` | `/api/jobs` | 创建作业并同步扫描规划；可选 `Idempotency-Key` |
| `GET` | `/api/jobs/{id}` | 作业详情 |
| `GET` | `/api/jobs/{id}/files` | 作业的文件任务，`limit`/`offset` 分页 |
| `POST` | `/api/jobs/{id}/cancel` | 请求取消作业 |
| `GET` | `/api/matches` | 命中结果；必须提供 `job_id` 或 `created_from` + `created_to`，支持 `rule_id`、`file_name`、`severity`、`(created_at,id)` 游标与 `limit` |
| `GET` | `/api/workers` | Worker 列表，`limit`/`offset` 分页（不包含心跳/管理写接口） |
| `POST` | `/api/workers/{id}/drain` | 请求排空；返回 `{id, drain_requested, status}`，离线 Worker 只记录标记、状态保持 `offline` |
| `POST` | `/api/workers/{id}/resume` | 取消排空并置为 `online`；对 `offline` Worker 返回 `409` |
| `POST` | `/api/workers/drain-version?version=` | 按版本批量排空；返回 `{drained, flagged}`，`drained` 为在线转 `draining` 的数量，`flagged` 为仅记录标记的离线数量 |
| `GET` | `/api/jobs/{id}/events` | SSE 作业进度，`text/event-stream`；见下文 |
| `GET` | `/api/system/status` | 文件任务状态计数、最老 pending 等待秒数、在线 Worker 数 |

所有列表接口都分页：`limit` 默认 `50`、`offset` 默认 `0`；数据源、规则集、作业、文件任务与命中结果的 `limit` 最大 `500`，Worker 最大 `200`；`limit` 超出上限或 `offset` 为负数返回 `422`。`GET /api/matches` 使用 `(created_at, id)` 游标而不是 `offset`：查询参数为 `created_before` + `id_before`（必须成对提供，取上一页最后一条的 `created_at` 与 `id`），按 `created_at DESC, id DESC` 排序，`limit` 默认 `50`、最大 `500`。

规则集创建时会调用运行时编译器与 Worker 输入校验：正则语法、flags（仅 `i`/`m`/`s`）、threshold 命名分组、`file_glob`（gitwildmatch）、`encoding`/`fallback_encoding` 编码名；任一非法返回 `422`，不会写入数据库。

### 创建作业流程

1. `POST /api/data-sources` 登记 NAS 挂载点 `root_path`（默认 `read_only=true`、`copy_to_local=true`）。
2. `POST /api/rule-sets` 提交规则配置（关键字/正则、`file_glob`、编码与回退编码），得到 `rule_set_id` 与 `version`；同名再次提交会新增版本。
3. `POST /api/jobs` 提交 `name`、`data_source_id`、`source_path`、`rule_set_id`，可选 `file_filter`（默认 `recursive=true`、`max_depth=5`、`max_files=20000`）。`suffixes` 省略或为 `null` 时使用压缩包默认后缀集合；显式传入时先与压缩包后缀集合求交，只保留 zip/tar/gz/bz2/xz 等可解压格式，交集为空（例如只传 `.log`/`.txt`）返回 `400`，原始文本文件不会进入作业。可选请求头 `Idempotency-Key`。
4. 服务解析并校验路径：`source_path` 必须位于数据源 `root_path` 之内（路径逃逸 `400`），且必须是已存在目录（`400`）。
5. 在请求内同步递归扫描该目录，识别 zip、tar 系列、单文件 gz/bz2/xz；跳过符号链接和点目录。`max_depth` 是静默截断：相对深度达到 `max_depth` 的目录不再下探（该层文件仍会纳入），不会报错；扫描在发现第 `max_files + 1` 个压缩文件时立即中止并返回 `400`，不再遍历剩余目录；扫描超过 10 秒返回 `503`。扫描前会结束当前数据库读事务，避免长时间占用连接。`max_depth` 超出 1–20 或 `max_files` 超出 1–20000 属于 schema 校验失败，返回 `422` 且不开始扫描。
6. 创建一条 `Job`，并为每个压缩文件创建一条 `FileTask`（`pending`，`job_id + source_path` 唯一），累计 `total_files` / `total_bytes`。目录为空或未发现匹配的压缩文件时直接 `succeeded`；发现压缩文件时初始为 `running`，由 Worker 领取循环消费并在全部文件任务进入终态后汇总作业终态。
7. 相同管理员使用相同 `Idempotency-Key` 再次提交返回已有作业（`200`）；键会去除首尾空白后参与比较与入库，空白字符串返回 `400`。相同键但 `name`/`data_source_id`/`source_path`/`rule_set_id`/`file_filter` 归一化后不一致返回 `409`。并发插入冲突时会回查并返回已创建作业。
8. `POST /api/jobs/{id}/cancel` 将 `pending` 文件任务置为 `cancelled` 并设置 `cancel_requested=true`；若没有 `running` 任务则作业立即 `cancelled`，已处于终态的作业不做变更。仍有 `running` 任务时，作业由该任务完成后的终态汇总收尾（`cancel_requested` 为真时汇总为 `cancelled`）。

## Worker 运行时

```bash
cd service
# 队列只在 PostgreSQL 上运行；Worker 必须指向 PostgreSQL 连接串
LOGHARVEST_DATABASE_URL=postgresql://logharvest:logharvest@127.0.0.1:5432/logharvest \
  uv run --python 3.12 python -m src.worker
```

`SIGTERM`/`SIGINT` 触发优雅退出：完成当前任务后把 `workers.status` 置为 `offline`。

领取循环（`src/worker/main.py` → `src/application/worker/processor.py`）：

1. 注册 Worker（`online`），启动即执行一次僵死回收（`heartbeat_at` 落后 120 秒的 `running` 任务）和一次作业终态补偿扫描。
2. 每轮先读 `workers.drain_requested`：为真时只发送 `draining` 心跳、不领取新任务，当前任务跑完即停。
3. `CLAIM_SQL` 用 `FOR UPDATE SKIP LOCKED` 原子领取一条 `scheduled_at <= now()` 的 `pending` 任务（已请求取消的作业不再领取），`attempts + 1` 并写入 `file_task_attempts`。
4. 复制到本地临时盘：1 MiB 分块，至少每 5 秒 `touch` 续租（`id + attempts + status='running'` 栅栏）。续租返回假说明租约已被回收或接管，立即抛 `LeaseLost` 中止，不写任务终态、也不决定作业终态。
5. 流式解压与逐行匹配：每 20000 行或每 5 秒 `heartbeat` 续租并回写进度计数，因此被 `file_glob` 过滤掉的行同样会续租；命中按 `LOGHARVEST_MATCHES_BATCH_SIZE` 批量写入 `matches`。
6. 重试幂等：`attempts > 1` 时先 `delete_for_task` 清理上一次写入的命中，再重新写入。
7. 终态写回 `complete` 使用同一租约栅栏，迟到的完成不会覆盖已被回收的任务；`failed` 且未达 `max_attempts`（5）时退回 `pending`，`scheduled_at` 线性退避 `attempts * 5` 秒。
8. 任务处理成功后尝试汇总作业终态；队列为空时等待 `LOGHARVEST_WORKER_POLL_SECONDS` 再继续领取。Worker 不绑定作业，不会因为某个作业结束而退出。
9. 每 300 秒重复执行僵死回收 + 作业终态补偿，覆盖没有完成回调的路径。

并发模型：

- 一个 Worker 进程一次只处理一个任务；单机并行通过启动多个进程实现（默认 4、最多 6，内存压力时按 4→3→2 减少进程数）。
- `LOGHARVEST_MAX_CONCURRENT_COPIES` 目前是运维声明的单机并发预算（会展示在 Worker 记录中），进程内并发池尚未实现。
- 同一主机启动多个 Worker 进程时，必须为每个进程设置唯一的 `LOGHARVEST_WORKER_ID`；默认取主机名，多进程共用主机名会互相覆盖 `workers` 表中的同一行状态，并且启动时的本地残留清理可能删掉同 id 对端正在使用的复制目录（该任务会失败重试，NAS 数据不受影响）。
- NAS 只读：源文件仅以 `rb` 打开，不写入、不移动、不改属性；所有写入只发生在 `LOGHARVEST_WORK_DIR` 下按任务隔离的临时目录，任务结束（含失败）后删除。

## 作业终态汇总

| 条件 | `jobs.status` |
|---|---|
| 全部文件任务 `succeeded` | `succeeded` |
| 有成功且存在 `failed`/`partial` | `partial` |
| 无成功且存在 `failed` | `failed` |
| `cancel_requested` 为真 | `cancelled` |
| 文件任务全部被取消 | `cancelled` |

仅在作业仍为 `running` 且不存在 `pending`/`running` 文件任务时写入，同时设置 `finished_at`。并发汇总以 `jobs.status = 'running'` 行级栅栏保证单一写入者；触发点是完成任务的 Worker，兜底是 Worker 启动与每 300 秒的补偿扫描。文件级 `partial`（单个压缩包内部分成员解析失败）仍未实现。

## SSE 作业进度

`GET /api/jobs/{id}/events`，`text/event-stream`：

- 未知作业在返回流之前给出 `404`（`application/json`），不会先发响应头再报错。
- 每秒轮询一次快照：

```text
event: progress
data: {"job_id":1,"status":"running","total":12,"by_status":{"pending":3,"running":1,"succeeded":8},"processed_lines":81234,"matched_rows":977}
```

- `max_events` 默认 `0`（持续到作业终态），`1..100` 为调试/测试用的有限流；非法值返回 `422`。
- 作业进入终态后发送最后一个快照再结束；作业在流中被删除时安静结束。
- 每次轮询使用独立的短生命周期 Session，长连接不会占用数据库连接。

## PostgreSQL 依赖与验证边界

- 队列 SQL（`FOR UPDATE SKIP LOCKED`、`make_interval`、`ON CONFLICT`、CTE `UPDATE ... RETURNING`）只能在 PostgreSQL 上执行。当前开发机没有 PostgreSQL 实例，因此队列部分以 SQL 形状断言 + fake 运行时验证，真实 PostgreSQL 环境与 `SKIP LOCKED` 并发行为仍待补验。
- PostgreSQL 连接会强制 `timezone=UTC`（`connect_args_for`）：时间戳列是 `without time zone`，SQL `now()` 写入的是会话时区的墙上时间，而 Python 侧写入 UTC。若会话不是 UTC，`heartbeat_at`、僵死回收阈值与"静默 Worker 降级"的比较会整体偏移，降级可能永远不触发。
- 作业终态汇总 SQL 使用可移植写法，已在真实建表的 SQLite 上验证聚合与栅栏语义。
- 表结构仍由 `create_db_and_tables()` 建立，Alembic 迁移与应用启动建表尚未接入。

## 测试

- API 测试（`tests/api/`、`tests/test_health.py`）使用内存 SQLite（`StaticPool`）与 FastAPI `dependency_overrides`，不需要 PostgreSQL；每个 API 用例通过 `create_db_and_tables()` 建表。`tests/domain/` 与 `tests/application/` 为领域/应用单元测试，不经过 API 与依赖覆盖（模型测试自行使用内存 SQLite 验证建表）。
- `tests/api/` 覆盖认证、数据源、规则集、作业规划/幂等/路径校验/取消、结果查询、Worker 管理、SSE 与系统状态；`tests/application/` 覆盖 Worker 处理器的复制续租、时间心跳与租约丢失；`tests/worker/` 用 fake 仓库覆盖领取循环、drain、注册、僵死回收、周期补偿与信号退出；`tests/infrastructure/` 覆盖队列 SQL 形状、批量写入和作业终态汇总（后者在真实建表的 SQLite 上执行）。
- 作业唯一约束、幂等并发回退、作业终态栅栏等并发语义仅在 SQLite 上验证；`SKIP LOCKED` 领取、CTE 回收等 PostgreSQL 行为留待真实环境验证。

## 范围

### M0（已交付）

- 规则编译/匹配：关键字 `any`/`all`、正则命名分组、数值门限、禁用规则、超时恢复。
- 压缩包发现与流式读取：递归发现、zip、tar.gz、单文件 gz/bz2/xz、原始文本、GBK 回退、超长行截断。
- Worker 处理器：本地复制、规则集成员 glob 与编码回退解析、逐行匹配、批量结果缓冲、重试清场、成功/失败终态。
- 七张核心表的 SQLModel 模型：`data_sources`、`rule_sets`、`jobs`、`file_tasks`、`file_task_attempts`、`matches`、`workers`；PostgreSQL 方言下字节/内存计数为 `BIGINT`、`Match.id` 为 `BIGSERIAL`、JSON 列为 `JSONB`。

### M1a（已交付）

- 管理员 Bearer 认证（`secrets.compare_digest`、拒绝空 token、非 dev/test 拒绝默认 token）、Session 依赖与测试夹具；`Job` 增加 `idempotency_key` 及 `(created_by, idempotency_key)` 唯一约束。
- 数据源创建/列表（分页）与路径检测；规则集版本化创建/列表（分页）、运行时配置校验与完整详情查询。
- 作业创建时的 NAS 目录递归扫描（`max_files` 提前中止、10 秒 deadline、扫描前释放读事务）、路径逃逸校验、幂等重放/请求体一致性校验、空目录处理、每个压缩文件一条 `file_task`；作业列表/详情/文件任务/取消。
- 命中结果 `(created_at,id)` 游标查询（强制 `job_id` 或时间范围、支持 severity）、Worker 列表、系统状态汇总。
- API 层测试全部基于 SQLite，无外部服务依赖。

### M1b（已交付）

- PostgreSQL 队列适配：`FOR UPDATE SKIP LOCKED` 原子领取、`attempts` 租约栅栏的 `heartbeat`/`touch`/`complete`、`file_task_attempts` 历史、失败线性退避、`recover_stale` 僵死回收、Worker 注册与心跳、`matches` 批量写入与按任务清理。
- Worker 常驻循环 `python -m src.worker`：注册、启动与周期（300 秒）僵死回收和作业汇总补偿、drain 检查、领取-处理-续租-终态、复制期 5 秒 `touch`、行处理每 5 秒或每 20000 行心跳、`LeaseLost` 中止、信号优雅退出与 `offline` 收尾。
- 作业终态汇总：`succeeded`/`partial`/`failed`/`cancelled` 与 `finished_at`，由最后完成任务的 Worker 写入、周期扫描兜底，`status='running'` 行级栅栏保证单一写入者。
- SSE 作业进度 `GET /api/jobs/{id}/events`：终态或作业删除即结束，每次轮询独立短会话，不占用请求级连接。
- Worker 管理接口：`drain`、`resume`、`drain-version`；离线 Worker 不被改写成 `draining`/`online`，但会记录 `drain_requested` 以便重启后生效。

### 明确不包含（后续里程碑）

- Vue 3 + TypeScript + Element Plus 前端与 Worker 管理页。
- Alembic 迁移与应用启动建表接线（`LOGHARVEST_AUTO_CREATE_TABLES` 仍未接入）。
- 真实 PostgreSQL 集成测试环境（当前只有 SQL 形状断言与 fake 运行时）。
- Docker/PostgreSQL 部署编排；镜像替换由 Jenkins + Docker Compose 执行，Web 不承载部署。
- 进程内复制并发池与 `LOGHARVEST_MAX_MATCHES_PER_TASK` 强制执行；单文件级 `partial` 成员状态。
- 多行堆栈规则；v1 保持单行优先。
- JWT/RBAC 接入，当前仍为单一管理员 Bearer token。

## 目录

```text
service/
├── src/
│   ├── api/
│   │   ├── dependencies.py         # Session / Engine / 管理员认证依赖
│   │   └── v1/
│   │       ├── router.py           # /api 路由聚合与健康检查
│   │       ├── routes_data_sources.py
│   │       ├── routes_events.py    # SSE 作业进度
│   │       ├── routes_jobs.py
│   │       ├── routes_matches.py
│   │       ├── routes_rule_sets.py
│   │       ├── routes_system.py
│   │       ├── routes_workers.py   # drain / resume / drain-version
│   │       └── schemas.py          # 请求/响应模型
│   ├── application/
│   │   ├── services/               # 数据源、规则集、作业规划与查询
│   │   └── worker/                 # Worker 处理器与端口协议
│   ├── config/settings.py          # LOGHARVEST_* 配置
│   ├── domain/
│   │   ├── archive/                # 压缩包发现与流式行读取
│   │   ├── models/entities.py      # 七张核心表
│   │   └── rules/                  # 规则编译与匹配
│   ├── infrastructure/
│   │   ├── database.py             # 引擎与建表函数
│   │   ├── match_writer.py         # matches 批量写入与清理
│   │   └── task_queue.py           # PostgreSQL 队列与作业终态汇总
│   ├── main.py                     # FastAPI 入口
│   └── worker/                     # Worker 常驻循环与 __main__ 入口
├── tests/                          # pytest 用例（api / application / domain / infrastructure / worker）
├── pyproject.toml
├── uv.lock
└── .python-version
```
