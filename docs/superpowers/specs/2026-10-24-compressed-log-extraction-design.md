# 压缩日志规则提取平台：三服务精简方案

- 文档状态：待评审
- 日期：2026-10-24
- 范围：只做方案与框架设计，不包含实现代码
- 定位：基于企业磁盘挂载和 PostgreSQL 任务队列，先跑通“规则录入 → 任务下发 → 多 Worker 解析 → 结果入库”的核心闭环

已确认约束：

- Web 前端采用 **Vue 3 + TypeScript + Element Plus**，参考 `Hello-FastApi` 的 Pure Admin 前端扩展。
- Web Server 采用 **FastAPI + SQLModel + DDD 分层**，复用现有 JWT/RBAC、日志、配置和 Alembic 迁移。
- 数据源为企业 NAS 挂载目录，Server 和 Worker 都能访问；Worker 复制到本地临时盘后解析。
- **NAS 原始数据只读**：只允许扫描、`stat`、读取和复制；禁止写入、删除、移动、重命名或修改 `mtime`。
- 本地临时盘可容纳 20 个最大压缩文件；单机内存上限 16GB。复制/解析并发由内存决定，默认 4 个 Worker 进程（最多 6 个），磁盘容量不是瓶颈；内存压力时降低并发，不把直读当作内存兜底。
- 任务队列采用 **PostgreSQL `SKIP LOCKED`**，不引入 Celery/Redis 作为任务队列；必须提供任务可观测与重试保障。
- Worker 纳入可视化管理：注册、心跳、版本展示、drain/resume；支持滚动升级，升级期间不丢任务。
- 规则以单行日志为主，多行堆栈作为后续扩展。
- 登录先做基础管理员账号，不扩展复杂权限场景。

---

## 1. 目标与边界

### 1.1 要解决的问题

企业磁盘挂载目录下有大量压缩日志（zip、tar.gz、tar.bz2、单文件 .gz 等），每个压缩包解压后包含多个文本日志。需要：

1. 在 Web 界面配置 NAS 挂载数据源、录入提取规则、创建分析任务。
2. 用户创建任务时指定 NAS 上的日志目录；Server 扫描该目录，把 1000+ 个压缩文件拆成任务写入 PostgreSQL。
3. Worker 从 NAS 挂载盘复制压缩包到本地临时盘，流式解压并逐行执行规则。
4. 满足关键字、正则、数值门限的记录写入 PostgreSQL，并保存源文件路径、包内文件路径、文件名、物理行号、原始行和规则版本。
5. Web 界面查看任务进度、重试历史、失败原因和提取结果。
6. 管理员可在 Web 查看 Worker 状态、版本、当前任务，执行 drain/resume，并按版本滚动升级 Worker。

### 1.2 规模假设

- 单批次压缩日志总量可达 500 GB 以上。
- 单批次压缩文件 1000+ 个。
- 单个文本日志按 10 万行级设计；v1 以 10 万行作为容量目标，超大文件后续扩展。
- 单 Worker 主机内存上限 16GB；本地临时盘可容纳 20 个最大压缩文件。
- 命中结果最终可能达到千万级。

### 1.3 v1 不做的事

- 不做 FTP 接入；数据源只支持企业磁盘挂载目录。
- 不管理 NAS 原始文件：不删除、不移动、不重命名、不写入、不修改属性。
- 不做暂存层（S3/MinIO/NFS 共享暂存）；Worker 从挂载盘复制到本地临时盘。
- 不做复杂规则 DSL（不做跨文件关联、sequence、frequency、复杂表达式）。
- 不做多行堆栈解析；v1 单行优先，多行作为 M3 扩展。
- 不做完整的可观测性平台（不接 OTel/Tempo）；先做任务状态、重试历史、结构化日志和基础指标。
- 不做多租户、复杂 RBAC、作业暂停/恢复。
- v1 不在 Web 服务内执行部署或调用 Docker/K8s 高权限 API；升级统一由现有 Jenkins + Docker Compose 执行，Web 只负责 Worker 状态、版本分布、drain/resume 和升级准备。
- 不把 500 GB 原文存进数据库，数据库只存命中结果和任务元数据。

---

## 2. 总体架构

### 2.1 三服务划分

| 服务 | 职责 | 技术 |
|---|---|---|
| Web 前端 | 数据源、规则录入/试跑、任务创建、进度与重试历史、结果展示 | Vue 3 + TypeScript + Element Plus + Monaco（Pure Admin） |
| Web Server | REST API、规则/任务/结果读写、挂载目录扫描、任务拆分、基础鉴权 | FastAPI + SQLModel + DDD + psycopg |
| Worker | 领取任务、从挂载盘复制文件、流式解压、规则匹配、结果入库、进度上报 | Python 多进程 + psycopg + 标准库解压 |

任务队列不单独部署：`file_tasks` 表本身就是队列，Worker 用 `SKIP LOCKED` 领取。

共享基础设施：

- PostgreSQL：业务数据、任务状态、规则、命中结果，同时承担任务队列。
- Redis：复用 `Hello-FastApi` 现有缓存、会话和限流，不作为任务队列。
- Nginx：托管前端静态文件，反向代理 `/api`。
- 企业磁盘挂载：Server 和 Worker 通过同一挂载路径访问日志。

### 2.2 架构图

```text
浏览器
  │ HTTPS
  ▼
Nginx / Vue 前端（复用 Hello-FastApi 的 Pure Admin）
  │ REST
  ▼
FastAPI Server ──────────────> PostgreSQL
  │                           （元数据、规则、file_tasks、matches）
  │ 扫描任务指定的 NAS 目录            ▲
  ▼                                │ SKIP LOCKED 领取任务
企业磁盘挂载 <──── Worker 池 ───────┘
                  （复制到本地、解压、规则匹配、写进度/结果）
```

说明：

- Server 和 Worker 必须能访问同一个挂载路径；Server 负责扫描和拆分，Worker 负责执行。
- 任务表与业务表在同一个 PostgreSQL 中，任务状态只有一个事实源。
- Redis 是现有基座的一部分，但任务领取、状态和重试全部走 PostgreSQL。

### 2.3 为什么是这三个服务

- 前端独立：规则编辑、任务展示迭代频繁，不和解析任务抢资源。
- Server 独立：只做 API、编排和数据查询，不执行耗时解析，保证 Web 响应稳定。
- Worker 独立：磁盘 I/O 和 CPU 密集，按待处理任务数横向扩容，故障不影响 Web。
- Server 与 Worker 通过 PostgreSQL 任务表解耦：Server 只写 `file_tasks`，Worker 用 `SKIP LOCKED` 领取；不直接 HTTP 调用 Worker。
- v1 不再拆调度器、暂存服务、独立 Planner；Server 中的一个轻量任务规划模块完成目录扫描和任务拆分即可。

### 2.4 与 Hello-FastApi 的集成方式

现有 `Hello-FastApi` 已具备：

- 前端：Vue 3 + TypeScript + Element Plus + Vite，基于 Pure Admin。
- 后端：FastAPI + SQLModel + DDD 四层（api / application / domain / infrastructure）。
- 数据库：SQLite（开发）/ PostgreSQL（生产）。
- 缓存：Redis。
- 认证：JWT 双令牌 + RBAC。
- 部署：Docker Compose；Alembic 迁移；pytest 测试体系。

本方案按以下方式扩展，不另起一套基座：

1. **前端**：在 `web` 中增加日志分析相关 views、API 模块、路由和菜单，复用现有布局、登录、权限、请求封装和组件。
2. **后端**：在现有 DDD 分层中增加“日志分析”限界上下文：
   - `api/v1`：数据源、规则集、任务、结果接口；
   - `application`：任务规划、规则校验、查询服务、DTO；
   - `domain`：规则模型、任务状态、仓储接口；
   - `infrastructure`：SQLModel 仓储、文件系统适配器、批量结果写入。
3. **Worker**：新增独立入口 `service/src/worker/`，复用配置、日志、数据库和规则引擎库；不放在 FastAPI 进程内。
4. **认证与权限**：复用现有 JWT 双令牌和 RBAC；v1 只创建管理员角色，不扩展复杂权限。
5. **Redis**：继续用于现有缓存、会话和限流，不承担任务队列。
6. **数据库**：日志分析模块只支持 PostgreSQL（需要 `SKIP LOCKED`、JSONB 和分区），开发环境也使用 PostgreSQL；不复用 SQLite 开发配置。
7. **迁移与部署**：沿用 Alembic 和 Docker Compose；增加 Worker 服务和挂载盘 volume。

---

## 3. 技术选型

> 详细对比、评分、拒绝理由和迁移路径见 [技术栈选型评估](./2026-10-24-technology-selection-evaluation.md)。本节只保留结论。

| 领域 | 选型 | 说明 |
|---|---|---|
| Web 框架 | FastAPI + SQLModel + DDD | 复用现有基座；Pydantic/SQLModel 校验 |
| 前端 | Vue 3 + TypeScript + Element Plus + Monaco（Pure Admin） | 复用现有前端；表单 + JSON 双模式规则编辑 |
| 数据库 | PostgreSQL 15+ | 元数据、任务、规则、命中结果 |
| 任务队列 | PostgreSQL `SKIP LOCKED` | `file_tasks` 即队列；可观测、可重试 |
| 文件源 | 企业磁盘挂载 | 路径扫描；Worker 复制到本地临时盘 |
| 解压 | 标准库 `zipfile` / `tarfile` / `gzip` / `bz2` / `lzma` | 流式读取，不整包解压到磁盘 |
| 正则 | `regex` | 支持单次匹配超时，防止 ReDoS |
| 关键词预过滤 | `ahocorasick_rs` | 关键词规则一次性扫描 |
| 结果写入 | psycopg 3 + `COPY` / `executemany` | 避免 ORM 逐行写结果 |
| 日志/指标 | `structlog` + `prometheus-client` | 结构化日志；基础指标 |
| 部署 | Docker Compose + Nginx | 复用现有基座；增加 Worker 和挂载盘 |

选型原则：

- 复用 `Hello-FastApi` 已具备的前端、认证、DDD、Alembic 和测试基座，避免重复建设。
- 任务量只有 1000+ 条，PostgreSQL 作为队列足够；不引入 Celery/Redis 任务队列。
- 千万级命中用 PostgreSQL 分区和必要索引承载；不引入 Elasticsearch/ClickHouse。
- 规则引擎自研轻量版，范围严格限制在关键字、正则、门限和字段提取。

---

## 4. 核心流程

```text
1. 管理员配置 NAS 挂载数据源（挂载根路径、只读、启用状态）
2. 用户在 Web 录入规则集，保存为一个版本
3. 用户创建分析任务：选择数据源，**输入或选择本次要分析的 NAS 目录**，配置文件过滤和规则集版本
4. Server 扫描该 NAS 目录，过滤压缩文件，写入 jobs + file_tasks
5. Worker 循环用 SKIP LOCKED 领取 pending 任务；完成当前任务后立即继续领取，直到没有任务或收到停止信号
6. Worker 执行：
   a. 校验源文件存在、大小/修改时间未变
   b. 从挂载盘复制压缩包到本地临时盘
   c. 流式遍历压缩包成员，逐行读取
   d. 执行规则（关键词/正则/数值门限）
   e. 批量写入 matches
   f. 定期更新进度和心跳，完成后写终态
7. Server 聚合 file_tasks，展示任务进度、重试历史和结果
8. 失败任务按策略自动重试；用户可手动重试或取消任务
```

关键原则：

- **一个压缩文件 = 一个 file_task**。1000+ 个文件就是 1000+ 个可并行任务。
- **Worker 持续领取**。Worker 完成一个 `file_task` 后立即回到队列领取下一个，直到队列为空或收到停止信号；Worker 不绑定单个作业。
- **任务表就是队列**。Server 只写 `file_tasks`，Worker 通过 `SKIP LOCKED` 原子领取。
- **PostgreSQL 是唯一事实源**。任务状态、重试历史、命中结果都在 PostgreSQL，不存在外部队列与数据库双写。
- **Worker 直接写结果**。命中结果和任务进度都写 PostgreSQL，Server 不转发解析数据。
- **失败可重试且幂等**。重试前清理该任务旧结果，写入时用 `attempts` 栅栏，避免重复。
- **文件复制到本地**。避免网络挂载抖动影响解析；本地临时盘按“最大压缩包 × 复制并发数”规划。
- **NAS 原始数据只读**。Server 只扫描和 `stat`，Worker 只读打开并复制；所有写操作只发生在本地临时盘和 PostgreSQL。

---

## 5. 规则模型（核心）

### 5.1 v1 规则能力

v1 只做四类能力：

1. **关键字匹配**：任意关键字命中，或全部关键字命中。
2. **正则匹配**：单行正则；命名分组直接作为提取字段。
3. **数值门限**：对提取出的数值字段做 `>`、`>=`、`<`、`<=`、`==` 判断。
4. **输出控制**：严重级别、标签、是否保存原始行。

多行堆栈作为 M3 扩展：规则集预留 `record_start_regex`，命中起始行后把后续非起始行并入同一条记录，再对整条记录执行规则。v1 默认按单行处理。

### 5.2 规则集结构

规则集保存为 JSON，示例：

```json
{
  "name": "app-prod-baseline",
  "file_glob": "**/*.log",
  "encoding": "utf-8",
  "fallback_encoding": "gb18030",
  "rules": [
    {
      "id": "error_keyword",
      "name": "错误关键字",
      "match": {"type": "keyword", "any": ["ERROR", "Exception"], "case_sensitive": true},
      "severity": "error"
    },
    {
      "id": "slow_request",
      "name": "慢请求",
      "match": {"type": "regex", "pattern": "elapsed=(?P<elapsed_ms>\\d+)ms"},
      "threshold": {"field": "elapsed_ms", "op": "gt", "value": 5000},
      "severity": "warning"
    }
  ]
}
```

| 字段 | 说明 |
|---|---|
| `file_glob` | 对压缩包内成员路径做匹配；默认 `**/*` |
| `encoding` / `fallback_encoding` | 主编码与回退编码；默认 UTF-8 / gb18030 |
| `match.type` | `keyword` 或 `regex` |
| `match.any` / `match.all` | 关键字匹配方式；二选一 |
| `match.pattern` | 正则；命名分组作为字段 |
| `threshold` | 可选；对字段做数值比较；字段缺失或无法转换时不命中并计数 |
| `severity` / `tags` | 结果分级和标记 |
| `enabled` | 规则开关 |

约束：

- 规则 ID 在规则集内唯一；`match.any` / `match.all` 二选一；`threshold` 引用的字段必须由正则命名分组产出；正则可编译。
- 正则有单次匹配超时（默认 50 ms）；超时记录错误并跳过该规则，不让 Worker 挂死。
- 纯正则规则会作用于所有行；规则集应控制纯正则规则数量，关键词规则优先走 AC 预过滤。
- 日志编码默认 UTF-8，规则集可配置 `gb18030` 作为回退；解码错误用替换字符并计数，不中断整个文件。
- 单行长度上限默认 1 MB；超过时截断匹配并标记。
- 规则不能执行任意代码，只能使用内置节点。

### 5.3 规则录入与版本

- Web 提供表单模式（关键字、正则、门限、级别）和 JSON 模式（Monaco 编辑器）。
- 提供样本试跑：粘贴样本行或上传小文件，返回命中规则、字段、行号、耗时。
- 保存规则集时生成新版本：`rule_sets(name, version, config)`，不覆盖旧版本；`version = max(version) + 1`，唯一约束兜底。
- 创建任务时固定 `rule_set_id`（指向具体版本）；运行中的任务不受后续规则修改影响。
- v1 不做草稿/发布审批流；保存版本即可用。

---

## 6. 数据模型（表级）

v1 保留 7 张核心表。

### 6.1 `data_sources`：挂载盘数据源

| 字段 | 说明 |
|---|---|
| `id` | 主键 |
| `name` | 数据源名称 |
| `type` | `mount`（企业磁盘挂载） |
| `root_path` | 挂载根路径，例如 `/data/logs` |
| `read_only` | 必须为 true；挂载层与应用层双重只读，禁止任何写操作 |
| `copy_to_local` | 是否先复制到本地临时盘再解析，默认 true |
| `enabled` | 是否启用 |
| `description` | 备注 |
| `created_at` | 创建时间 |

### 6.2 `rule_sets`：规则集版本

| 字段 | 说明 |
|---|---|
| `id` | 主键 |
| `name` | 规则集名称 |
| `version` | 版本号，从 1 递增 |
| `config` | JSONB，完整规则文档（`file_glob`、`encoding`、`rules`） |
| `created_by` / `created_at` | 创建信息 |

唯一约束：`(name, version)`。

### 6.3 `jobs`：分析任务

| 字段 | 说明 |
|---|---|
| `id` | 主键 |
| `name` | 任务名称 |
| `data_source_id` | 挂载盘数据源 |
| `source_path` | 本次任务要扫描的 NAS 目录（用户创建任务时指定，必须位于数据源根路径下） |
| `file_filter` | 扩展名/glob/大小/是否递归/最大深度 |
| `rule_set_id` | 固定的规则集版本 |
| `status` | `pending` / `running` / `succeeded` / `partial` / `failed` / `cancelled` |
| `total_files` / `total_bytes` | 规划结果 |
| `cancel_requested` | 取消标记 |
| `created_by` / `created_at` / `started_at` / `finished_at` | 时间信息 |
| `error` | 规划或汇总错误 |

### 6.4 `file_tasks`：单个压缩文件的执行任务

| 字段 | 说明 |
|---|---|
| `id` | 主键 |
| `job_id` | 所属任务 |
| `source_path` / `file_name` | 挂载盘上的压缩包路径/文件名 |
| `size_bytes` / `mtime` | 规划时记录的大小/修改时间，执行前校验 |
| `status` | `pending` / `running` / `succeeded` / `partial` / `failed` / `cancelled` |
| `attempts` / `max_attempts` | 已尝试次数 / 最大自动重试次数 |
| `scheduled_at` | 最早可领取时间，用于失败退避 |
| `worker_id` | 当前执行 Worker |
| `heartbeat_at` | Worker 最近一次进度上报时间 |
| `processed_lines` / `matched_rows` | 进度 |
| `started_at` / `finished_at` | 时间 |
| `error` | 最近一次失败原因 |

唯一约束：`(job_id, source_path)`，防止重复规划。

### 6.5 `file_task_attempts`：任务尝试历史

| 字段 | 说明 |
|---|---|
| `file_task_id` / `attempt_no` | 联合主键；第几次尝试 |
| `worker_id` | 执行 Worker |
| `worker_version` | 尝试时的 Worker 版本，便于升级兼容排查 |
| `status` | `running` / `succeeded` / `partial` / `failed` / `timeout` / `cancelled` |
| `started_at` / `finished_at` | 起止时间 |
| `processed_lines` / `matched_rows` | 本次尝试的进度和命中数 |
| `error` | 本次失败原因 |

用途：UI 展示重试历史、统计失败原因、排查 Worker 崩溃和超时。每次领取任务时插入一条记录，完成或失败时更新。

### 6.6 `matches`：命中结果

| 字段 | 说明 |
|---|---|
| `id` | 主键（bigserial） |
| `job_id` / `file_task_id` | 所属任务和文件任务 |
| `rule_id` / `rule_set_id` / `rule_version` | 命中规则与规则版本 |
| `source_path` | 压缩包在挂载盘上的路径 |
| `member_path` / `file_name` | 包内路径 / 文件名 |
| `line_no` | 解压后成员内的物理行号（从 1 开始） |
| `raw_line` | 原始行，默认截断到 4 KB |
| `fields` | JSONB，提取出的字段 |
| `numeric_value` | 参与门限判断的数值（可选） |
| `severity` / `tags` | 级别和标签 |
| `created_at` | 入库时间 |

设计要点：

- 按月分区，避免单表无限增长。
- 索引只保留：`(job_id, created_at, id)`、`(file_task_id)`、`(rule_id, created_at)`。
- 批量写入 1000–5000 行/批；命中率高时用 `COPY` 到临时表再合并。
- `raw_line` 截断；完整原文不入库。
- 结果查询强制带 `job_id` 或时间范围；使用游标分页。

### 6.7 `workers`：Worker 注册、心跳与管理

| 字段 | 说明 |
|---|---|
| `id` | Worker 标识（主机名 + 进程/容器 ID） |
| `hostname` | 主机名 |
| `version` | Worker 应用/镜像版本，用于版本分布和升级 |
| `status` | `online` / `busy` / `draining` / `offline` |
| `drain_requested` | 管理员请求排空：不再领取新任务，完成当前任务后退出 |
| `current_task_id` | 当前任务 |
| `max_concurrent_copies` | 本机允许的复制/解析并发（由内存决定，默认 4–6） |
| `cpu_percent` / `memory_bytes` | 资源使用（心跳上报，可选） |
| `temp_disk_free_bytes` | 本地临时盘剩余空间 |
| `last_heartbeat` | 最后心跳时间 |
| `started_at` | 启动时间 |
| `last_error` | 最近一次 Worker 级错误 |

用途：Web“Worker 管理”页面显示在线/忙碌/排空/离线、版本分布、当前任务、资源、心跳；管理员可 drain/resume；升级时按版本排空旧 Worker。Worker 不参与任务调度决策，领取仍以 `file_tasks` 为准。

---

## 7. 任务下发与 Worker 执行

### 7.1 任务规划

`POST /api/jobs` 的主流程：

1. 校验数据源、用户指定的 NAS 路径、规则集版本和文件过滤条件；确认 `jobs.source_path` 位于 `data_sources.root_path` 下。
2. 创建 `jobs` 记录，状态 `pending`。
3. 默认递归扫描 `jobs.source_path`，默认最大深度 5（可配置）；不跟随符号链接，跳过隐藏目录和临时文件；文件数超过上限（如 20,000）时中止并提示缩小目录；扫描放线程池，避免阻塞 FastAPI 事件循环。
4. 批量写入 `file_tasks`，一个压缩文件一条；记录 `source_path`、`size_bytes`、`mtime`；唯一约束保证重复规划不重复建任务。
5. 更新 `jobs.total_files/total_bytes`，状态置 `running`。
6. 返回 `job_id`，前端轮询任务详情；Worker 默认每 1 秒轮询领取 `pending` 任务，可选 `LISTEN/NOTIFY` 降低领取延迟。

递归扫描是默认行为；目录过大时可关闭递归、减小最大深度或缩小任务目录。单个子目录无权限时记录警告并跳过，根目录不可读则任务失败。

规划失败（目录不存在、无权限、路径越界）直接把 `jobs.status` 置 `failed` 并记录错误。目录为空则任务直接 `succeeded`（0 文件）。

任务生成规则：

- `jobs.source_path` 是用户本次指定的 NAS 目录；该目录下符合条件的压缩文件才是本次任务的分析对象。
- 每个压缩文件生成一条 `file_task`，`source_path` 记录完整文件路径；非压缩文件不生成任务。
- 任务写入 PostgreSQL 即视为“下发”完成；Worker 通过 `SKIP LOCKED` 主动领取，不需要 Server 逐条推送。
- Worker 完成一个 `file_task` 后立即领取下一个；直到该作业所有 `file_task` 进入终态，作业才结束。

### 7.2 Worker 执行

Worker 运行配置：

- 每个 Worker 进程一个循环；16GB 内存主机默认运行 4 个 Worker 进程（最多 6 个），每个进程内存预算 1.5–2GB，预留操作系统和 page cache。
- 领取前检查本机可用内存；低于安全阈值时暂停领取或降低并发（4→3→2），不通过直读绕开内存问题。
- Worker **不绑定某个作业**：完成当前 `file_task` 后立即回到领取循环，继续处理全局队列中的下一个 pending 任务；没有任务时等待 1 秒再查，直到进程收到停止信号。
- Worker 启动时向 `workers` 表注册 `id`、`hostname`、`version`、`max_concurrent_copies`；每 5 秒更新心跳、当前任务、资源和本地临时盘剩余空间。
- 领取前检查 `workers.drain_requested`；为 true 时不再领取新任务，完成当前任务后置 `draining`/`offline`。管理员可在 Web 上恢复（resume）。
- 收到 SIGTERM/SIGINT 时停止领取新任务，完成当前任务后退出并置 `offline`；如果进程被强杀，心跳超时由 Server 回收。
- 所有 `file_task` 进入终态后作业才算结束；最后一个完成任务的 Worker 尝试汇总作业终态，Server 定时补偿兜底。
- 领取：`UPDATE file_tasks ... WHERE id = (SELECT id FROM file_tasks WHERE status='pending' AND scheduled_at <= now() ORDER BY scheduled_at, id FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *`，原子取一条任务并置为 `running`；多个 Worker 不会重复领取。
- 默认每 1 秒轮询一次；任务量只有 1000+ 条，轮询开销可以忽略；需要更低延迟时用 PostgreSQL `LISTEN/NOTIFY` 唤醒。
- 单任务设置最长运行时间（如 2 小时），超时由 Worker 主动失败并记录原因；僵死任务由心跳回收兜底。

单个 Worker 任务：

1. 原子领取任务，`attempts` 加 1；以新的 `attempts` 作为 `attempt_no` 插入 `file_task_attempts`；返回的 `attempts` 作为本次执行的栅栏版本。
2. 从 `data_sources` 读取 `root_path`；校验 `file_tasks.source_path` 存在，且 `size_bytes`/`mtime` 与规划时一致；只做只读 `stat`，不修改源文件。
3. 以只读方式将 `file_tasks.source_path` 指向的压缩包复制到本地临时目录（文件名使用任务 ID，避免路径注入）；复制后再次校验大小和 `mtime`。源文件全程不写、不删、不移动、不重命名。
4. 识别格式并流式遍历成员：
   - zip：`zipfile.ZipFile.open()` 逐个成员读取；
   - tar/tar.gz/bz2/xz：`tarfile.open(mode="r|*")` 流式遍历；
   - 单文件 gzip/bz2/xz：视为一个成员；
   - 未压缩文本：视为一个成员。
5. 逐行解码、计数、执行规则；命中写入内存缓冲。
6. 每 1000–5000 行或 1–2 秒批量写 `matches`。
7. 每 5 秒或每 2 万行更新一次 `file_tasks` 进度（`processed_lines`、`matched_rows`、`heartbeat_at`）和 `file_task_attempts`。
8. 完成/失败后写终态、清理本地临时文件；尝试汇总所属 `jobs` 的终态（由服务端定时补偿兜底）。若压缩包内部分成员损坏、其余成员成功，`file_tasks.status` 记为 `partial`，作业汇总为 `partial`。

复制策略：

- 默认 `copy_to_local=true`：Worker 先把压缩包复制到本地临时盘，再解析；避免网络挂载抖动直接影响解析。
- 若挂载盘本身是本地盘或延迟很低，可在数据源配置中关闭复制，直接流式读取；v1 默认开启。
- 复制前检查本地临时盘可用空间；临时盘可容纳 20 个最大压缩文件，容量足够，但**复制/解析并发仍按内存限制为 4–6 个**，不要按磁盘槽位启动 20 个进程。
- 内存压力时优先降低并发（4 → 3 → 2）；直读 NAS 只用于避免复制，不作为内存兜底。单任务仍超过 2GB 时按 `memory_limit` 失败并记录，后续再扩展超大文件处理。
- 每个 Worker 容器设置 2GB 内存上限（Docker `mem_limit` 或 cgroup）；OOM 被系统杀死后由心跳超时回收任务并重试，避免拖垮整机。
- 任务结束或失败后只清理本地临时文件；**禁止在 NAS 上创建、删除、重命名、写入或修改属性**。Worker 启动时清理超过一定时间的本地残留文件。
- Server 和 Worker 在启动/健康检查时校验 NAS 挂载为只读；如果发现可写，拒绝处理并告警，避免误操作。

### 7.3 重试、取消与幂等

- **自动重试**：挂载盘读取超时、文件被短暂占用、数据库短暂不可用等可重试错误，置 `pending` 并设置 `scheduled_at = now() + 退避`（指数退避 + 抖动），`attempts < max_attempts`（默认 3–5 次）。
- **永久失败**：文件不存在、大小/修改时间变化、格式损坏、权限不足等，直接置 `failed`，不自动重试；修复后由用户手动重试。
- **手动重试**：Web 提供“重试失败文件”按钮；将 `failed` 置 `pending`、`scheduled_at=now()`，保留 `attempts` 历史，新增一次尝试记录。
- **重试清场**：Worker 领取后若 `attempts > 1`，先按 `file_task_id` 删除该任务之前写入的 `matches`，再重新处理，保证重试不产生重复结果。
- **写入栅栏**：Worker 以 `(file_task_id, attempts)` 作为轻量栅栏；每批写库前确认任务仍为 `running` 且 `attempts` 未变，否则停止。避免旧 Worker 的迟到写入污染结果。
- **取消**：任务取消设置 `jobs.cancel_requested=true`；Server 把未开始的 `file_tasks` 置 `cancelled`；Worker 每处理 2 万行检查一次取消标记，命中后停止并置 `cancelled`，同时更新尝试历史。
- **僵死任务回收**：Worker 每 5 秒更新 `heartbeat_at`；Server 定期扫描 `running` 且心跳超过 2 分钟的任务，将尝试记录标记为 `timeout`，任务按 `attempts` 置 `pending` 或 `failed`。

状态机：

```text
job:        pending -> running -> succeeded | partial | failed | cancelled
file_task:  pending -> running -> succeeded | partial | failed | cancelled
                       ^                  |
                       └──── retry ───────┘
```

### 7.4 任务可观测与重试保障（必须项）

任务可观测：

- **任务列表**：状态、文件总数、成功/失败/重试中数量、进度、命中数、创建时间。
- **任务详情**：每个文件任务的状态、Worker、尝试次数、已处理行数、命中数、最近错误、心跳时间。
- **尝试历史**：每次尝试的 Worker、开始/结束时间、处理行数、命中数、错误原因；支持查看失败和超时记录。
- **系统状态**：pending 任务数、最老 pending 等待时长、running 数、重试中/失败数、在线 Worker 数、数据库健康。
- **作业完成条件**：该作业下不存在 `pending`/`running` 的 `file_task`。全部 `succeeded` → `succeeded`；存在 `failed`/`partial` 且有成功 → `partial`；全部失败 → `failed`；用户取消 → `cancelled`。Worker 空闲时继续等待新任务，不会因为某个作业结束而退出。
- **结构化日志**：`request_id`、`job_id`、`file_task_id`、`attempt_no`、`worker_id`、`error_code`。
- **指标**：任务总数/各状态数、待处理任务数和最老等待时间、任务耗时、尝试次数、重试次数、失败原因分布、Worker 心跳年龄、处理字节/行数、命中数。
- **告警建议**：最老 pending 超过 10 分钟；失败率超过 5%；Worker 心跳超过 2 分钟；任务 running 但 10 分钟无进度；重试耗尽任务数突增。

重试保障：

- 任务状态、尝试次数、下次执行时间、心跳、错误全部持久化在 PostgreSQL。
- 每次领取和完成都写 `file_task_attempts`，重试历史可追溯。
- 重试前清理旧命中，写入带 `attempts` 栅栏，原子领取避免重复执行。
- 自动重试有上限；永久错误不自动重试；手动重试可绕过自动上限。
- Server 定时补偿：回收僵死任务、汇总作业终态、修复长时间 `pending` 未领取的任务状态。

### 7.5 Worker 可视化管理和滚动升级

**可视化管理页面：**

- Worker 列表：ID、主机、版本、状态（online/busy/draining/offline）、当前作业/任务、处理行数、命中数、最后心跳、CPU/内存、本地临时盘剩余空间、复制并发。
- 版本分布：各版本 Worker 数量、忙碌/空闲/排空数量，升级时直观看到旧版本是否清零；分布默认统计 online/busy/draining，offline 仅作历史记录。
- 操作：drain（排空，不再领取新任务）、resume（恢复）、查看最近错误；v1 不提供手动“指派任务”，任务仍由队列自动领取。
- resume 只对仍在线、尚未退出的 Worker 有效；已经 `offline` 的 Worker 需要重新启动实例。
- 权限：只有管理员可 drain/resume；操作写入审计和结构化日志。

**升级流程（滚动、任务不丢）：**

1. 数据库迁移采用向后兼容的增量变更（先加字段/索引，不删除旧字段）；旧版本 Worker 仍能运行。
2. 构建新版本 Worker 镜像并启动新实例；新 Worker 注册到 `workers`，版本号与旧版本并存。
3. 在 Web 上按旧版本执行 drain（或使用 `drain-version` 接口）；旧 Worker 停止领取新任务，完成当前 `file_task` 后置 `offline`。
4. 确认旧版本没有 running 任务后，停止旧容器；版本分布显示旧版本为 0。
5. 如果旧 Worker 被强杀，心跳超时后任务由 Server 回收并重试；`file_task_attempts` 记录旧版本和中途失败原因。

**兼容性要求：**

- 任务领取协议、`file_tasks` 状态和结果表结构在升级前后保持兼容；数据库变更只做增量。
- 规则版本在任务创建时固定，不依赖 Worker 版本。
- `file_task_attempts.worker_version` 记录每次尝试的版本，便于定位新旧版本差异。
- 升级期间允许新旧 Worker 并存；旧版本只处理已领取任务，新版本领取新任务。

**部署方式：**

- v1 统一由现有 **Jenkins + Docker Compose** 执行升级：Jenkins 构建新镜像、启动新版本 Worker、调用 Web `drain-version`、等待旧版本 running 清零、停止旧容器；Web 不执行部署。
- 如果后续需要原生滚动和可视化服务管理，引入 **Docker Swarm + Portainer** 或 **K3s + Argo CD/Rancher**；Portainer/Argo CD 作为独立运维工具，不嵌入业务 Web。
- K8s：使用 Deployment rolling update + preStop drain + `terminationGracePeriodSeconds`；Worker 就绪探针检查数据库和挂载路径。
- 不自研 deploy-agent；如果确实需要“一键升级”，优先使用成熟编排/部署平台，而不是在业务 Web 中增加 Docker/K8s 调用逻辑。

---

## 8. 面向 500 GB / 1000+ 文件的性能设计

### 8.1 吞吐路径

- **复制后流式解析**：从挂载盘复制到本地临时盘，再流式读取；不把成员全部解压到磁盘。
- **逐行处理**：内存占用与文件大小无关；单文件 10 万行不会造成内存问题。
- **关键词预过滤**：关键词规则使用 Aho-Corasick 一次性扫描；正则规则只在候选行上执行。
- **批量写库**：命中结果按批写入；避免 ORM 逐行插入和自动 flush。
- **规则编译缓存**：Worker 进程内按 `rule_set_id` 缓存编译后的规则和 AC 自动机；规则版本不可变，缓存无需失效。
- **进度节流**：进度每 5 秒/2 万行更新一次，避免 `file_tasks` 行成为写热点。

### 8.2 并行度

- 单机 16GB 内存是主要约束：默认 4 个 Worker 进程，最多 6 个；每个进程预算 1.5–2GB，预留操作系统和 page cache。
- 本地临时盘可容纳 20 个最大压缩文件，容量不是并发目标；`max_concurrent_copies = min(20, 内存允许的进程数)`，默认 4–6。
- 不要按 20 个磁盘槽位启动 20 个解析进程；内存压力时优先降低并发（4 → 3 → 2），不把直读当作内存兜底。
- 1000+ 个文件对应 1000+ 个 `pending` 任务；PostgreSQL 的 `SKIP LOCKED` 足以支撑这个量级。
- 挂载盘读带宽和本地磁盘写入是主要瓶颈：先保证内存安全，再通过增加 Worker 主机扩容。
- 单个压缩文件过大时，v1 允许任务运行时间较长；单文件超过 10 万行不作为 v1 优化目标，仍按流式处理，后续再增加专项优化/拆分，不在首版引入。

### 8.3 数据库

- `matches` 按月分区，千万级数据量下查询和清理都可控。
- 只建必要索引；高命中率写入时索引成本明显，优先保证写入吞吐。
- 结果查询强制带 `job_id` 或时间范围，避免全分区扫描；使用 `(created_at, id)` 游标分页，避免千万级表的深 offset。
- 结果保留期可配置（如 90 天），到期按分区删除；任务元数据保留更久。
- 任务详情和进度聚合基于 `file_tasks(job_id, status)` 索引；领取使用 `(status, scheduled_at)` 索引，僵死任务回收使用 `(status, heartbeat_at)` 索引，作业列表使用 `jobs(status, created_at)` 索引。
- Worker 管理使用 `workers(last_heartbeat)` 判断离线，`workers(version, status)` 统计版本分布；尝试历史按 `(file_task_id, attempt_no)` 主键读取。
- 数据库连接池每 Worker 2–4 个，不要按任务创建连接。

### 8.4 容量估算

```text
Worker 数 ≈ 峰值解压吞吐 ÷ 单 Worker 吞吐 × 1.3
压缩文件任务数 ≈ 本批次压缩文件数（1000+）
命中写入量 ≈ 命中率 × 总日志行数
本地临时盘容量 ≈ 最大压缩包大小 × 20（已确认）
单机并发进程数 ≈ min(20, floor((16GB - 4GB 预留) ÷ 每进程内存预算 2GB)) ≈ 6；默认 4
```

示例：按解压后 500 GB、单 Worker 30 MB/s 估算，单 Worker 约需 4.6 小时；4 个 Worker 约 69 分钟，6 个约 46 分钟（挂载盘读带宽、本地磁盘和数据库写入另计）。这用于判断是否需要增加 Worker，而不是精确承诺。

v1 的目标不是追求极限吞吐，而是验证：

- 1000+ 任务能稳定下发、领取、重试和回收；
- 单 Worker 能处理 10 万行级文件；
- 千万级命中写入不拖垮 PostgreSQL；
- 失败重试、取消和 Worker 崩溃不会产生重复数据；
- 任务状态、尝试历史和失败原因在 Web 上可查。

---

## 9. Web 页面与 API

### 9.1 页面

| 页面 | 内容 |
|---|---|
| 数据源 | 挂载根路径、只读、启用状态、路径可读性测试 |
| 规则集 | 列表、版本、表单/JSON 编辑、样本试跑 |
| 创建任务 | 选数据源 → 输入/选择本次 NAS 目录 → 配置过滤（默认递归、最大深度 5、扩展名）→ 选规则集版本 → 提交 |
| 任务列表 | 状态、文件数、进度、命中数、失败数、重试数 |
| 任务详情 | 文件任务列表、进度、尝试历史、失败原因、取消/重试 |
| 结果查询 | 按任务、规则、文件、级别筛选；查看原始行和行号 |
| Worker 管理 | 在线/忙碌/排空/离线、版本分布、当前任务、资源、drain/resume |
| 系统状态 | Worker 在线数、pending 数/最老等待、失败/重试耗尽数、数据库健康 |

前端每 2–3 秒轮询任务详情；v1 不做 SSE/WebSocket。任务和文件数量大时，任务详情分页展示文件任务。

只有一个 NAS 挂载时，可将数据源设为默认；创建任务只需填写/选择该 NAS 下的目录。数据源的作用是限定挂载根路径和只读权限，真正的任务目录由 `jobs.source_path` 指定。

### 9.2 API（最小集合）

```text
POST   /api/auth/login
GET    /api/data-sources
POST   /api/data-sources
POST   /api/data-sources/{id}/test          # 路径存在/可读/权限
GET    /api/rule-sets
POST   /api/rule-sets                       # 保存新版本
POST   /api/rule-sets/test                  # 样本试跑
POST   /api/jobs                            # 创建并规划任务（data_source_id + source_path + filter + rule_set_id）
GET    /api/jobs
GET    /api/jobs/{id}
GET    /api/jobs/{id}/files
GET    /api/jobs/{id}/files/{file_id}/attempts
POST   /api/jobs/{id}/cancel
POST   /api/jobs/{id}/retry
GET    /api/matches?job_id=&rule_id=&file_name=&cursor=
GET    /api/workers                        # Worker 列表、版本、状态、当前任务
GET    /api/workers/{id}                   # Worker 详情
POST   /api/workers/{id}/drain             # 排空（不再领取新任务）
POST   /api/workers/{id}/resume            # 恢复领取
POST   /api/workers/drain-version          # 按版本排空，用于滚动升级
GET    /api/system/status
GET    /api/health
GET    /api/metrics                         # 可选
```

约定：

- 所有列表分页；任务列表默认 50 条，结果使用游标分页。
- 所有写接口做参数校验和权限校验。
- 错误返回统一 `code`、`message`、`detail`、`request_id`。
- 任务创建接口不在请求内做长时间目录扫描；扫描设置超时，超时返回明确错误，后续可改为后台规划。

---

## 10. 可观测性与运维

### 10.1 任务可观测性

- `jobs`、`file_tasks`、`file_task_attempts` 的状态、进度、错误字段是主要事实来源。
- 任务详情页展示：文件总数、成功/失败/运行中/重试中数量、已处理行数、命中数、当前 Worker、尝试历史。
- Worker 心跳表展示在线数、忙碌数、排空数、最后心跳时间、版本分布和资源使用。
- 心跳超过 2 分钟的 Worker 由 Server 置 `offline`；Worker 管理页可执行 drain/resume。
- 结构化 JSON 日志：Server 带 `request_id`，Worker 带 `job_id`、`file_task_id`、`attempt_no`、`worker_id`、`error_code`。
- 失败任务保留错误摘要（源文件错误、解压错误、规则错误），不把完整堆栈写入数据库。

### 10.2 指标（可选 Prometheus）

v1 只做低成本指标，不接入完整 OTel 链路：

- `http_requests_total{route,status}`、`http_request_duration_seconds`。
- `jobs_total{status}`、`file_tasks_total{status}`、`file_task_attempts_total{status}`。
- `pending_tasks`、`oldest_pending_age_seconds`、`running_tasks`、`retry_tasks`。
- `task_duration_seconds`、`processed_bytes_total`、`processed_lines_total`、`matches_total`。
- `task_errors_total{reason}`、`rule_errors_total{rule_id,reason}`。
- `worker_heartbeat_age_seconds`、`worker_version_info{version}`、`worker_drain_total{version}`。
- `worker_memory_bytes{worker_id}`、`worker_temp_disk_free_bytes{worker_id}`、`worker_oom_total`。

### 10.3 部署

- 复用 `Hello-FastApi` 的 Docker Compose：PostgreSQL、Redis、Server、Nginx + 前端构建产物，新增 Worker 服务和挂载盘 volume。
- 企业磁盘挂载同时挂载到 Server 和 Worker；**必须使用只读挂载**（Docker volume `:ro`、NFS `ro` 或等价方式），建议同时使用 `noatime`，避免读取更新访问时间；Worker 额外挂载可写的本地临时盘，容量按 20 个最大压缩文件规划。
- Worker 容器设置 2GB 内存上限；16GB 主机默认 4 个 Worker（最多 6 个），预留操作系统和 page cache；监控 RSS 和 OOM。
- 启动/健康检查校验 NAS 挂载为只读；如果发现可写，拒绝处理并告警。
- 升级：Jenkins 构建并启动新版本 Worker → Web drain 旧版本 → 等待 running 清零 → Jenkins 停止旧容器；Web 不执行部署。详见 7.5。
- 生产：前端静态文件 + Nginx；Server 2 个副本；Worker 按待处理任务数横向扩容；PostgreSQL 主库。
- 健康检查：Server `/api/health` 检查数据库和挂载路径；Worker 由 `workers` 表心跳监控。
- 备份：PostgreSQL 定期备份；任务状态、重试历史和命中结果在同一个库。

---

## 11. 安全底线

| 风险 | v1 措施 |
|---|---|
| 挂载路径越界 | `source_path` 必须位于 `data_sources.root_path` 下；规范化路径，拒绝 `..` 和绝对路径越界 |
| 符号链接逃逸 | 扫描和复制时使用 `realpath` 校验，禁止跟随指向根路径外的符号链接 |
| 权限过大 | 挂载盘只读；Worker 只读源文件，只写本地临时盘和数据库 |
| NAS 原始数据被误改 | 挂载层 `ro` + 应用层 `read_only=true` + 只读打开；禁止删除/移动/重命名/写入/改属性；测试校验处理前后 `mtime` 和校验和不变 |
| 磁盘占满 | 本地临时盘配额；复制前检查可用空间；任务结束清理 |
| 内存耗尽 / OOM | Worker 被系统杀死、任务失败 | 单进程 1.5–2GB 预算；容器 2GB 限制；16GB 主机默认 4 个 Worker；内存压力降低并发 4→3→2；RSS 监控；OOM 后任务重试 |
| 压缩炸弹 | 限制单成员解压大小、单包总解压大小、压缩比、成员数；超限终止 |
| ReDoS | `regex` 单次匹配超时（50 ms）、正则长度/规则数上限 |
| SQL 注入 | 参数化查询，禁止拼接用户输入 |
| 前端 XSS | 原始日志按纯文本展示；Vue 默认转义；不使用 `v-html` 渲染日志 |
| 越权 | 复用现有 JWT 双令牌 + RBAC；v1 只使用管理员角色；接口级校验 |
| 恶意规则 | 规则节点封闭，不允许任意代码；保存前 schema 校验和样本试跑 |
| Worker 管理越权 | drain/resume/drain-version 仅管理员；写审计日志；Web 不持有 Docker/K8s API 权限 |
| Worker 被访问 | Worker 不暴露入站端口，只读取挂载盘、写数据库 |

---

## 12. 实施阶段

| 阶段 | 交付 | 验收 |
|---|---|---|
| M0 骨架（约 1–2 周） | 复用基座、7 张表、规则 CRUD、任务创建、PG 任务领取、Worker 写结果 | 一条真实日志完成“规则 → 任务 → Worker → 结果”闭环 |
| M1 核心（约 2–3 周） | 挂载目录扫描、复制到本地、zip/tar.gz 流式解析、关键字/正则/门限、字段提取、行号、任务进度、Web 页面 | 一个目录 1000+ 文件可完整跑通；结果路径/行号准确 |
| M2 稳定与规模（约 2 周） | 自动重试、手动重试、取消、幂等、尝试历史、僵死任务回收、Worker 管理页、drain/resume、按版本滚动升级、分区、指标、压测 | 模拟 500 GB / 1000+ 文件；失败重试不重复；Worker 可观测、可排空、可升级 |
| M3 扩展 | 多行堆栈、成员级拆分、暂存层、复杂规则、审计、OIDC | 按实际需求排期 |

实施原则：

- 先跑通闭环，再优化吞吐；不要在 M0 引入暂存、复杂规则引擎和完整可观测性。
- 规则引擎与 Worker 解耦成独立 Python 包，Server 的试跑和 Worker 的正式执行复用同一套代码。
- 所有任务状态以 PostgreSQL 为准；任务表同时承担队列，方便补偿和排障。
- 复用 `Hello-FastApi` 的 DDD 分层、SQLModel、JWT/RBAC、日志、Alembic 和 Docker Compose，避免重复建设。

### 12.1 测试重点

- 单元测试：规则编译/匹配、数值门限、编码回退、行号计数、zip/tar 成员遍历。
- 集成测试：临时挂载目录 + 固定样本压缩包 + Testcontainers PostgreSQL，跑完整“任务 → Worker → 结果”链路。
- 递归扫描测试：多级子目录、隐藏目录、符号链接、无权限子目录；结果完整、不越界、不重复。
- 规模测试：生成 1000+ 个压缩文件、500 GB 级样本，验证下发、进度、重试、取消。
- 资源测试：16GB 主机默认 4 个 Worker、最多 6 个；验证 RSS、容器 2GB 限制、临时盘 20 个最大文件、OOM 后任务可重试。
- 幂等验证：重复领取、Worker 崩溃重跑、手动重试后，`matches` 不产生重复。
- 可观测性验证：尝试历史、心跳、pending 最老等待、失败原因、`/api/system/status`。
- Worker 管理验证：注册、心跳、drain/resume、版本分布；滚动升级期间不丢任务、不重复结果。
- 只读验证：在只读挂载下运行完整任务；尝试写/删源文件必须失败；处理前后源文件 `mtime`、大小和校验和不变。
- 验收标准：源文件路径/成员路径/文件名/行号准确；1000+ 任务可完成；千万级命中可写入和游标分页查询。

---

## 13. 默认假设

| 项目 | 默认选择 | 备选 |
|---|---|---|
| 前端框架 | Vue 3 + TypeScript + Element Plus（复用 Hello-FastApi） | React + Ant Design |
| 任务队列 | PostgreSQL `SKIP LOCKED` | Celery + Redis / Redis Streams / RabbitMQ |
| 解析粒度 | 一个压缩文件一个任务 | 成员级/字节级拆分（M3） |
| 递归扫描 | 默认递归，最大深度 5，可配置 | 不递归 |
| 单文件规模 | v1 按 10 万行级设计；超出后续扩展 | 首版支持超大文件 |
| 单行/多行 | v1 单行；多行堆栈 M3 | 如强需求，可提前到 M1 |
| 数据源 | 企业 NAS 挂载；任务创建时指定具体目录；默认复制到本地后解析；NAS 只读 | 直接读取挂载盘；后续增加对象存储/FTP 适配器 |
| 原始数据 | 只读，不删除/移动/重命名/写入 | — |
| 单机资源 | 内存 16GB；本地临时盘 20×最大压缩包；Worker 默认 4、最多 6 | 更高内存主机 / 更多主机 |
| 内存压力 | 降低并发 4→3→2；直读不作为内存兜底 | 增加内存/主机 |
| 暂存 | 不引入 | S3/MinIO/NFS 暂存（后续） |
| 认证 | 复用现有 JWT/RBAC，v1 只使用管理员角色 | OIDC/SSO |
| Worker 升级 | Jenkins + Docker Compose + drain，Web 不执行部署 | Docker Swarm + Portainer / K3s + Argo CD |
| 前端进度 | 2–3 秒轮询 | SSE/WebSocket |
| 可观测性 | 日志 + 任务状态 + 尝试历史 + 基础指标 | OTel + Tempo + Grafana |

---

## 14. 已确认事项与实现假设

已确认：

1. Server 和 Worker 都能挂载同一个 NAS 目录；Server 负责扫描和任务规划。
2. 本地临时盘可容纳 20 个最大压缩文件；单机内存上限 16GB；Worker 默认 4 个、最多 6 个，复制/解析并发按内存限制，不按磁盘 20 个槽位并发。
3. v1 单行规则优先，多行堆栈放 M3。
4. 使用基础管理员账号，复用现有 JWT/RBAC，只配置管理员角色。
5. NAS 原始数据只读，不删除、不移动、不重命名、不写入。
6. Worker 升级统一由现有 Jenkins + Docker Compose 执行；Web 只做 Worker 可视化、drain 和版本分布，不在 Web 内引入部署逻辑。
7. 单文件规模：v1 按 10 万行级设计，超大文件后续扩展，不在首版做专项优化。
8. 内存压力时降低并发（4→3→2），不把直读 NAS 当作内存兜底；单任务超过 2GB 记录 `memory_limit` 并失败，后续扩展。
9. 任务目录默认开启递归扫描，最大深度 5，可在创建任务时调整。

当前关键约束已全部确认，下一步可按 M0/M1 拆分实施任务清单。

---

## 15. 结论

v1 采用 **Web 前端 + FastAPI Server + Worker** 三服务：

- 前端复用 `Hello-FastApi` 的 Vue 3 + TypeScript + Element Plus + Pure Admin。
- Server 负责数据源、规则、任务、结果和任务规划，复用现有 DDD、SQLModel、JWT/RBAC。
- 用户创建任务时指定 NAS 目录；Server 扫描该目录，把每个压缩文件写成一条 `file_task`。
- Worker 从企业磁盘挂载复制压缩包到本地临时盘，流式解压、规则匹配、批量入库；完成一个任务后继续领取下一个，直到队列为空。
- NAS 原始数据全程只读：不删除、不移动、不重命名、不写入；所有写操作只发生在本地临时盘和 PostgreSQL。
- PostgreSQL 同时保存任务状态、重试历史和命中结果，并用 `file_tasks` + `SKIP LOCKED` 承担任务队列。
- 规则以关键字、正则、数值门限为主，规则集版本化，任务固定版本；v1 单行优先。
- 通过任务尝试历史、心跳、指标、失败原因和重试策略保证任务可观测、可重试、可排障。
- Worker 支持 Web 可视化管理（状态、版本、当前任务、资源、drain/resume）；升级统一由 Jenkins + Docker Compose 执行，Web 不引入部署逻辑；升级期间任务不丢失。

这套方案的重点是先把“下发、解析、规则、入库、重试、可观测”闭环跑通，后续再根据真实瓶颈增加多行规则、细粒度分片、暂存层和更完整的可观测性。
