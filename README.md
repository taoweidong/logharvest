# LogHarvest

> 压缩日志规则提取平台 · Compressed Log Rule Extraction Platform

LogHarvest 面向企业 NAS 上的海量压缩日志：按任务目录递归扫描压缩文件，拆分给多个 Worker 并行下载、解压和规则匹配，将命中记录连同压缩包路径、包内路径、文件名、物理行号、原始行和规则版本写入 PostgreSQL。

M0 核心引擎与 M1a 控制面 API 已落地：规则编译/匹配、压缩包发现与流式读取、Worker 处理器、七张核心表模型，以及数据源、规则集版本、作业规划、文件任务、结果与系统状态 REST API；代码位于 [`service/`](service/)，接口、认证与开发说明见 [`service/README.md`](service/README.md)。PostgreSQL 队列、Worker 运行时、SSE、前端与迁移仍待 M1b/M2，仓库同时归档架构设计与技术选型文档。

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
- M1a 控制面 API 已实现：管理员 Bearer 认证；数据源创建/列表/路径检测；规则集版本化；作业创建时同步递归扫描 NAS 目录并为每个压缩文件生成 `file_task`（含路径逃逸校验、幂等重放、空目录处理）；作业详情/文件任务/取消；命中结果游标查询；Worker 列表与系统状态。API 测试使用 SQLite，无需 PostgreSQL。接口、认证、作业规划流程与边界见 [service/README.md](service/README.md)。
- 尚未实现（M1b/M2）：PostgreSQL `file_tasks` + `SKIP LOCKED` 队列适配、Worker 领取循环与运行时、SSE 实时进度、Vue 前端与 Worker 管理页、Alembic 迁移与启动建表接线、Docker/PostgreSQL 部署编排、多行堆栈规则。
- 下一步：M1b 实现 PostgreSQL 队列与 Worker 运行时，并补 SSE 进度与前端骨架。
- 历史详细稿保留在本地 `docs/superpowers/specs/archive/`，未纳入仓库，避免与当前方案混淆。
