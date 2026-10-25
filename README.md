# LogHarvest

> 压缩日志规则提取平台 · Compressed Log Rule Extraction Platform

LogHarvest 面向企业 NAS 上的海量压缩日志：按任务目录递归扫描压缩文件，拆分给多个 Worker 并行下载、解压和规则匹配，将命中记录连同压缩包路径、包内路径、文件名、物理行号、原始行和规则版本写入 PostgreSQL。

M0 核心引擎、M1a 控制面 API、M1b Worker/队列、M2a 后端认证/运维、M2b 前端与 M2c 安全/可观测性已落地：规则编译/匹配、压缩包发现与流式读取、Worker 处理器、七张核心表模型；数据源、规则集版本、作业规划、文件任务、结果与系统状态 REST API；PostgreSQL `SKIP LOCKED` 任务队列、Worker 常驻领取循环（复制/解析期租约续期、僵死回收、重试退避、尝试历史、成员级 `partial`）、作业终态汇总、SSE 进度与 Worker drain/resume 管理；JWT 登录与最小 RBAC、Alembic 迁移与启动接线、取消/永久失败/手动重试语义、概览统计与服务端维护入口；Vue 3 + TypeScript + Element Plus 前端（派生自 vue-pure-admin，MIT）含登录、概览、作业+SSE 实时进度、规则集、数据源、命中结果游标浏览与 Worker 管理。

后端见 [`service/`](service/)（接口、登录流程、Worker 启动说明在 [`service/README.md`](service/README.md)），前端见 [`web/`](web/)（开发、联调与授权声明在 [`web/README.md`](web/README.md)）。真实 PostgreSQL 集成测试见 `service/tests/integration/`，仓库同时归档架构设计与技术选型文档。

## 核心约束

- 单批次日志 500 GB 以上。
- 单批次压缩文件 1000+ 个。
- 单个文本日志按 10 万行级设计。
- 单机内存上限 16 GB；本地临时盘可容纳 20 个最大压缩文件。
- NAS 原始数据只读，不删除、不移动、不重命名、不写入。
- Worker 默认 4 个进程、最多 6 个，复制/解析并发由内存决定。

## 三服务架构

```text
浏览器
  │ HTTPS
  ▼
Nginx / Vue 前端（Vue 3 + TypeScript + Element Plus）
  │ REST
  ▼
FastAPI Server ──────────────> PostgreSQL
  │                           （元数据、规则、file_tasks、matches）
  │ 扫描任务指定的 NAS 目录          ▲
  ▼                                │ SKIP LOCKED 领取任务
企业 NAS（只读） <── Worker 池 ─────┘
                    复制到本地 → 解压 → 规则匹配 → 写入结果
```

## 关键设计决策

| 层 | 选择 |
|---|---|
| 前端 | Vue 3 + TypeScript + Element Plus + Monaco，复用 Hello-FastApi / Pure Admin |
| Web Server | FastAPI + SQLModel + DDD，复用现有 JWT/RBAC |
| 任务队列 | PostgreSQL `file_tasks` + `SKIP LOCKED`，不引入 Celery/Redis 任务队列 |
| 数据源 | 企业 NAS 挂载，任务创建时指定目录，默认递归、最大深度 5 |
| Worker | Python 多进程，从 NAS 只读复制到本地临时盘，流式解压与规则匹配 |
| 规则 | 关键字、正则、数值门限、字段提取；v1 单行优先，多行堆栈后续扩展 |
| 结果存储 | PostgreSQL 按月分区，批量写入 + 游标分页 |
| Worker 升级 | Web 只做状态/版本/drain；Jenkins + Docker Compose 执行滚动替换 |

## 设计文档

- [三服务精简方案](docs/superpowers/specs/2026-10-24-compressed-log-extraction-design.md)
- [技术栈选型评估](docs/superpowers/specs/2026-10-24-technology-selection-evaluation.md)

## 仓库状态

- M0 核心引擎已实现：规则引擎、压缩包发现与流式读取、Worker 处理器、七张核心表 SQLModel 模型。
- M1a 控制面 API 已实现：管理员 Bearer 认证（该静态 token 已在 M2a 被 JWT 登录替换，不再接受）；数据源创建/列表/路径检测；规则集版本化、配置校验、完整详情与列表分页；作业创建时同步递归扫描 NAS 目录（10 秒扫描上限、文件数超限提前中止）并为每个压缩文件生成 `file_task`（含路径逃逸校验、幂等重放与请求体一致性校验、空目录处理）；作业详情/文件任务/取消；命中结果强制 `job_id` 或时间范围并按 `(created_at,id)` 游标查询；Worker 列表与系统状态。API 测试使用 SQLite，无需 PostgreSQL。接口、认证、作业规划流程与边界见 [service/README.md](service/README.md)。
- M1b Worker/队列已实现：PostgreSQL `file_tasks` + `FOR UPDATE SKIP LOCKED` 领取、`attempts` 租约栅栏的心跳/复制续租/终态写回、`file_task_attempts` 尝试历史、失败线性退避、`recover_stale` 僵死回收与 Worker 启动/每 300 秒周期补偿、`matches` 批量写入与重试清理；Worker 常驻循环 `python -m src.worker`（drain 检查、不绑定作业、信号优雅退出）；作业终态汇总（`succeeded`/`partial`/`failed`/`cancelled`）由最后完成任务的 Worker 写入并周期兜底；SSE 作业进度 `GET /api/jobs/{id}/events`（终态或作业删除即结束、每轮独立短会话）；Worker 管理接口 `drain`/`resume`/`drain-version`。队列 SQL 为 PostgreSQL 专有；真实执行由 `tests/integration/test_postgres_queue.py`（设置 `LOGHARVEST_TEST_PG_URL`）覆盖，SQLite 套件只验证可移植部分。
- M2a 后端认证/运维已实现：`users`/`roles`/`user_roles` 三张表 + bcrypt 口令（超 72 字节先 SHA-256）+ HS256 `TokenService`（access/refresh 带 `type` 声明，不可互换）；`POST /api/login`、`POST /api/refresh-token`、`GET /api/auth/routes`、`POST /api/logout` 四个返回 `{code,message,data}` 信封的端点，其余业务接口保持纯 REST；`AdminDep` 改为解码 JWT 并校验 `admin` 角色码（`401` 缺失/无效/过期，`403` 非管理员）。Alembic `0001_baseline`/`0002_auth_tables` 与启动接线，测试用 autouse fixture 关掉迁移、改用 `create_all`，并逐表逐列比对迁移结果与模型 metadata。队列语义补齐：领取带回 `size_bytes`/`mtime`、复制前源校验、源缺失或变更判 `PermanentTaskFailure` 且 `retryable=False` 不自动重试、行循环取消检查（`is_job_cancelled` → `cancelled`）、`POST /api/jobs/{id}/files/retry-failed` 手动重试。运维接口：`GET /api/system/overview`（分组计数、24 小时命中量、在线 Worker 与版本分布、最老 pending 等待秒数）、`POST /api/system/maintenance`（服务端补偿：回收僵死任务、降级静默 Worker、收尾作业，幂等可重复，周期调度交给外部 cron/compose）、`GET /api/jobs/{id}/files?status=` 状态筛选。认证与迁移的 SQLite/测试边界见 [service/README.md](service/README.md)。
- 成员级容错已实现：zip 损坏成员隔离后继续读取其余成员，流式 tar 在损坏成员处停止并校验结束标记；至少一个 `file_glob` 匹配成员成功时任务写 `partial`，全部匹配成员失败写 `failed`，两者均不自动重试。错误摘要写入 `file_tasks.error`，作业汇总按 `partial` 计入。
- 本地运行注意：应用启动（lifespan）时按 `LOGHARVEST_AUTO_CREATE_TABLES=true` 执行 `alembic upgrade head`，不需要手工建表；新库先跑 `uv run --python 3.12 -m scripts.seed_admin` 建管理员，再用 `POST /api/login` 换 access token（静态管理员 token 已不再参与认证）。Worker 必须指向 PostgreSQL 连接串，命令与配置见 [service/README.md](service/README.md)。
- 尚未实现（后续里程碑）：Alembic 迁移在真实 PostgreSQL 上的执行验证（队列 SQL 已由 `tests/integration/test_postgres_queue.py` 覆盖）、Docker/PostgreSQL 部署编排、进程内复制并发池与单任务命中上限强制、多行堆栈规则、动态菜单与按钮级权限、token 黑名单/服务端会话与 Redis。
- 前端验证口径：`pnpm typecheck` 与 `pnpm build` 均通过，接口契约用真实后端逐条实测；但本机内置浏览器是隐藏页，**未做任何人工视觉确认**，没有界面被断言"渲染正确"。
- 下一步：多行堆栈规则（M3）按实际需求排期；部署编排由 Jenkins + Docker Compose 侧承接。
- 历史详细稿保留在本地 `docs/superpowers/specs/archive/`，未纳入仓库，避免与当前方案混淆。
