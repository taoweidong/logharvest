# LogHarvest Service

M0 核心引擎：规则编译/匹配、压缩包发现与流式读取、Worker 任务处理器、七张核心表 SQLModel 模型。

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

健康检查：

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
| `LOGHARVEST_DATABASE_URL` | `sqlite:///./logharvest.db` | M0 本地使用 SQLite，相对路径基于启动目录；M1 接入 `create_db_and_tables` 后会在当前工作目录生成 `logharvest.db`（已被 `.gitignore` 忽略）；M0 只有模型和模型测试，不自动建库 |
| `LOGHARVEST_AUTO_CREATE_TABLES` | `true` | M0 仅保留该开关，尚未接入应用启动（M1 接线） |
| `LOGHARVEST_WORKER_VERSION` | `0.1.0` | Worker 版本号；写入任务尝试记录的执行逻辑尚未接入（M1 接线） |
| `LOGHARVEST_MAX_CONCURRENT_COPIES` | `4` | 单 Worker 本地复制并发上限；作为并发上限生效的执行逻辑尚未接入（M1 接线） |
| `LOGHARVEST_WORKER_POLL_SECONDS` | `1.0` | Worker 轮询间隔；尚未被运行时消费（M1 接线） |
| `LOGHARVEST_MATCHES_BATCH_SIZE` | `1000` | 命中结果批量写入条数；尚未被运行时消费（M1 接线） |
| `LOGHARVEST_MAX_MATCHES_PER_TASK` | `200_000` | 单任务命中上限；尚未被运行时消费（M1 接线） |

`src/infrastructure/database.py` 提供 `create_db_and_tables()`，但当前没有运行时路径导入该模块（仅测试使用），`src/main.py` 也不在启动时建库；建表接线留待 M1。

## M0 范围

- 规则编译/匹配：关键字 `any`/`all`、正则命名分组、数值门限、禁用规则、超时恢复。
- 压缩包发现与流式读取：递归发现、zip、tar.gz、单文件 gz/bz2/xz、原始文本、GBK 回退、超长行截断。
- Worker 处理器：本地复制、规则集成员 glob 与编码回退解析、逐行匹配、批量结果缓冲、重试清场、成功/失败终态。
- 七张核心表的 SQLModel 模型：`data_sources`、`rule_sets`、`jobs`、`file_tasks`、`file_task_attempts`、`matches`、`workers`；PostgreSQL 方言下字节/内存计数为 `BIGINT`、`Match.id` 为 `BIGSERIAL`、JSON 列为 `JSONB`。
- 服务骨架：FastAPI 应用与 `GET /api/health` 健康检查。
- 测试：`pytest` 覆盖上述领域逻辑、模型建表与 PostgreSQL 方言 DDL 断言。

## M1 及以后（M0 不包含）

- PostgreSQL 队列适配（`file_tasks` + `SKIP LOCKED` 领取任务）与 PostgreSQL 运行时；M0 仅使用 SQLite 做模型测试。
- 任务/规则/结果查询 API，以及 Worker 管理、drain、升级 API。
- Vue 3 + TypeScript + Element Plus 前端与 Worker 管理页。
- Alembic 迁移；`main.py` 启动建表接线。
- Docker/PostgreSQL 部署编排。
- 多行堆栈规则；v1 保持单行优先。

## 目录

```text
service/
├── src/
│   ├── api/v1/router.py            # 健康检查
│   ├── application/worker/         # Worker 处理器与端口
│   ├── config/settings.py          # LOGHARVEST_* 配置
│   ├── domain/archive/             # 压缩包发现与流式行读取
│   ├── domain/models/entities.py   # 七张核心表
│   ├── domain/rules/               # 规则编译与匹配
│   ├── infrastructure/database.py  # 引擎与建表函数
│   └── main.py                     # FastAPI 入口
├── tests/                          # pytest 用例
├── pyproject.toml
├── uv.lock
└── .python-version
```
