# 技术栈选型评估（LogRule v1）

- 文档状态：待评审
- 日期：2026-10-24
- 关联设计：[三服务精简方案](./2026-10-24-compressed-log-extraction-design.md)
- 范围：只做选型评估，不包含实现代码

---

## 1. 评估背景

### 1.1 业务与规模

- 企业磁盘挂载目录下的压缩日志，单批次 500 GB 以上。
- 用户创建任务时指定 NAS 上的具体日志目录；Server 扫描该目录，把每个压缩文件写成一条任务。
- 单批次压缩文件 1000+ 个，单个文本日志可能超过 10 万行。
- 命中结果最终可能达到千万级。
- 三个应用服务：Web 前端、Web Server、Worker。
- 核心闭环：规则录入 → Server 扫描 NAS 目录并生成任务 → Worker 领取、复制/解压/匹配 → 结果入库 → Web 展示；Worker 完成一个任务后继续领取下一个。

### 1.2 团队与运维假设

- Python 团队，优先使用 Python 生态。
- 小团队快速交付 MVP，不希望维护 Kafka、Elasticsearch、ClickHouse 等重型中间件。
- 已有 PostgreSQL 运维能力；允许 Docker Compose 部署。
- 已有 `Hello-FastApi` 基座：Vue 3 + TypeScript + Element Plus、FastAPI + SQLModel + DDD、PostgreSQL、Redis、JWT/RBAC、Alembic 和 pytest。
- 先保证正确性和可运维性，再追求极限吞吐。

### 1.3 硬约束

1. 规则必须可配置、可版本化，不能改代码发版。
2. 命中结果必须能追溯到压缩包、包内文件、物理行号和规则版本。
3. 单 Worker 不能把压缩包整体读入内存。
4. 任务失败可重试，重试不能产生重复结果；任务状态、尝试历史和失败原因必须可观测。
5. Web 服务不能被解析任务拖慢。
6. 系统不能依赖大量自研基础设施；规则引擎和任务编排可以自研轻量实现。

### 1.4 已确认决策

| 决策 | 结论 |
|---|---|
| 前端 | 接受 Vue 3 + Element Plus，参考 `Hello-FastApi` 扩展 |
| 任务队列 | 接受 PostgreSQL 任务表作为队列，但必须提供可观测、重试、幂等和僵死回收 |
| 多行堆栈 | v1 单行优先，多行放 M3 |
| 数据源 | 企业磁盘挂载，任务创建时指定具体 NAS 目录；Server 和 Worker 都可访问，默认复制到本地临时盘解析 |
| 挂载范围 | Server 和 Worker 都挂载同一 NAS；Server 扫描并生成任务，Worker 只读复制 |
| 本地临时盘 | 可容纳 20 个最大压缩文件；磁盘不是并发瓶颈 |
| 单机内存 | 最大 16GB；默认 4 个 Worker（最多 6 个），每进程 1.5–2GB 预算；内存压力降低并发 4→3→2 |
| 原始数据 | NAS 只读，不删除、不移动、不重命名、不写入 |
| Worker 管理/升级 | Web 展示状态/版本/当前任务，支持 drain/resume；镜像替换统一由 Jenkins + Docker Compose 执行 |
| 目录扫描 | 默认递归，最大深度 5（已确认） |
| 登录 | 基础管理员账号，不扩展复杂权限 |
| 解释器/服务 | 复用 `Hello-FastApi` 的 Python/FastAPI/SQLModel/DDD 基座 |

---

## 2. 评估方法

### 2.1 评估维度与权重

| 维度 | 权重 | 说明 |
|---|---:|---|
| 规模与性能适配 | 25% | 能否支撑 500 GB、1000+ 文件、千万级结果 |
| 运维复杂度 | 20% | 组件数量、部署、监控、故障恢复 |
| 开发效率 | 20% | 上手速度、代码量、生态和文档 |
| 一致性与可追溯 | 15% | 任务状态、失败重试、结果定位是否可靠 |
| 生态与人才 | 10% | 社区活跃度、招聘和排障资料 |
| 退出/迁移成本 | 10% | 后续替换或扩展的难度 |

评分采用 1–5 分，5 分最好。加权分只用于相对比较，不作为精确结论。

### 2.2 决策原则

1. **先排除不满足硬约束的方案**：例如不能流式解压、不能保证行号、不能重试幂等。
2. **同分时优先组件更少、事实源更单一的方案**：减少故障面。
3. **优先成熟组件，但不为未来规模预埋重型中间件**。
4. **保留退出路径**：核心接口隔离，后续可替换队列、存储或匹配引擎。

---

## 3. 结论摘要

### 3.1 推荐技术栈

| 层 | 推荐 | 版本建议 | 说明 |
|---|---|---|---|
| Web 前端 | Vue 3 + TypeScript + Element Plus + Monaco（Pure Admin） | Vue 3.x / Element Plus 2.x | 复用 Hello-FastApi 前端；表单 + JSON 双模式规则编辑 |
| Web Server | FastAPI + SQLModel + DDD | Python 3.12+ / FastAPI 0.11x | 复用现有 DDD 分层、JWT/RBAC、Alembic |
| Worker | Python 多进程/多容器 + psycopg 3 | Python 3.12+ | 每进程一个任务循环；16GB 主机默认 4 个、最多 6 个；不引入 Celery |
| Worker 管理/升级 | workers 心跳表 + Web drain/resume + Jenkins + Docker Compose | Swarm + Portainer / K3s + Argo CD | 版本分布可见；Web 不执行部署 |
| 任务队列 | PostgreSQL `file_tasks` + `SKIP LOCKED` | PostgreSQL 15+ | 任务量与业务表同库；单一事实源 |
| 数据库 | PostgreSQL | 15+ | 元数据、任务、尝试历史、规则、命中结果 |
| 文件源 | 企业磁盘挂载 | 本地/NFS/CIFS 挂载 | Worker 复制到本地临时盘后解析 |
| 归档解析 | `zipfile` / `tarfile` / `gzip` / `bz2` / `lzma` | 随 Python | 覆盖 v1 格式，流式读取 |
| 规则引擎 | 自研轻量引擎 + `regex` + `ahocorasick_rs` | `regex` 2024+ / `ahocorasick_rs` 0.2x | 关键字、正则、门限、字段提取 |
| 数据访问 | SQLModel（元数据）+ psycopg 3（批量结果） | SQLModel 0.0.x / psycopg 3.x | 复用现有 ORM；批量结果不用 ORM 逐行写 |
| 校验/序列化 | Pydantic v2 + orjson | Pydantic 2.x / orjson 3.x | FastAPI 原生；规则 JSON Schema |
| 日志/指标 | `structlog` + `prometheus-client` | 最新稳定版 | 结构化日志；基础 RED/任务指标 |
| 测试 | pytest + testcontainers + hypothesis | 最新稳定版 | 真实 PostgreSQL + 临时挂载目录 |
| 部署 | Docker Compose + Nginx | Compose v2 | 复用现有基座；增加 Worker 和挂载盘 |

### 3.2 关键结论

1. **任务队列用 PostgreSQL，不引入 Celery/Redis 任务队列**：任务只有 1000+ 条，`SKIP LOCKED` 足够；任务状态、尝试历史和业务数据同库。现有 Redis 继续用于缓存/会话/限流。
2. **不引入 Elasticsearch/ClickHouse**：千万级命中用 PostgreSQL 分区和必要索引可以支撑；检索复杂化后再评估。
3. **规则引擎自研轻量版**：现成通用规则引擎难以同时满足行号追溯、字段提取、门限和性能；自研范围严格限制在四类能力。
4. **前端复用 Hello-FastApi**：Vue 3 + TypeScript + Element Plus + Pure Admin，已有登录、权限、布局和请求封装，新增日志分析页面即可。
5. **后端复用 FastAPI + SQLModel + DDD**：沿用现有分层、认证、迁移和测试体系；读多写少走 SQLModel，高吞吐结果写入走 psycopg/COPY。
6. **数据源用企业磁盘挂载**：不引入 FTP/对象存储；任务创建时指定 NAS 目录，Server 扫描生成任务；Worker 复制到本地临时盘后解析，避免网络挂载抖动。
7. **Worker 可视化管理和升级**：`workers` 表 + 心跳 + 版本 + drain；Web 展示状态和版本分布；镜像替换统一由 Jenkins + Docker Compose 执行，Web 不引入部署逻辑，也不持有 Docker/K8s 高权限。

---

## 4. 分项评估

### 4.1 Web 前端

候选：

| 方案 | 规模/性能 | 运维 | 开发效率 | 一致性 | 生态 | 迁移 | 加权分 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Vue 3 + TypeScript + Element Plus（Hello-FastApi） | 4 | 5 | 5 | 4 | 5 | 4 | **4.50** |
| React + Ant Design | 4 | 5 | 4 | 4 | 5 | 4 | 4.30 |
| Jinja2 + HTMX | 3 | 5 | 4 | 3 | 4 | 3 | 3.70 |

分析：

- 页面以数据源表单、规则编辑器、任务列表/详情、结果表格为主，属于典型中后台应用；Vue 3 + Element Plus 的表格、表单、分页、弹窗组件覆盖度高。
- `Hello-FastApi` 已经提供 Vue 3 + TypeScript + Element Plus + Pure Admin 的布局、登录、权限、请求封装和路由，直接新增日志分析页面即可，复用收益明显。
- 规则编辑需要 JSON 编辑能力，Monaco 可独立集成，和框架无关。
- 任务详情有 1000+ 文件任务，需要分页和筛选；Vue 的响应式状态管理足够，不需要引入复杂状态机。
- React 生态同样成熟，若团队已有 React 经验，切换成本低于重新培养；但本项目的现实选择是复用现有 Vue 基座。
- HTMX 适合服务端渲染的简单页面，但规则编辑器、复杂表格和任务进度交互会越来越重，后期迁移成本高。

结论：**Vue 3 + TypeScript + Element Plus + Monaco，复用 Hello-FastApi 的 Pure Admin 前端**。前端只通过 REST API 访问后端，框架可替换。

### 4.2 Web 框架

候选：

| 方案 | 规模/性能 | 运维 | 开发效率 | 一致性 | 生态 | 迁移 | 加权分 |
|---|---:|---:|---:|---:|---:|---:|---:|
| FastAPI + SQLModel + DDD | 4 | 4 | 5 | 5 | 5 | 5 | **4.55** |
| Flask | 3 | 5 | 5 | 3 | 5 | 4 | 4.10 |
| Django | 3 | 4 | 4 | 5 | 5 | 4 | 4.00 |
| Litestar | 4 | 4 | 3 | 4 | 3 | 4 | 3.70 |

分析：

- FastAPI 与 Pydantic v2、SQLModel 深度集成，规则 JSON 校验、请求校验、OpenAPI 文档可以复用同一套模型；现有基座已经采用 FastAPI + SQLModel + DDD。
- 目录扫描、路径可读性测试是阻塞操作，放在线程池执行；解析任务完全在 Worker，不占 Web 进程。
- Django 自带 Admin、ORM、用户体系，但本项目的前端是独立 SPA，Admin 价值有限；ORM 对大批量结果写入也不合适。
- Flask 更轻，但需要自己拼装校验、文档和异步能力；FastAPI 的默认能力更完整。
- Litestar 性能好，但团队熟悉度和生态弱于 FastAPI，v1 不选。

结论：**FastAPI + SQLModel + DDD**。沿用现有分层、认证、迁移和测试体系；业务逻辑与传输层分离。

### 4.3 数据库

候选：

| 方案 | 规模/性能 | 运维 | 开发效率 | 一致性 | 生态 | 迁移 | 加权分 |
|---|---:|---:|---:|---:|---:|---:|---:|
| PostgreSQL | 5 | 4 | 5 | 5 | 5 | 5 | **4.80** |
| MySQL | 4 | 4 | 4 | 4 | 5 | 4 | 4.10 |
| MongoDB | 4 | 3 | 4 | 3 | 5 | 3 | 3.65 |
| Elasticsearch | 5 | 3 | 3 | 3 | 4 | 3 | 3.60 |
| ClickHouse | 5 | 3 | 3 | 3 | 4 | 2 | 3.50 |

分析：

- 本项目需要事务性的任务状态、规则版本、结果写入和分页查询；PostgreSQL 的强一致性和事务能力最匹配。日志分析模块依赖 `SKIP LOCKED`、JSONB 和分区，开发环境也必须使用 PostgreSQL，不使用现有基座的 SQLite 开发配置。
- 千万级 `matches` 通过按月分区、`(job_id, created_at, id)` 索引、批量写入和游标分页可以支撑；`fields` 使用 JSONB，暂不需要 GIN 索引。
- MySQL 也能完成，但 PostgreSQL 的 JSONB、分区、`SKIP LOCKED`、表达式索引和运维资料更适合本方案。
- Elasticsearch 擅长全文检索，但作为任务状态和结果主库会引入双写、一致性和运维成本；v1 的查询以任务/规则/文件为主，不需要全文检索。
- ClickHouse 适合大规模分析聚合，但事务、任务状态和行级更新不合适；只有未来做跨任务统计分析时才考虑作为旁路。
- MongoDB 的 schema 灵活，但任务状态和关系查询不如 PostgreSQL 直接。

结论：**PostgreSQL 15+ 作为唯一主库**。未来若做复杂全文检索或跨任务分析，再增加 ES/ClickHouse 旁路，不替换主库。

### 4.4 任务队列与下发

候选：

| 方案 | 规模/性能 | 运维 | 开发效率 | 一致性 | 生态 | 迁移 | 加权分 |
|---|---:|---:|---:|---:|---:|---:|---:|
| PostgreSQL `SKIP LOCKED` | 4 | 5 | 3 | 5 | 4 | 4 | **4.15** |
| Celery + Redis | 4 | 3 | 5 | 3 | 5 | 4 | 3.95 |
| Redis Streams | 4 | 3 | 3 | 4 | 4 | 4 | 3.60 |
| RabbitMQ | 5 | 2 | 4 | 3 | 5 | 4 | 3.80 |
| Kafka | 5 | 1 | 2 | 3 | 5 | 3 | 3.10 |

分析：

- 本项目的任务量是“每批 1000+ 条”，不是每秒数万条消息；任务执行时间以分钟计，领取频率很低。
- `file_tasks` 已经在 PostgreSQL 中保存状态，直接用它当队列可以保证任务状态只有一个事实源，不需要“数据库 + broker”双写。
- `SKIP LOCKED` 的领取模式成熟：多个 Worker 并发领取不会重复，任务行锁只在领取瞬间持有。
- 失败重试通过 `status` + `scheduled_at` 实现；僵死任务通过 `heartbeat_at` 回收；取消通过 `cancel_requested` 和任务状态实现。
- 代价是需要自己写一个 Worker 主循环和领取 SQL，代码量约几十行；相比引入 Redis/Celery 的部署、监控、可见性超时和重复投递问题，v1 更可控。
- Celery + Redis 的开发效率最高，适合团队已有 Celery 运维体系、任务类型多、需要定时任务/复杂重试策略的场景。它的主要问题是额外组件、broker 与数据库双状态、Redis `visibility_timeout` 对长任务不友好。
- Redis Streams 比 Celery 轻，但仍需自己实现消费组、ack、pending 回收；没有明显优于 PostgreSQL 队列。
- RabbitMQ 更可靠但运维更重；Kafka 是日志流平台，不是任务队列，v1 不需要。

已确认的队列保障要求：

- **可观测**：`file_tasks` 保存状态、进度、尝试次数、心跳、错误；新增 `file_task_attempts` 保存每次尝试的 Worker、起止时间、处理行数、命中数和错误。
- **可重试**：自动重试 + 退避 + 最大次数；永久错误不自动重试；支持手动重试。
- **幂等**：重试前按 `file_task_id` 删除旧命中；写入时用 `(file_task_id, attempts)` 栅栏；原子领取避免重复执行。
- **可排障**：任务详情展示尝试历史；系统状态展示 pending 数、最老等待时长、失败/重试耗尽数和 Worker 心跳。
- **可告警**：最老 pending 超过 10 分钟、失败率超过 5%、Worker 心跳超过 2 分钟、任务 10 分钟无进度。

领取模式示意：

```sql
UPDATE file_tasks
SET status = 'running',
    worker_id = $1,
    attempts = attempts + 1,
    started_at = COALESCE(started_at, now()),
    heartbeat_at = now()
WHERE id = (
    SELECT id
    FROM file_tasks
    WHERE status = 'pending'
      AND scheduled_at <= now()
    ORDER BY scheduled_at, id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING *;
```

结论：**v1 采用 PostgreSQL `SKIP LOCKED`（已确认）**。必须同时交付 `file_task_attempts` 尝试历史、心跳回收、自动/手动重试、幂等清理和系统状态页。如果团队未来已有 Celery/Redis 运维体系，可把“领取层”替换为 Celery，业务表和规则引擎不变。触发切换的条件：任务领取频率持续超过约 50–100 次/秒、需要多队列/复杂路由、需要定时任务体系，或团队明确要求统一消息平台。

### 4.5 文件源访问（企业磁盘挂载）

候选：

| 方案 | 规模/性能 | 运维 | 开发效率 | 一致性 | 生态 | 迁移 | 加权分 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 挂载盘 + 复制到本地 | 5 | 5 | 5 | 5 | 5 | 4 | **4.90** |
| 直接读取挂载盘 | 5 | 4 | 5 | 4 | 5 | 4 | 4.55 |
| 对象存储 S3/MinIO | 4 | 3 | 3 | 4 | 4 | 4 | 3.60 |
| FTP/FTPS 适配器（后续） | 3 | 2 | 3 | 3 | 4 | 3 | 2.90 |

分析：

- 企业磁盘挂载（本地盘、NFS、CIFS）对 Server 和 Worker 都是普通文件路径；任务创建时用户指定具体 NAS 目录，Server 扫描该目录生成文件任务，Worker 读取/复制文件。
- **复制到本地临时盘**：Worker 先把压缩包复制到本地，再解析本地副本；网络挂载抖动不会直接影响解析，源文件变化由复制前后的大小/修改时间校验发现。代价是多一次读写和本地磁盘占用。
- 已确认本地临时盘可容纳 20 个最大压缩文件，磁盘容量不是瓶颈；单机内存上限 16GB，默认运行 4 个 Worker（最多 6 个），每进程 1.5–2GB 预算，容器设置 2GB 内存上限。
- 复制/解析并发取 `min(20, 内存允许的进程数)`，不要按 20 个磁盘槽位启动 20 个进程；内存压力时优先降低并发（4→3→2），直读 NAS 不作为内存兜底；OOM 被系统杀死后由心跳超时回收任务并重试。
- **直接读取挂载盘**：少一次复制，适合挂载盘本身是本地盘或延迟很低的场景；但网络挂载抖动会直接拖慢解析，且文件在读取过程中变化会影响结果。
- 无论哪种方式，**NAS 原始数据只读**：只允许扫描、`stat`、读取和复制，不允许写入、删除、移动、重命名或修改属性；挂载建议使用 `ro,noatime`，避免读取更新访问时间。
- 对象存储需要额外的上传/下载流程和权限体系；v1 没有该基础设施。
- FTP/FTPS 是历史方案；当前数据已经在企业磁盘挂载上，不再需要 FTP 客户端、连接数控制和凭据管理。未来如有外部 FTP 源，可作为独立适配器加入。
- 无论哪种方式，都必须做路径规范化、符号链接校验和只读权限控制。

结论：**企业磁盘挂载 + 默认复制到本地临时盘**。数据源只读；临时盘可容纳 20 个最大文件，但并发由 16GB 内存限制为 4–6 个 Worker；内存压力时降低并发，直读不作为内存兜底；Worker 负责复制、解析和清理。

### 4.6 压缩包解析

候选：

| 方案 | 规模/性能 | 运维 | 开发效率 | 一致性 | 生态 | 迁移 | 加权分 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Python 标准库 | 4 | 5 | 5 | 4 | 5 | 4 | **4.50** |
| libarchive 绑定 | 5 | 4 | 3 | 4 | 4 | 4 | 4.05 |
| py7zr / rarfile | 4 | 4 | 3 | 4 | 4 | 3 | 3.70 |

分析：

- v1 需要 zip、tar、tar.gz、tar.bz2、tar.xz、单文件 gz/bz2/xz；标准库全部覆盖，且解压核心由 C 实现。
- `zipfile.ZipFile.open()` 和 `tarfile.open(mode="r|*")` 支持流式读取，不会把整包解压到磁盘。
- 标准库不支持 rar/7z；如果业务确实有这些格式，再引入 `rarfile`/`py7zr` 或 libarchive，不影响主流程。
- libarchive 格式覆盖最全，但 Python 绑定、编译依赖和错误处理更复杂；v1 不选。

结论：**Python 标准库**。通过格式识别和成员遍历接口隔离，未来可替换为 libarchive。

### 4.7 规则引擎与匹配

候选：

| 方案 | 规模/性能 | 运维 | 开发效率 | 一致性 | 生态 | 迁移 | 加权分 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 自研轻量引擎 + regex + AC | 5 | 4 | 4 | 5 | 4 | 4 | **4.40** |
| JSON Logic | 3 | 4 | 3 | 3 | 4 | 4 | 3.40 |
| Hyperscan / Vectorscan | 5 | 3 | 2 | 3 | 3 | 3 | 3.30 |
| 通用 rule-engine 库 | 3 | 4 | 3 | 3 | 3 | 3 | 3.20 |
| durable_rules | 3 | 4 | 2 | 2 | 2 | 2 | 2.65 |

分析：

- 需求不是通用专家系统，而是“逐行匹配 + 字段提取 + 数值门限 + 结果定位”。通用规则引擎（Rete/JSON Logic）难以直接提供物理行号、压缩包成员路径、规则版本和批量输出。
- 自研范围严格限制在四类能力：关键字、正则、门限、输出；规则 JSON 用 Pydantic 校验；正则用 `regex` 超时；关键词用 `ahocorasick_rs` 做预过滤。
- Hyperscan/Vectorscan 吞吐更高，但 Python 绑定、编译部署和模式兼容性复杂；先自研 + AC，若压测发现正则匹配是瓶颈，再把底层匹配替换为 Vectorscan，不改变规则 JSON。
- JSON Logic 适合表达式组合，但性能、字段提取和行号追溯不满足要求。
- durable_rules 等项目活跃度和维护风险较高，不适合作为核心依赖。

结论：**自研轻量引擎**。规则引擎作为独立 Python 包，Server 试跑和 Worker 正式执行复用同一套编译/执行代码。

### 4.8 数据访问与批量写入

候选：

| 方案 | 规模/性能 | 运维 | 开发效率 | 一致性 | 生态 | 迁移 | 加权分 |
|---|---:|---:|---:|---:|---:|---:|---:|
| SQLModel（元数据）+ psycopg 3（批量结果） | 5 | 5 | 5 | 4 | 5 | 5 | **4.85** |
| SQLAlchemy Core + psycopg 3 | 5 | 5 | 4 | 4 | 5 | 5 | 4.65 |
| 纯 psycopg 3 | 4 | 5 | 5 | 4 | 4 | 4 | 4.40 |
| asyncpg | 5 | 4 | 4 | 4 | 4 | 3 | 4.15 |
| SQLAlchemy ORM | 4 | 4 | 4 | 4 | 5 | 4 | 4.10 |

分析：

- 现有 `Hello-FastApi` 使用 SQLModel，元数据 CRUD（数据源、规则、任务）继续用 SQLModel，保持 DDD 分层和现有仓储风格。
- 高吞吐 `matches` 写入用 psycopg 3 的 `executemany`/`COPY`，避免 ORM 逐行 flush；SQLModel 本身基于 SQLAlchemy，需要时可直接下探 Core 写复杂查询。
- psycopg 3 同时支持同步和异步，Worker 用同步连接池，Server 用异步连接池；一套驱动即可。
- asyncpg 性能好，但生态和类型系统与 psycopg 不同，团队需要维护两套数据库栈；v1 不选。
- 纯 psycopg 3 最直接，但元数据查询会散落 SQL；复用 SQLModel 更符合现有基座。

结论：**SQLModel（元数据 CRUD）+ psycopg 3（批量结果/复杂查询）**；结果批量写入优先 `COPY`，命中稀疏时 `executemany`。

### 4.9 校验与序列化

候选：

| 方案 | 规模/性能 | 运维 | 开发效率 | 一致性 | 生态 | 迁移 | 加权分 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Pydantic v2 + orjson | 5 | 5 | 5 | 4 | 5 | 4 | **4.75** |
| msgspec | 4 | 5 | 4 | 4 | 4 | 3 | 4.10 |
| marshmallow | 3 | 5 | 4 | 4 | 4 | 4 | 3.95 |

分析：

- FastAPI 原生集成 Pydantic v2，请求/响应校验和 OpenAPI 文档不需要额外适配。
- 规则文档需要 JSON Schema 给 Monaco 做补全，Pydantic 可直接生成。
- msgspec 性能更好，但与 FastAPI 的集成和生态弱于 Pydantic；v1 的 API 不是性能瓶颈。
- orjson 用于 API 响应和结构化日志，性能好且支持 datetime/Decimal 等类型。

结论：**Pydantic v2 + orjson**。

### 4.10 可观测性

候选：

| 方案 | 规模/性能 | 运维 | 开发效率 | 一致性 | 生态 | 迁移 | 加权分 |
|---|---:|---:|---:|---:|---:|---:|---:|
| structlog + prometheus-client | 4 | 4 | 5 | 5 | 5 | 4 | **4.45** |
| 标准库 logging + 自研指标 | 4 | 3 | 4 | 5 | 5 | 4 | 4.05 |
| 完整 OTel + Tempo + Grafana | 5 | 2 | 3 | 5 | 5 | 5 | 4.00 |

分析：

- v1 的核心可观测性是：任务/文件任务状态、进度、尝试历史、失败原因、Worker 心跳、待处理任务积压、结构化日志和基础指标。
- `structlog` 输出 JSON 日志，字段包含 `request_id`、`job_id`、`file_task_id`、`attempt_no`、`worker_id`；`prometheus-client` 暴露任务时长、处理字节/行数、命中数、错误数、待处理任务数和最老 pending 等待时间。
- `file_task_attempts` 保存每次重试的 Worker、起止时间、处理行数、命中数和错误，支持 UI 直接查看重试历史。
- 完整 OTel 链路在 500 GB 高吞吐场景下采样和存储成本高；v1 先把指标和日志做扎实，M3 再接入 OTel。
- 不引入 Grafana/Tempo 作为 v1 硬依赖；Prometheus 端点可选，先用 `/api/system/status` 和日志排障。

结论：**structlog + prometheus-client**；OTel 作为后续扩展。

### 4.11 测试

候选：

| 方案 | 规模/性能 | 运维 | 开发效率 | 一致性 | 生态 | 迁移 | 加权分 |
|---|---:|---:|---:|---:|---:|---:|---:|
| pytest + testcontainers + hypothesis | 5 | 5 | 5 | 5 | 5 | 5 | **5.00** |
| unittest | 3 | 4 | 3 | 5 | 5 | 4 | 3.80 |
| Robot Framework | 3 | 4 | 3 | 4 | 4 | 3 | 3.30 |

分析：

- 复用 `Hello-FastApi` 已有的 pytest 体系和测试组织方式。
- `testcontainers` 启动真实 PostgreSQL，验证分区、`SKIP LOCKED`、尝试历史、批量写入和事务行为。
- 挂载目录测试用临时目录 + 固定样本压缩包，覆盖文件缺失、大小/修改时间变化、权限不足和复制中断；不再需要 FTP 模拟器。
- `hypothesis` 验证规则匹配、门限比较和行号切分的性质（例如任意分块读取不改变结果）。
- pytest 生态和插件最完整；不需要引入 BDD 框架。

结论：**pytest + testcontainers + hypothesis + 临时挂载目录**。

### 4.12 部署

候选：

| 方案 | 规模/性能 | 运维 | 开发效率 | 一致性 | 生态 | 迁移 | 加权分 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Docker Compose | 4 | 5 | 5 | 5 | 5 | 5 | **4.75** |
| Kubernetes | 3 | 2 | 4 | 5 | 5 | 5 | 3.70 |

分析：

- 复用现有 `Hello-FastApi` 的 Docker Compose（PostgreSQL、Redis、Server、Nginx 前端），新增 Worker 服务和挂载盘 volume。
- Worker 通过增加容器副本横向扩容；Server 可 2 个副本；Nginx 托管前端。
- 企业磁盘挂载同时挂载到 Server 和 Worker；Worker 额外挂载本地临时盘。
- Worker 升级采用“启动新版本 → Web drain 旧版本 → 等待 running 清零 → 停止旧容器”；K8s 使用 rolling update + preStop drain。Web 服务不直接持有 Docker/K8s 高权限。
- Kubernetes 的滚动升级、弹性伸缩和资源隔离更强，但运维成本高；等 Worker 规模和多环境管理成为瓶颈再迁移。
- 镜像和配置保持 12-Factor，迁移 K8s 时不需要改业务代码。

结论：**Docker Compose + Nginx（复用现有基座）**；K8s 作为后续部署选项。

### 4.13 认证与权限

候选：

| 方案 | 规模/性能 | 运维 | 开发效率 | 一致性 | 生态 | 迁移 | 加权分 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 复用现有 JWT 双令牌 + RBAC（v1 只用管理员） | 4 | 5 | 5 | 5 | 5 | 4 | **4.65** |
| 单管理员环境变量 + JWT | 4 | 5 | 5 | 3 | 4 | 4 | 4.25 |
| FastAPI Users | 4 | 4 | 4 | 5 | 5 | 4 | 4.25 |
| OIDC/SSO（Authlib） | 4 | 3 | 4 | 5 | 5 | 4 | 4.05 |

分析：

- `Hello-FastApi` 已经具备 JWT 双令牌、用户、角色、菜单和数据权限；直接复用，v1 只创建管理员角色，避免重复实现登录和用户体系。
- 单管理员 + 环境变量最简单，但会绕开现有用户/会话/登录日志，长期不利于审计和扩展。
- FastAPI Users 提供注册、登录、角色和 OAuth，但现有基座已经覆盖，不需要重复引入。
- OIDC/SSO 是企业上线的长期方向；v1 预留接入点，生产前再切换。

结论：**复用现有 JWT 双令牌 + RBAC，v1 只使用管理员角色**；需要多角色时直接扩展现有 RBAC；生产接入 OIDC。

### 4.14 Worker 生命周期与升级

候选：

| 方案 | 规模/性能 | 运维 | 开发效率 | 一致性 | 生态 | 迁移 | 加权分 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Jenkins + Docker Compose + drain | 4 | 5 | 5 | 5 | 5 | 5 | **4.75** |
| Docker Swarm + Portainer | 5 | 4 | 4 | 5 | 4 | 4 | 4.45 |
| Kubernetes rolling update | 5 | 3 | 4 | 5 | 5 | 5 | 4.40 |
| systemd/supervisor + 手工脚本 | 3 | 4 | 4 | 4 | 4 | 3 | 3.65 |
| Web 直接调用 Docker/K8s API | 3 | 2 | 5 | 3 | 3 | 2 | 3.10 |

分析：

- v1 已有 Jenkins CI/CD 和 Docker Compose；升级采用“Jenkins 构建新镜像 → 启动新版本 Worker → 调用 Web drain-version → 等待 running 清零 → 停止旧容器”。Web 不执行部署，也不接触 Docker/K8s 凭据。
- Worker 通过 `workers` 表注册版本、心跳、当前任务、资源和 drain 状态；Web 提供 Worker 列表、版本分布、drain/resume。升级期间新旧版本可并存。
- 任务不丢：drain 让旧 Worker 完成当前任务；被强杀时依靠 `heartbeat_at` 超时回收和 `file_task_attempts` 重试；`worker_version` 记录版本，便于排查兼容问题。
- Docker Swarm + Portainer 提供原生滚动更新和可视化容器管理；如果后续要求可视化部署，可作为独立运维平台引入，不把管理逻辑塞进业务 Web。Swarm 增加一套编排运维，v1 不默认引入。
- systemd/supervisor 手工脚本适合单机，但版本分布、优雅停机、回滚和审计能力弱。
- Kubernetes + Argo CD/Rancher 的 rolling update、preStop、就绪探针最成熟，但运维复杂度最高；等 Worker 规模、多环境发布和弹性伸缩成为瓶颈再迁移。
- Web 直接调用 Docker/K8s API 最“可视化”，但等于把容器控制面权限暴露给 Web，安全风险高；不采用。
- Watchtower 等自动更新工具无法感知任务 drain，可能中断正在解析的大文件任务，不采用。

结论：**v1 统一采用 Jenkins + Docker Compose + drain**；Web 负责 Worker 状态、版本分布和 drain/resume，不执行部署。后续需要原生滚动/可视化部署时，采用 Docker Swarm + Portainer 或 K3s + Argo CD，不自研部署逻辑。升级流程和兼容性要求见主设计 7.5。

---

## 5. 加权评分汇总

| 层 | 推荐方案 | 加权分 | 次选方案 | 加权分 | 结论 |
|---|---|---:|---|---:|---|
| 前端 | Vue 3 + TypeScript + Element Plus | 4.50 | React + Ant Design | 4.30 | 复用 Hello-FastApi；团队强 React 则 React |
| Web 框架 | FastAPI + SQLModel + DDD | 4.55 | Flask | 4.10 | 复用现有基座；校验、文档、异步能力完整 |
| 数据库 | PostgreSQL | 4.80 | MySQL | 4.10 | 事务、JSONB、分区、SKIP LOCKED |
| 任务队列 | PostgreSQL `SKIP LOCKED` | 4.15 | Celery + Redis | 3.95 | 1000+ 任务量级，少一个中间件 |
| 文件源 | 挂载盘 + 复制到本地 | 4.90 | 直接读取挂载盘 | 4.55 | 企业磁盘挂载；复制隔离网络挂载抖动 |
| 归档解析 | Python 标准库 | 4.50 | libarchive | 4.05 | 覆盖 v1 格式，流式读取 |
| 规则引擎 | 自研轻量引擎 | 4.40 | JSON Logic | 3.40 | 行号/字段/门限/性能综合最优 |
| 数据访问 | SQLModel + psycopg 3 | 4.85 | SQLAlchemy Core + psycopg 3 | 4.65 | 元数据复用 ORM，结果批量写 |
| 校验 | Pydantic v2 | 4.75 | msgspec | 4.10 | FastAPI 原生，JSON Schema |
| 可观测性 | structlog + prometheus-client | 4.45 | 完整 OTel | 4.00 | 先做好日志/指标，链路后置 |
| 测试 | pytest 全家桶 | 5.00 | unittest | 3.80 | 生态和真实依赖测试能力最好 |
| 部署 | Docker Compose | 4.75 | Kubernetes | 3.70 | 复用现有基座；增加 Worker 和挂载盘 |
| Worker 管理/升级 | Jenkins + Docker Compose + drain | 4.75 | Docker Swarm + Portainer | 4.45 | v1 复用现有 CI；Web 不执行部署；任务不丢 |
| 认证 | 现有 JWT + RBAC（管理员） | 4.65 | 单管理员 JWT | 4.25 | 复用基座；v1 只用管理员角色 |

说明：评分是相对参考，不是绝对值；团队已有技术栈可以覆盖评分。例如团队已有成熟 React 体系，React 的实际开发效率会高于表中分数。

---

## 6. 关键权衡与拒绝理由

### 6.1 为什么 v1 用 PostgreSQL 队列

- 已确认接受 PostgreSQL 任务表作为队列；任务量只有 1000+ 条/批，`SKIP LOCKED` 足够，领取频率远低于 PostgreSQL 的处理能力。
- Worker 不绑定某个作业；完成一个 `file_task` 后继续领取全局 pending 任务，直到队列为空或收到停止信号。
- 任务状态、尝试历史、进度和命中结果在同一个库，只有一个事实源；不需要“数据库 + broker”双写。
- 可观测和重试通过 `file_tasks`、`file_task_attempts`、`heartbeat_at`、`attempts`、`scheduled_at` 实现；重试前清理旧命中，写入用 `attempts` 栅栏。
- Celery 需要额外维护 Redis 任务队列、Worker 进程模型、可见性超时、任务路由和监控；对当前三服务小系统是额外复杂度。现有 Redis 仍用于缓存/会话/限流。
- 长任务（大压缩包）在 Redis broker 下需要仔细设置 `visibility_timeout`，否则会重复投递；PostgreSQL 队列用 `heartbeat_at` 和 `attempts` 更直接。
- 如果团队未来统一使用 Celery/RabbitMQ，可替换 Worker 领取层，保留 `file_tasks` 作为业务状态。

### 6.2 为什么不用 Elasticsearch / ClickHouse

- v1 的查询是“按任务、规则、文件、时间查命中”，PostgreSQL 分区 + 索引足够。
- ES/ClickHouse 引入第二套存储，需要同步、双写、运维和权限体系；千万级结果并不构成必须使用的理由。
- 如果未来需要全文检索、跨任务聚合分析或命中数据达到数亿级，再把 `matches` 异步同步到 ES/ClickHouse，作为查询旁路，不替换主库。

### 6.3 为什么规则引擎自研

- 现成通用规则引擎解决的是“条件表达式/推理”，不是“压缩包成员 + 物理行号 + 字段提取 + 数值门限 + 批量入库”。
- 自研范围被严格限制在四类能力，规则文档用 Pydantic 校验，正则用超时库，关键词用 AC 预过滤；复杂度可控。
- 规则引擎与 Server 试跑、Worker 执行共用同一套代码，保证“试跑即所得”。
- 如果未来正则匹配成为瓶颈，可把匹配层替换为 Vectorscan/Hyperscan，规则 JSON 和结果模型不变。

### 6.4 为什么前端不选服务端渲染

- 规则编辑需要 JSON 编辑器、实时校验、试跑面板；任务详情需要分页表格、状态刷新、失败筛选；结果页需要多条件查询。
- HTMX 可以实现，但随着交互增加会不断引入前端状态，最终还是要 SPA；不如一开始就用 Vue。
- 前端独立部署也符合三服务边界，前端迭代不影响 Worker。

### 6.5 为什么不引入暂存层

- 暂存（S3/NFS）能提供跨作业复用和共享，但会增加存储、清理、一致性和权限管理。
- v1 已经通过“复制到本地临时盘”隔离网络挂载抖动；失败重新复制即可，先把主流程跑通。
- 如果挂载盘不稳定、任务重试频繁或需要跨作业复用，再在 M3 引入暂存；任务表和解析接口不变。

### 6.6 为什么复用 Hello-FastApi

- 已有 Vue 3 + TypeScript + Element Plus + Pure Admin，前端登录、权限、布局、请求封装和路由现成。
- 已有 FastAPI + SQLModel + DDD 四层、JWT 双令牌、RBAC、Alembic、日志和 pytest 测试体系。
- 已有 PostgreSQL/Redis/Docker Compose 部署基础，新增 Worker 和挂载盘即可。
- 复用的边界：日志分析作为独立限界上下文接入 DDD 分层；规则引擎和任务领取逻辑保持独立，不污染现有系统模块。
- 代价：需要遵循现有编码规范、DTO/响应格式和测试要求；高吞吐结果写入不能硬套 ORM，需使用 psycopg/COPY。

### 6.7 为什么 Web 不直接操作 Docker/K8s

- 可视化管理和“服务升级”是两件事：前者需要状态、版本、drain/resume；后者是镜像替换和流量排空。
- 让 Web 直接调用 Docker/K8s API 需要挂载 Docker socket 或集群高权限凭据，一旦 Web 被攻破，影响整个主机/集群。
- v1 用现有 Jenkins 执行镜像替换，Web 只负责 drain 和版本视图；升级过程可审计、可回滚，安全边界清晰。
- 如果后续要求原生滚动和可视化服务管理，使用 Docker Swarm + Portainer 或 K3s + Argo CD/Rancher；Portainer/Argo CD 作为独立运维工具，不嵌入业务 Web。
- 不自研 deploy-agent；如果确实需要一键升级，优先用成熟编排/部署平台。

---

## 7. 迁移与退出路径

| 当前选择 | 触发条件 | 迁移方向 | 对业务的影响 |
|---|---|---|---|
| PostgreSQL `SKIP LOCKED` 队列 | 领取频率 > 50–100 次/秒；需要多队列/复杂路由；团队统一消息平台 | Celery + Redis / RabbitMQ | 保留 `file_tasks` 作为业务状态，替换 Worker 领取层 |
| PostgreSQL 结果表 | 需要全文检索；跨任务聚合慢；结果数亿级 | Elasticsearch / ClickHouse 旁路 | 主库继续写入，异步同步查询库 |
| 挂载盘 + 复制到本地 | 挂载盘延迟低且稳定；复制成本高于解析成本 | 直接读取挂载盘 | 数据源适配器接口不变 |
| 挂载盘 | 需要外部 FTP 或对象存储接入 | S3/MinIO/FTP 适配器 | 任务和规则模型不变 |
| Python 标准库解压 | 需要 rar/7z 或更高压缩吞吐 | libarchive / py7zr / rarfile | 归档读取接口不变 |
| 自研规则引擎 | 正则匹配成为瓶颈 | Vectorscan/Hyperscan 匹配层 | 规则 JSON、字段和结果模型不变 |
| Vue 前端 | 团队统一 React 或需要复杂可视化 | React + Ant Design | REST API 不变 |
| 现有 JWT/RBAC | 需要统一身份提供商 | OIDC/SSO | 认证中间件替换，业务接口不变 |
| Docker Compose | 多环境、弹性伸缩、滚动升级 | Kubernetes | 镜像和 12-Factor 配置不变 |
| Web drain + Jenkins + Compose | 需要全自动弹性伸缩/自愈/可视化部署 | Docker Swarm + Portainer / K3s + Argo CD | Worker 注册/心跳/版本字段不变 |

核心原则：**把“任务领取、文件源适配、归档读取、规则执行、结果写入”做成可替换接口**，v1 选简单实现，后续按瓶颈替换。

---

## 8. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| PostgreSQL 队列轮询增加数据库负载 | 大量 Worker 空转查询 | 1000+ 任务量级负载很小；领取间隔 1 秒；后续可用 `LISTEN/NOTIFY` |
| 长任务与 Worker 崩溃 | 任务卡在 `running` | `heartbeat_at` + `attempts` 回收；尝试历史；单任务最长运行时间 |
| 挂载盘读延迟/网络抖动 | 复制慢或失败 | 默认复制到本地；失败退避重试；监控复制耗时和错误率 |
| 本地临时盘不足 | 复制失败或并发受限 | 已确认可容纳 20 个最大文件；复制前检查空间；并发由内存限制为 4–6；任务结束清理 |
| 单机内存耗尽/OOM | Worker 被杀、任务失败 | 16GB 主机默认 4 个 Worker（最多 6 个）；每进程 1.5–2GB 预算；容器 2GB 限制；内存压力降低并发 4→3→2；RSS 监控；OOM 后任务重试 |
| 误写/误删 NAS 原始数据 | 数据损坏或合规风险 | NAS 只读挂载 + 应用层 `read_only` + 只读打开；禁止删除/移动/重命名/写入；测试校验 `mtime` 和校验和 |
| 规划后源文件变化 | 解析结果与规划清单不一致 | 记录 `size_bytes`/`mtime`，复制前后校验；变化则失败并提示重跑 |
| 500 GB 输入导致任务长尾 | 整体完成时间被单个大包拖慢 | v1 接受；后续按成员/字节拆分；监控任务时长 |
| 千万级命中写入压力 | 数据库写入变慢 | 批量写入、COPY、月分区、只建必要索引、保留期 |
| 纯正则规则过慢 | Worker 吞吐下降 | 正则超时、规则数上限、关键词预过滤；压测后引入 Vectorscan |
| 规则引擎与试跑不一致 | 线上误报/漏报 | Server 试跑与 Worker 复用同一引擎库；固定规则版本 |
| 管理员权限过大 | 误操作或越权 | 复用现有 RBAC；v1 只给管理员必要权限；操作日志；后续 OIDC |
| 重试/可观测不足 | 任务失败原因难定位、重复数据 | `file_task_attempts`、心跳、尝试次数、失败原因、幂等清理、写入栅栏 |
| Worker 升级中断/版本不兼容 | 任务丢失、重复结果或新旧 Worker 行为不一致 | 增量数据库迁移；先启动新版本；按版本 drain；心跳回收强杀任务；`worker_version` 记录；滚动期间保留旧 Worker 完成当前任务 |
| 全部 Worker 同时排空 | pending 任务无人处理 | 按版本/批次 drain，新版本就绪后再排空旧版本；监控 pending 最老等待 |
| Docker Compose 单机资源有限 | Worker 扩展受单机限制 | Worker 可拆到多台主机；后续迁 K8s |

---

## 9. 推荐版本与依赖清单

以下为 v1 建议版本方向，具体小版本在实现时锁定：

| 类别 | 依赖 |
|---|---|
| 运行时 | Python 3.12+ |
| Web | FastAPI、Uvicorn、SQLModel、Pydantic v2、orjson |
| 数据库 | PostgreSQL 15+、psycopg 3、SQLAlchemy/SQLModel |
| 文件源 | 标准库 `pathlib`、`shutil`、`os.scandir` |
| 归档 | 标准库 `zipfile`、`tarfile`、`gzip`、`bz2`、`lzma` |
| 规则 | `regex`、`ahocorasick_rs` |
| 日志/指标 | `structlog`、`prometheus-client` |
| 前端 | Vue 3、TypeScript、Vite、Element Plus、Pure Admin、Monaco Editor、Axios、Vue Router、Pinia |
| 测试 | pytest、pytest-asyncio、testcontainers、hypothesis |
| 部署/升级 | Jenkins、Docker Compose、Nginx；可选 Portainer（独立运维） |

不建议在 v1 额外引入：Celery 任务队列、Kafka、RabbitMQ、Elasticsearch、ClickHouse、Kubernetes、OTel Collector、Tempo、Grafana（可按团队现有监控体系选配 Prometheus）。现有 Redis 继续用于缓存/会话/限流。

---

## 10. 待确认决策

| 编号 | 决策 | 当前建议 | 备选 |
|---|---|---|---|
| D1 | 任务队列 | PostgreSQL `SKIP LOCKED`（已接受，需保证可观测/重试） | Celery + Redis |
| D2 | 前端框架 | Vue 3 + TypeScript + Element Plus（已接受） | React + Ant Design |
| D3 | 规则引擎 | 自研轻量引擎 | 通用规则引擎 + 自定义扩展 |
| D4 | 多行堆栈 | M3 支持 | v1 必须支持 |
| D5 | 任务目录递归 | 默认递归，最大深度 5（已确认） | 不递归 |
| D6 | 复制策略 | 默认复制到本地（已确认）；磁盘可容纳 20 个最大文件，但并发按 16GB 内存限制为 4–6 个 Worker | 直接读取 NAS |
| D7 | 认证 | 复用现有 JWT/RBAC，v1 只使用管理员角色 | 单管理员环境变量 / OIDC |
| D8 | 部署 | Docker Compose | Kubernetes |
| D9 | Worker 升级 | Web drain/版本视图 + Jenkins + Docker Compose（已确认，Web 不执行部署） | Docker Swarm + Portainer / K3s + Argo CD |
| D10 | 单机资源 | 16GB 内存；临时盘 20×最大压缩包；默认 4 个 Worker（最多 6）；内存压力降低并发 4→3→2（已确认） | 更高内存主机 / 更多主机 |

NAS 原始数据只读、Server/Worker 均可挂载 NAS、本地临时盘可容纳 20 个最大压缩文件、单机内存 16GB，已确认，不再作为待确认项。

D1、D2、D5、D6、D9、D10 已确认；其余按推荐执行。后续按主设计文档的 M0/M1 拆分实施任务清单；选型变化不影响业务表结构和规则 JSON。
