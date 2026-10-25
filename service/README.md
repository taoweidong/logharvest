# LogHarvest Service

M0 核心引擎 + M1a 控制面 API + M1b Worker/队列 + M2a 认证/运维：规则编译/匹配、压缩包发现与流式读取、Worker 任务处理器、七张核心表 SQLModel 模型；数据源、规则集版本、作业规划、文件任务、结果与系统状态 REST API；PostgreSQL `SKIP LOCKED` 任务队列、Worker 常驻领取循环、SSE 作业进度与 Worker drain/resume 管理；JWT 登录与管理员种子、Alembic 迁移与启动接线、取消/永久失败/手动重试语义、概览统计与服务端维护入口。

Worker 与队列需要 PostgreSQL；控制面 API 测试仍使用内存 SQLite，无需外部服务。前端（M2b）不在本里程碑。

## 环境要求

- Python 3.12（仓库内 `service/.python-version` 已固定为 `3.12`，uv 会优先使用该版本）
- [uv](https://docs.astral.sh/uv/)

## 开发

```bash
cd service
uv sync --python 3.12
uv run --python 3.12 pytest
uv run --python 3.12 ruff check src tests scripts
uv run --python 3.12 uvicorn src.main:app --reload --port 8000
```

`pyproject.toml` 的 `addopts` 已包含 `-q`；再追加 `-q` 会连汇总行一起隐藏，故命令中不重复传入。

健康检查（无需认证）：

```bash
curl http://127.0.0.1:8000/api/health
# {"status":"ok","service":"logharvest"}
```

依赖锁定：`service/uv.lock` 已提交，`uv sync --python 3.12` 会按锁文件安装可复现的环境。该锁文件使用阿里云 PyPI 镜像（`https://mirrors.aliyun.com/pypi/simple/`）生成；使用默认 PyPI 的贡献者如需切换索引，可重新执行 `uv lock` 并提交更新后的锁文件。

## 首次启动

应用启动时（`LOGHARVEST_AUTO_CREATE_TABLES=true`，默认值）执行 `alembic upgrade head`，因此本地起服务前不需要手工建表：

```bash
cd service
cp ../.env.example .env                       # 按需修改 LOGHARVEST_* 变量
uv run --python 3.12 alembic upgrade head     # 可选：先手工把库升到 head
uv run --python 3.12 -m scripts.seed_admin    # 创建/补齐管理员账号（幂等）
uv run --python 3.12 uvicorn src.main:app --reload --port 8000
```

`scripts/seed_admin` 从 `LOGHARVEST_ADMIN_PASSWORD` 取口令（未设置则交互式询问），少于 8 位直接拒绝并返回 1；已存在同名用户时只补角色关联，不覆盖已有口令。

登录并调用受保护接口：

```bash
curl -s -X POST http://127.0.0.1:8000/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<seed_admin 里的口令>"}'
# {"code":0,"message":"ok","data":{"accessToken":"...","refreshToken":"...","expires":1792...,"roles":["admin"],...}}

curl -s http://127.0.0.1:8000/api/jobs -H "Authorization: Bearer <accessToken>"
```

## 配置

变量按 `LOGHARVEST_` 前缀从环境变量与 `service/.env` 读取（复制方式见上面"首次启动"）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LOGHARVEST_ENVIRONMENT` | `dev` | 运行环境标识；`dev`/`test` 之外的环境会强制下面的 JWT 与管理员口令 |
| `LOGHARVEST_JWT_SECRET_KEY` | `""` | HS256 签名密钥，**最短 32 字符**；`dev`/`test` 可留空（回退到固定的开发密钥 `unit-secret-key-0123456789-abcdef`），非 `dev`/`test` 必填且留空会在配置校验阶段报错 |
| `LOGHARVEST_ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | `accessToken` 有效期（分钟） |
| `LOGHARVEST_REFRESH_TOKEN_EXPIRE_DAYS` | `7` | `refreshToken` 有效期（天） |
| `LOGHARVEST_ADMIN_USERNAME` | `admin` | `scripts/seed_admin` 创建的管理员用户名；登录本身只校验库中已有的用户 |
| `LOGHARVEST_ADMIN_PASSWORD` | `""` | 种子脚本读取的口令来源；非 `dev`/`test` 环境必须显式配置（留空在配置校验阶段报错），少于 8 位时种子脚本拒绝执行 |
| `LOGHARVEST_ADMIN_TOKEN` | `dev-admin-token` | **历史字段，已不参与任何鉴权**：静态 token 认证已随 JWT 登录移除，`Authorization: Bearer <该值>` 一律 `401`。字段与"非 `dev`/`test` 不得使用默认值"的校验暂时保留，仅为兼容既有部署的环境文件，删除留待后续清理 |
| `LOGHARVEST_DATABASE_URL` | `sqlite:///./logharvest.db` | 相对路径基于启动目录；控制面 API 测试使用内存 SQLite。**启动 Worker 必须改为 PostgreSQL 连接串**，队列 SQL 依赖 PostgreSQL 语法 |
| `LOGHARVEST_AUTO_CREATE_TABLES` | `true` | 现在表示**应用启动时执行 `alembic upgrade head`**（`src/main.py` 的 lifespan），不再是"未接入的开关"；置为 `false` 时启动不动表结构。测试夹具通过 autouse fixture 强制关掉它，改用内存 SQLite 的 `create_all`，因此跑测试不会碰到开发库 |
| `LOGHARVEST_WORKER_ID` | 主机名 | Worker 标识；同机启动多个 Worker 进程时必须逐个设为唯一值，否则会共用 `workers` 表的同一行 |
| `LOGHARVEST_WORKER_VERSION` | `0.1.0` | Worker 版本号，注册与任务尝试记录都会写入 |
| `LOGHARVEST_WORK_DIR` | 系统临时目录 | 本地临时盘根目录，压缩包复制到 `<work_dir>/logharvest/<worker_id>/<task_id>/` |
| `LOGHARVEST_WORKER_POLL_SECONDS` | `1.0` | 队列为空时的轮询间隔 |
| `LOGHARVEST_MATCHES_BATCH_SIZE` | `1000` | 命中结果批量写入条数，已接线到 `TaskProcessor.batch_size` |
| `LOGHARVEST_MAX_CONCURRENT_COPIES` | `4` | 单机并发预算，仅作运维声明与展示；M1b 的并发由启动的 Worker 进程数实现（一个进程一次处理一个任务），进程内并发池未实现 |
| `LOGHARVEST_MAX_MATCHES_PER_TASK` | `200_000` | 单任务命中上限，尚未强制执行 |

## 数据库迁移

DDL 的所有权已经从 `create_all` 交给 Alembic（`alembic.ini` + `service/alembic/`）。`alembic/env.py` 用 `settings.database_url` 建同步引擎（SQLite 走 `connect_args_for`），目标 metadata 是 `SQLModel.metadata`。

| 版本 | 职责 |
|---|---|
| `0001_baseline` | 一次建出 M1 的七张业务表（`data_sources`、`rule_sets`、`jobs`、`file_tasks`、`file_task_attempts`、`matches`、`workers`）。生产库尚不存在，这里有意使用 `SQLModel.metadata.create_all(bind, tables=BUSINESS_TABLES)` 捷径 |
| `0002_auth_tables` | 新增 `users`/`roles`/`user_roles`，写成显式 `op.create_table`/`op.create_index`，不复用捷径，保证可 `downgrade` |

```bash
cd service
uv run --python 3.12 alembic upgrade head   # 幂等：第二次不重复建表
uv run --python 3.12 alembic current        # 0002 (head)
```

启动接线：`src/infrastructure/migrations.py` 的 `run_migrations()` 由 `src/main.py` 的 lifespan 在 `LOGHARVEST_AUTO_CREATE_TABLES=true`（默认）时调用，置 `false` 则启动不动 schema。

**测试例外**：`tests/conftest.py` 有一个 autouse fixture 把 `auto_create_tables` 强制设为 `False`，每个用例改用 `src/infrastructure/database.py:create_db_and_tables()` 在内存 SQLite 上建表。原因是不让测试碰开发库（也避免每个用例跑一遍迁移）。副作用是 `create_all` 会跟着模型自动同步，新增表时忘了写迁移不会被测试发现；为此 `tests/infrastructure/test_migrations.py` 会把 `alembic upgrade head` 的结果逐表、逐列与 `SQLModel.metadata` 比对，漏写迁移直接失败。

`alembic upgrade head` → `alembic current`（`0002 (head)`）→ `alembic downgrade base`（回到只剩 `alembic_version`）已在临时 SQLite 文件上真实执行确认；**未在 PostgreSQL 上验证过**（见"PostgreSQL 依赖与验证边界"）。

## 认证

M2a 起改为 JWT 登录。静态 `LOGHARVEST_ADMIN_TOKEN` **已退出认证路径**：把它的值当作 Bearer token 提交一律 `401`（该配置字段本身暂时保留，见上面配置表）。

流程：

1. `POST /api/login`（`{"username": "...", "password": "..."}`）取 `accessToken`/`refreshToken`。登录**只校验、不创建用户**：库里没有任何用户时返回 `401`（不是 `500`），管理员由 `scripts/seed_admin` 建立。
2. 受保护接口带 `Authorization: Bearer <accessToken>`；只有 `GET /api/health`、`POST /api/login` 与 `POST /api/refresh-token` 不需要凭证（`/api/auth/routes` 与 `/api/logout` 需要）。
3. `POST /api/refresh-token`（`{"refreshToken": "..."}`，字段名是 camelCase 以对齐前端基座）换一对新的 access/refresh。两类令牌带不同的 `type` 声明，不能互换使用。
4. `POST /api/logout` 只回一个空数据信封：服务端没有会话、没有 token 黑名单，令牌靠 `exp` 自然过期（access 默认 30 分钟）。

令牌与口令：

- HS256，声明为 `sub`（用户 id 的字符串形式）、`username`、`type`（`access` / `refresh`）、`exp`。
- 签名密钥取 `LOGHARVEST_JWT_SECRET_KEY`（最短 32 字符）；`dev`/`test` 环境可以留空，此时使用固定的开发密钥 `unit-secret-key-0123456789-abcdef`，本地与测试共享同一把，生产环境留空会在配置校验阶段报错。
- 口令存 bcrypt；超过 72 字节的口令先做 SHA-256 再哈希，避免 bcrypt 静默截断尾部差异。

| 状态码 | 场景 | `detail` |
|---|---|---|
| `401` | 缺少 Bearer 头，或 scheme 不是 `bearer` | `missing bearer token` |
| `401` | 签名不符、已过期或 `type` 不匹配 | `invalid or expired token` |
| `401` | `sub` 不是数字用户 id | `invalid token payload` |
| `401` | 用户不存在或 `is_active=false` | `user is not available` |
| `401` | 登录时用户名或口令错误 | `invalid credentials` |
| `403` | 口令正确但账号被停用 | `user is disabled` |
| `403` | 已认证但没有 `admin` 角色 | `admin role required` |

受保护接口返回的 `401`（认证依赖发出）带 `WWW-Authenticate: Bearer` 头；`POST /api/login` 的 `401` 是普通 `HTTPException`，只有 `{"detail": "invalid credentials"}`，不带该头。

**响应信封**：只有 `POST /api/login`、`POST /api/refresh-token`、`GET /api/auth/routes`、`POST /api/logout` 返回 `{"code": 0, "message": "ok", "data": ...}`（前端基座按 `data.code === 0` 判定）；其余业务接口保持纯 REST 对象与 `{"detail": ...}` 错误体，不包信封。

**RBAC 边界（有意的最简实现）**：授权只看 `admin` 这一个角色码，`roles`/`user_roles` 表已经建好但没有角色管理接口；不做动态菜单、按钮级权限，菜单位在前端代码里静态定义，`GET /api/auth/routes` 因此返回空数组。

## API（M1a 控制面 + M1b 队列/进度 + M2a 认证/运维）

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/health` | 健康检查，无需认证 |
| `POST` | `/api/login` | 用户名口令换 `accessToken`/`refreshToken`；返回信封，`roles`/`permissions` 在 `data` 里 |
| `POST` | `/api/refresh-token` | 用 `refreshToken` 换新令牌对；返回信封 |
| `GET` | `/api/auth/routes` | 需认证；固定返回空数组（菜单在前端静态定义）；返回信封 |
| `POST` | `/api/logout` | 需认证；只回 `data: null`，服务端无会话；返回信封 |
| `GET` | `/api/data-sources` | 数据源列表，`limit`/`offset` 分页 |
| `POST` | `/api/data-sources` | 创建数据源；名称重复返回 `409`；`read_only` 只接受 `true` |
| `POST` | `/api/data-sources/{id}/test` | 检查 `root_path` 的 `exists` / `is_dir` / `readable` |
| `GET` | `/api/rule-sets` | 规则集轻量列表（不含 `config`/`created_by`），按名称、版本排序，`limit`/`offset` 分页 |
| `GET` | `/api/rule-sets/{id}` | 规则集详情，返回完整 `config` 与 `created_by`；不存在返回 `404` |
| `POST` | `/api/rule-sets` | 创建规则集新版本（同名 `max(version)+1`），不覆盖旧版本；配置校验失败返回 `422` |
| `GET` | `/api/jobs` | 作业列表，`limit`/`offset` 分页 |
| `POST` | `/api/jobs` | 创建作业并同步扫描规划；可选 `Idempotency-Key` |
| `GET` | `/api/jobs/{id}` | 作业详情 |
| `GET` | `/api/jobs/{id}/files` | 作业的文件任务，`limit`/`offset` 分页；可选 `status` 精确筛选（`pending`/`running`/`succeeded`/`partial`/`failed`/`cancelled`），值不在枚举内返回 `422`；条目含 `status`、`attempts`、`max_attempts`、`worker_id`、`heartbeat_at`、`error` |
| `POST` | `/api/jobs/{id}/cancel` | 请求取消作业 |
| `POST` | `/api/jobs/{id}/files/retry-failed` | 把该作业处于 `failed` 的文件任务放回队列（置 `pending`、清 `error`/`worker_id`/`finished_at`、`scheduled_at=now()`），`attempts` 历史保留、不重置；返回 `{retried: n}`；作业不存在 `404`，作业已处于非 `cancelled` 的终态 `409` |
| `GET` | `/api/matches` | 命中结果；必须提供 `job_id` 或 `created_from` + `created_to`，支持 `rule_id`、`file_name`、`severity`、`(created_at,id)` 游标与 `limit` |
| `GET` | `/api/workers` | Worker 列表，`limit`/`offset` 分页（不包含心跳/管理写接口） |
| `POST` | `/api/workers/{id}/drain` | 请求排空；返回 `{id, drain_requested, status}`，离线 Worker 只记录标记、状态保持 `offline` |
| `POST` | `/api/workers/{id}/resume` | 取消排空并置为 `online`；对 `offline` Worker 返回 `409` |
| `POST` | `/api/workers/drain-version?version=` | 按版本批量排空；返回 `{drained, flagged}`，`drained` 为在线转 `draining` 的数量，`flagged` 为仅记录标记的离线数量 |
| `GET` | `/api/jobs/{id}/events` | SSE 作业进度，`text/event-stream`；见下文 |
| `GET` | `/api/system/status` | 文件任务状态计数、最老 pending 等待秒数、在线 Worker 数 |
| `GET` | `/api/system/overview` | 概览统计：`{jobs: {status: 数量}, file_tasks: {status: 数量}, matches_24h, workers: {online, by_version: {version: 数量}}, oldest_pending_seconds}`；`workers.online` 统计 `online`/`busy`/`draining`，`by_version` 包含 `offline` 行（升级时用来确认旧版本清零）；`oldest_pending_seconds` 是最老 `pending` 文件任务的等待秒数（整数），没有 pending 时为 `null` |
| `POST` | `/api/system/maintenance` | 立即执行一次服务端补偿：`{recovered, workers_demoted, jobs_finalized}`（回收僵死任务 / 把心跳超时的 Worker 置 `offline` / 收尾全部文件任务已终态的作业）；可重复执行，无待处理项时三项均为 `0`；见"服务端维护入口" |

所有列表接口都分页：`limit` 默认 `50`、`offset` 默认 `0`；数据源、规则集、作业、文件任务与命中结果的 `limit` 最大 `500`，Worker 最大 `200`；`limit` 超出上限或 `offset` 为负数返回 `422`。`GET /api/matches` 使用 `(created_at, id)` 游标而不是 `offset`：查询参数为 `created_before` + `id_before`（必须成对提供，取上一页最后一条的 `created_at` 与 `id`），按 `created_at DESC, id DESC` 排序，`limit` 默认 `50`、最大 `500`。

列表接口的响应是**纯 JSON 数组**（业务接口不包 `{code,message,data}` 信封，也没有 `{items, total}` 包装，总数不在响应里，需要总数时用 `GET /api/system/overview` 的分组计数）。`GET /api/jobs/{id}/files` 的 `status` 筛选与 `limit`/`offset` 叠加，语义是先按状态过滤再分页。

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

1. 注册 Worker（`online`），启动即执行一次补偿：僵死回收（`heartbeat_at` 落后 120 秒的 `running` 任务）、把心跳超时的 Worker 置 `offline`、作业终态补偿扫描。
2. 每轮先读 `workers.drain_requested`：为真时只发送 `draining` 心跳、不领取新任务，当前任务跑完即停。
3. `CLAIM_SQL` 用 `FOR UPDATE SKIP LOCKED` 原子领取一条 `scheduled_at <= now()` 的 `pending` 任务（已请求取消的作业不再领取），`attempts + 1` 并写入 `file_task_attempts`；领取同时把规划时记录的 `size_bytes`/`mtime` 带回给处理器。
4. **源文件校验（复制之前）**：只读 `stat` 一次，源文件不存在、`size_bytes` 与规划不符、`mtime` 与规划不符（按秒比较，naive 时间列按 UTC 解释）任意一条成立即抛 `PermanentTaskFailure`，走永久失败路径，不进入复制。
5. 复制到本地临时盘：1 MiB 分块，至少每 5 秒 `touch` 续租（`id + attempts + status='running'` 栅栏）。续租返回假说明租约已被回收或接管，立即抛 `LeaseLost` 中止，不写任务终态、也不决定作业终态。
6. 流式解压与逐行匹配：每 20000 行或每 5 秒 `heartbeat` 续租并回写进度计数，因此被 `file_glob` 过滤掉的行同样会续租；命中按 `LOGHARVEST_MATCHES_BATCH_SIZE` 批量写入 `matches`。
7. **取消检查**：行循环里每处理完一行调用一次 `cancel_check`（`SELECT cancel_requested FROM jobs WHERE id = ...`），为真即抛 `JobCancelled`、把该 `file_task` 写成 `cancelled`（并关闭当次尝试记录）后停止，不再评估剩余行。注意这是每行一次数据库查询，不是每 2 万行一次。
8. 重试幂等：`attempts > 1` 时先 `delete_for_task` 清理上一次写入的命中，再重新写入。
9. 终态写回 `complete` 使用同一租约栅栏，迟到的完成不会覆盖已被回收的任务；`failed` 且未达 `max_attempts`（5）时退回 `pending`，`scheduled_at` 线性退避 `attempts * 5` 秒。`COMPLETE_SQL` 的自动重排还要求 `:retryable` 为真：`PermanentTaskFailure` 以 `retryable=False` 落 `failed` 终态，**不自动重试**，等人工确认后用 `POST /api/jobs/{id}/files/retry-failed` 手动放回队列。`failed` 且作业已请求取消时写成 `cancelled`，避免作业被永远悬在 `running`。
10. 任务处理成功后尝试汇总作业终态；队列为空时等待 `LOGHARVEST_WORKER_POLL_SECONDS` 再继续领取。Worker 不绑定作业，不会因为某个作业结束而退出。
11. 每 300 秒重复执行僵死回收 + 作业终态补偿，覆盖没有完成回调的路径。

租约栅栏（M1b 写栅栏，M2a 未放宽）：任何 `matches` 写库前都必须先确认 `(file_task_id, attempts, status='running')` 仍然成立（`_verify_lease` → `touch`），丢租约立即 `LeaseLost` 中止且不写终态；`delete_for_task` 之前同样先过栅栏。

手动重试的边界：`retry_failed_tasks` 只搬 `failed` 行、保留 `attempts`，所以一次手动重试仍受 `max_attempts` 约束（回收与 `COMPLETE_SQL` 的比较都是 `attempts < LEAST(max_attempts, 队列上限)`）；`cancelled` 的任务不会被放回。

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

仅在作业仍为 `running` 且不存在 `pending`/`running` 文件任务时写入，同时设置 `finished_at`。并发汇总以 `jobs.status = 'running'` 行级栅栏保证单一写入者；触发点是完成任务的 Worker，兜底是 Worker 启动与每 300 秒的补偿扫描，以及下节的 `POST /api/system/maintenance`。文件级 `partial`（单个压缩包内部分成员解析失败）仍未实现。

## 服务端维护入口

设计方案 §7.2/§7.4 要求"Server 定时补偿兜底"：回收僵死任务、降级静默 Worker、收尾作业。Worker 循环里确实做了这三件事，但它们都跑在 Worker 进程内 —— **一个存活 Worker 都没有时（全部退出、还在滚动升级、队列被清空后进程被回收），没有人执行它们**，作业会一直悬在 `running`、任务一直占着租约。

`service/src/application/services/maintenance.py` 的 `run_maintenance(engine, stale_seconds=120)` 把同一段补偿搬到服务端，`POST /api/system/maintenance`（仅 `admin` 角色）立即执行一次并返回计数：

```json
{"recovered": 0, "workers_demoted": 2, "jobs_finalized": 1}
```

- `recovered`：`recover_stale` 回收的僵死 `running` 任务数（PostgreSQL 专有 SQL）。
- `workers_demoted`：`mark_dead_workers` 降级为 `offline` 并清空 `current_task_id` 的静默 Worker 数。
- `jobs_finalized`：`finalize_finished_jobs` 收尾的作业数。

三步都是幂等 SQL，可重复执行，因此**调度交给外部**（cron、systemd timer 或 compose job，例如每分钟一次）：本服务不内置定时器，也不引入 Redis/后台线程。接口没有请求体，`stale_seconds` 目前是服务层默认参数 120 秒，尚未暴露为查询参数。

验证边界：`mark_dead_workers` 与 `finalize_finished_jobs` 是可移植写法，已在内存 SQLite 上真实执行验证；`recover_stale` 是 PostgreSQL 专有语句（SQLite 上直接语法错误，已实测），仅由 SQL 形状断言覆盖，**未在 PostgreSQL 上执行过**。

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
- 该端点需要认证（`AdminDep`）：浏览器的 `EventSource` **不能**设置 `Authorization` 头，所以前端必须用 `fetch` + 流式读取（`response.body.getReader()`）自己解析 `event:`/`data:` 帧，而不是 `EventSource`。本里程碑没有为 SSE 提供查询参数口令等替代鉴权方式。

## PostgreSQL 依赖与验证边界

- 队列 SQL（`FOR UPDATE SKIP LOCKED`、`make_interval`、`ON CONFLICT`、CTE `UPDATE ... RETURNING`）只能在 PostgreSQL 上执行。当前开发机没有 PostgreSQL 实例，因此队列部分以 SQL 形状断言 + fake 运行时验证，真实 PostgreSQL 环境与 `SKIP LOCKED` 并发行为仍待补验。
- PostgreSQL 连接会强制 `timezone=UTC`（`connect_args_for`）：时间戳列是 `without time zone`，SQL `now()` 写入的是会话时区的墙上时间，而 Python 侧写入 UTC。若会话不是 UTC，`heartbeat_at`、僵死回收阈值与"静默 Worker 降级"的比较会整体偏移，降级可能永远不触发。
- 作业终态汇总 SQL 使用可移植写法，已在真实建表的 SQLite 上验证聚合与栅栏语义。
- 表结构 DDL 已交给 Alembic（`0001_baseline`、`0002_auth_tables`），并在启动时按 `LOGHARVEST_AUTO_CREATE_TABLES` 执行 `upgrade head`；`create_db_and_tables()` 仍然存在，用途收缩为测试夹具建表。迁移只在临时 SQLite 文件上真实跑过 `upgrade head` / `current` / `downgrade base`，**没有在 PostgreSQL 上执行过**：`0001` 的 `create_all` 捷径与 `0002` 的显式 op 生成的语句在 PG 方言下的行为（`JSONB`、`BIGSERIAL`、`BIGINT` 列类型）仍待补验。

## 测试

- API 测试（`tests/api/`、`tests/test_health.py`）使用内存 SQLite（`StaticPool`）与 FastAPI `dependency_overrides`，不需要 PostgreSQL；每个 API 用例通过 `create_db_and_tables()` 建表。`tests/conftest.py` 的 autouse fixture 会把 `settings.auto_create_tables` 置为 `False`，因此测试进程从不执行 Alembic，也不会碰开发库；`auth_headers` 夹具用固定开发密钥签发真实 access token（不再传静态 token）。`tests/domain/` 与 `tests/application/` 为领域/应用单元测试，不经过 API 与依赖覆盖（模型测试自行使用内存 SQLite 验证建表）。
- `tests/api/` 覆盖 JWT 登录/刷新/登出/停用账号、数据源、规则集、作业规划/幂等/路径校验/取消/失败重试、结果查询、Worker 管理、SSE、系统状态与概览/维护接口；`tests/domain/services/` 覆盖 bcrypt 与 `TokenService`（含过期、错密钥、access/refresh 不可互换）；`tests/scripts/test_seed_admin.py` 覆盖管理员种子的幂等与弱口令拒绝；`tests/application/` 覆盖 Worker 处理器的复制续租、时间心跳、租约丢失、取消检查与永久失败；`tests/worker/` 用 fake 仓库覆盖领取循环、drain、注册、僵死回收、周期补偿、取消检查接线与信号退出；`tests/infrastructure/` 覆盖队列 SQL 形状、批量写入、作业终态汇总（后两者在真实建表的 SQLite 上执行）以及迁移结果与 `SQLModel.metadata` 的逐表逐列比对。
- 作业唯一约束、幂等并发回退、作业终态栅栏等并发语义仅在 SQLite 上验证；`SKIP LOCKED` 领取、`make_interval` 退避、`ON CONFLICT` 注册、`RECOVER_STALE_SQL` 回收等 PostgreSQL 行为留待真实环境验证。概览与维护接口的 `recover_stale` 一步在测试里被替换为计数桩（该语句在 SQLite 上是语法错误），其余两步真实执行。

## 范围

### M0（已交付）

- 规则编译/匹配：关键字 `any`/`all`、正则命名分组、数值门限、禁用规则、超时恢复。
- 压缩包发现与流式读取：递归发现、zip、tar.gz、单文件 gz/bz2/xz、原始文本、GBK 回退、超长行截断。
- Worker 处理器：本地复制、规则集成员 glob 与编码回退解析、逐行匹配、批量结果缓冲、重试清场、成功/失败终态。
- 七张核心表的 SQLModel 模型：`data_sources`、`rule_sets`、`jobs`、`file_tasks`、`file_task_attempts`、`matches`、`workers`；PostgreSQL 方言下字节/内存计数为 `BIGINT`、`Match.id` 为 `BIGSERIAL`、JSON 列为 `JSONB`。

### M1a（已交付）

- 管理员 Bearer 认证（`secrets.compare_digest`、拒绝空 token、非 dev/test 拒绝默认 token）、Session 依赖与测试夹具；`Job` 增加 `idempotency_key` 及 `(created_by, idempotency_key)` 唯一约束。**该静态 token 认证已在 M2a 被 JWT 登录替换，不再接受。**
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

### M2a（已交付）

- 认证领域核心：`users`/`roles`/`user_roles` 三张表、bcrypt 口令哈希（超 72 字节先 SHA-256，拒绝空口令）、HS256 `TokenService`（`access`/`refresh` 带 `type` 声明、密钥最短 32 字符、过期与错签名一律拒绝）。
- JWT 登录链路：`POST /api/login`、`POST /api/refresh-token`、`GET /api/auth/routes`、`POST /api/logout`（四个端点返回 `{"code","message","data"}` 信封），`AdminDep` 由"比对静态 token"替换为"解码 access token + 校验 `admin` 角色码"并接入全部业务路由，`scripts/seed_admin.py` 幂等创建管理员。
- Alembic：`0001_baseline`（七张业务表）+ `0002_auth_tables`（显式 op），`src/infrastructure/migrations.py:run_migrations()` 由 lifespan 按 `LOGHARVEST_AUTO_CREATE_TABLES` 执行 `upgrade head`；测试关掉迁移改用 `create_all`，并用逐表逐列断言防止漏写迁移。
- 队列/Worker 遗留语义：领取语句带回 `size_bytes`/`mtime`，复制前源校验，源缺失或变更走 `PermanentTaskFailure` 并以 `retryable=False` 落 `failed`（不自动重试），行循环取消检查（`is_job_cancelled` → `cancelled`），手动重试 `POST /api/jobs/{id}/files/retry-failed`。
- 概览与运维：`GET /api/system/overview`（作业/文件任务分组计数、24 小时命中量、在线 Worker 与版本分布、最老 pending 等待秒数）、`POST /api/system/maintenance`（`run_maintenance`：回收僵死任务、降级静默 Worker、收尾作业）、`GET /api/jobs/{id}/files?status=` 状态筛选。

### 明确不包含（后续里程碑）

- Vue 3 + TypeScript + Element Plus 前端与 Worker 管理页（M2b）。
- 真实 PostgreSQL 集成测试环境（当前只有 SQL 形状断言与 fake 运行时）；Alembic 迁移与 `alembic upgrade head` 在 PostgreSQL 上的执行同样未验证。
- Docker/PostgreSQL 部署编排；镜像替换由 Jenkins + Docker Compose 执行，Web 不承载部署。
- 进程内复制并发池与 `LOGHARVEST_MAX_MATCHES_PER_TASK` 强制执行；单文件级 `partial` 成员状态。
- 多行堆栈规则；v1 保持单行优先。
- 动态菜单与按钮级权限、用户/角色管理接口（`roles`/`user_roles` 只是为 RBAC 留的表，当前只有 `admin` 一个角色码参与判断）。
- Token 黑名单与服务端会话、Redis 缓存、进程内后台定时器：`POST /api/system/maintenance` 只执行一次补偿，周期由外部 cron / compose job 负责。
- 登录接口的限流与口令策略（长度、复杂度、失败锁定）：口令强度只在种子脚本里检查最短 8 位。

## 目录

```text
service/
├── alembic/
│   ├── env.py                      # 用 settings.database_url 建同步引擎
│   └── versions/                   # 0001_baseline、0002_auth_tables
├── alembic.ini
├── scripts/seed_admin.py           # 幂等创建/补齐管理员账号
├── src/
│   ├── api/
│   │   ├── dependencies.py         # Session / Engine / JWT 与 admin 角色依赖
│   │   └── v1/
│   │       ├── router.py           # /api 路由聚合与健康检查
│   │       ├── routes_auth.py      # 登录/刷新/登出/菜单占位（响应信封）
│   │       ├── routes_data_sources.py
│   │       ├── routes_events.py    # SSE 作业进度
│   │       ├── routes_jobs.py
│   │       ├── routes_matches.py
│   │       ├── routes_rule_sets.py
│   │       ├── routes_system.py    # status / overview / maintenance
│   │       ├── routes_workers.py   # drain / resume / drain-version
│   │       └── schemas.py          # 请求/响应模型
│   ├── application/
│   │   ├── services/               # 数据源、规则集、作业规划、查询、认证与维护
│   │   └── worker/                 # Worker 处理器与端口协议
│   ├── config/settings.py          # LOGHARVEST_* 配置（含 JWT 与管理员）
│   ├── domain/
│   │   ├── archive/                # 压缩包发现与流式行读取
│   │   ├── models/                 # entities.py 七张业务表 + auth.py 三张认证表
│   │   ├── rules/                  # 规则编译与匹配
│   │   └── services/               # 口令哈希与 TokenService
│   ├── infrastructure/
│   │   ├── database.py             # 引擎、连接参数与测试建表函数
│   │   ├── match_writer.py         # matches 批量写入与清理
│   │   ├── migrations.py           # run_migrations() 与 BUSINESS_TABLES
│   │   └── task_queue.py           # PostgreSQL 队列与作业终态汇总
│   ├── main.py                     # FastAPI 入口（lifespan 里执行迁移）
│   └── worker/                     # Worker 常驻循环与 __main__ 入口
├── tests/                          # pytest 用例（api / application / domain / infrastructure / scripts / worker）
├── pyproject.toml
├── uv.lock
└── .python-version
```
