# LogHarvest Service

M0 核心引擎 + M1a 控制面 API：规则编译/匹配、压缩包发现与流式读取、Worker 任务处理器、七张核心表 SQLModel 模型，以及数据源、规则集版本、作业规划、文件任务、结果与系统状态 REST API。

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
| `LOGHARVEST_DATABASE_URL` | `sqlite:///./logharvest.db` | 相对路径基于启动目录；M1a 测试使用内存 SQLite |
| `LOGHARVEST_AUTO_CREATE_TABLES` | `true` | 仅保留开关，尚未接入应用启动（M1b） |
| `LOGHARVEST_WORKER_VERSION` | `0.1.0` | Worker 版本号；写入任务尝试记录的逻辑尚未接入（M1b） |
| `LOGHARVEST_MAX_CONCURRENT_COPIES` | `4` | 单 Worker 本地复制并发上限；执行逻辑尚未接入（M1b） |
| `LOGHARVEST_WORKER_POLL_SECONDS` | `1.0` | Worker 轮询间隔；尚未被运行时消费（M1b） |
| `LOGHARVEST_MATCHES_BATCH_SIZE` | `1000` | 命中结果批量写入条数；尚未被运行时消费（M1b） |
| `LOGHARVEST_MAX_MATCHES_PER_TASK` | `200_000` | 单任务命中上限；尚未被运行时消费（M1b） |

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

## M1a API

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
| `GET` | `/api/system/status` | 文件任务状态计数、最老 pending 等待秒数、在线 Worker 数 |

所有列表接口都分页：`limit` 默认 `50`、`offset` 默认 `0`；数据源、规则集、作业、文件任务与命中结果的 `limit` 最大 `500`，Worker 最大 `200`；`limit` 超出上限或 `offset` 为负数返回 `422`。`GET /api/matches` 使用 `(created_at, id)` 游标而不是 `offset`：查询参数为 `created_before` + `id_before`（必须成对提供，取上一页最后一条的 `created_at` 与 `id`），按 `created_at DESC, id DESC` 排序，`limit` 默认 `50`、最大 `500`。

规则集创建时会调用运行时编译器与 Worker 输入校验：正则语法、flags（仅 `i`/`m`/`s`）、threshold 命名分组、`file_glob`（gitwildmatch）、`encoding`/`fallback_encoding` 编码名；任一非法返回 `422`，不会写入数据库。

### 创建作业流程

1. `POST /api/data-sources` 登记 NAS 挂载点 `root_path`（默认 `read_only=true`、`copy_to_local=true`）。
2. `POST /api/rule-sets` 提交规则配置（关键字/正则、`file_glob`、编码与回退编码），得到 `rule_set_id` 与 `version`；同名再次提交会新增版本。
3. `POST /api/jobs` 提交 `name`、`data_source_id`、`source_path`、`rule_set_id`，可选 `file_filter`（默认 `recursive=true`、`max_depth=5`、`max_files=20000`）。`suffixes` 省略或为 `null` 时使用压缩包默认后缀集合；显式传入时先与压缩包后缀集合求交，只保留 zip/tar/gz/bz2/xz 等可解压格式，交集为空（例如只传 `.log`/`.txt`）返回 `400`，原始文本文件不会进入作业。可选请求头 `Idempotency-Key`。
4. 服务解析并校验路径：`source_path` 必须位于数据源 `root_path` 之内（路径逃逸 `400`），且必须是已存在目录（`400`）。
5. 在请求内同步递归扫描该目录，识别 zip、tar 系列、单文件 gz/bz2/xz；跳过符号链接和点目录。`max_depth` 是静默截断：相对深度达到 `max_depth` 的目录不再下探（该层文件仍会纳入），不会报错；扫描在发现第 `max_files + 1` 个压缩文件时立即中止并返回 `400`，不再遍历剩余目录；扫描超过 10 秒返回 `503`。扫描前会结束当前数据库读事务，避免长时间占用连接。`max_depth` 超出 1–20 或 `max_files` 超出 1–20000 属于 schema 校验失败，返回 `422` 且不开始扫描。
6. 创建一条 `Job`，并为每个压缩文件创建一条 `FileTask`（`pending`，`job_id + source_path` 唯一），累计 `total_files` / `total_bytes`。目录为空或未发现匹配的压缩文件时直接 `succeeded`；发现压缩文件时初始为 `running`。M1a 没有 Worker 领取循环，因此存在 `pending` 文件任务的作业会保持 `running` 直到 M1b。
7. 相同管理员使用相同 `Idempotency-Key` 再次提交返回已有作业（`200`）；键会去除首尾空白后参与比较与入库，空白字符串返回 `400`。相同键但 `name`/`data_source_id`/`source_path`/`rule_set_id`/`file_filter` 归一化后不一致返回 `409`。并发插入冲突时会回查并返回已创建作业。
8. `POST /api/jobs/{id}/cancel` 将 `pending` 文件任务置为 `cancelled` 并设置 `cancel_requested=true`；若没有 `running` 任务则作业立即 `cancelled`，已处于终态的作业不做变更。

## 测试

- API 测试（`tests/api/`、`tests/test_health.py`）使用内存 SQLite（`StaticPool`）与 FastAPI `dependency_overrides`，不需要 PostgreSQL；每个 API 用例通过 `create_db_and_tables()` 建表。`tests/domain/` 与 `tests/application/` 为领域/应用单元测试，不经过 API 与依赖覆盖（模型测试自行使用内存 SQLite 验证建表）。
- `tests/api/` 覆盖认证、数据源、规则集、作业规划/幂等/路径校验/取消、结果查询、Worker 与系统状态；`tests/domain/` 与 `tests/application/` 覆盖 M0 引擎。
- 作业唯一约束、幂等并发回退等并发语义仅在 SQLite 上验证，PostgreSQL 行为留待 M1b 验证。

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

### 明确不包含（M1b / M2）

- PostgreSQL 队列适配与 `file_tasks` + `SKIP LOCKED` 领取任务；M1a 只写入 `pending` 任务，不消费。
- Worker 运行时/领取循环、心跳与任务尝试记录接线、drain/升级管理接口。
- SSE 或 WebSocket 实时进度推送。
- Vue 3 + TypeScript + Element Plus 前端、Worker 管理页。
- Alembic 迁移与应用启动建表接线。
- Docker/PostgreSQL 部署编排。
- 多行堆栈规则；v1 保持单行优先。

## 目录

```text
service/
├── src/
│   ├── api/
│   │   ├── dependencies.py         # Session / 管理员认证依赖
│   │   └── v1/
│   │       ├── router.py           # /api 路由聚合与健康检查
│   │       ├── routes_data_sources.py
│   │       ├── routes_jobs.py
│   │       ├── routes_matches.py
│   │       ├── routes_rule_sets.py
│   │       ├── routes_system.py
│   │       └── schemas.py          # 请求/响应模型
│   ├── application/
│   │   ├── services/               # 数据源、规则集、作业规划与查询
│   │   └── worker/                 # Worker 处理器与端口
│   ├── config/settings.py          # LOGHARVEST_* 配置
│   ├── domain/
│   │   ├── archive/                # 压缩包发现与流式行读取
│   │   ├── models/entities.py      # 七张核心表
│   │   └── rules/                  # 规则编译与匹配
│   ├── infrastructure/database.py  # 引擎与建表函数
│   └── main.py                     # FastAPI 入口
├── tests/                          # pytest 用例（api / application / domain）
├── pyproject.toml
├── uv.lock
└── .python-version
```
