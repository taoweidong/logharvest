# LogHarvest 4+1 架构视图

> 4+1 模型（Kruchten, 1995）：**场景视图**（+1）驱动，**逻辑 / 开发 / 过程 / 物理**四个视图分别面向功能分解、代码组织、运行时行为与部署拓扑。所有图均为 Mermaid，GitHub / GitLab 可直接渲染。

---

## 0. 场景视图（+1）——驱动其他四个视图

六个核心场景，每个场景都可在前端界面直接操作：

```mermaid
flowchart LR
    admin(["管理员"])
    ops(["运维 / operator"])
    viewer(["只读用户 / viewer"])
    jenkins(["Jenkins（CI/CD）"])

    subgraph SYS["LogHarvest"]
        UC1(["登录 / 令牌刷新"])
        UC2(["登记数据源<br/>并验证只读挂载"])
        UC3(["维护规则集（版本化）<br/>与规则跨版本检索"])
        UC4(["创建作业（目录扫描规划）<br/>并监控实时进度"])
        UC5(["浏览/检索命中结果<br/>（游标分页）"])
        UC6(["取消作业 / 重试失败任务"])
        UC7(["Worker 管理<br/>（drain / resume / 按版本排空）"])
        UC8(["查看任务统计与运行概览"])
        UC9(["用户 / 角色 / 菜单管理（RBAC）"])
        UC10(["检索操作审计日志"])
        UC11(["执行系统维护<br/>（僵死回收 / 降级 / 收尾）"])
        UC12(["滚动升级 Worker"])
    end

    admin --> UC1
    admin --> UC2
    admin --> UC3
    admin --> UC4
    admin --> UC5
    admin --> UC6
    admin --> UC9
    ops --> UC1
    ops --> UC4
    ops --> UC6
    ops --> UC7
    ops --> UC8
    ops --> UC10
    ops --> UC11
    viewer --> UC1
    viewer --> UC4
    viewer --> UC5
    viewer --> UC8
    jenkins --> UC12
    UC4 -. include .-> UC2
    UC4 -. include .-> UC3
    UC7 -. include .-> UC12
```

角色与权限的映射（菜单即权限，19 个权限码）：

| 角色 | 能力边界 |
|---|---|
| admin | 全部（权限解析返回通配 `*:*:*`，不依赖授权表） |
| operator | 作业创建/操作、Worker 操作、维护执行、审计查看；无用户/角色/菜单管理与规则集写入 |
| viewer | 全部只读（作业/命中/统计/Worker 状态）；**无**审计（操作日志含权限拓扑） |

---

## 1. 逻辑视图——功能分解与领域模型

领域模型共 13 张表：业务 7 张、RBAC 5 张、审计 1 张。

```mermaid
erDiagram
    DATA_SOURCE ||--o{ JOB : "规划作业"
    RULE_SET ||--o{ JOB : "引用版本"
    JOB ||--o{ FILE_TASK : "拆分文件任务"
    FILE_TASK ||--o{ FILE_TASK_ATTEMPT : "尝试历史"
    JOB ||--o{ MATCH : "命中结果"
    FILE_TASK ||--o{ MATCH : "溯源"
    USER }o--o{ ROLE : "user_roles"
    ROLE }o--o{ MENU : "role_menus"
    MENU |o--o{ MENU : "parent_id 自引用"
    USER |o--o{ OPERATION_LOG : "actor_id（删除置空）"

    DATA_SOURCE {
        int id PK
        string name UK "unique"
        string root_path
        bool read_only "default true"
        bool copy_to_local
        bool enabled
        datetime created_at
    }
    RULE_SET {
        int id PK
        string name "name+version 联合唯一"
        int version
        json config "规则配置"
        string created_by
        datetime created_at
    }
    JOB {
        int id PK
        string name
        int data_source_id FK
        string source_path
        json file_filter
        int rule_set_id FK
        string status "pending/running/…"
        int total_files
        bigint total_bytes
        bool cancel_requested
        string idempotency_key "created_by+key 唯一"
        datetime created_at
        datetime finished_at
    }
    FILE_TASK {
        int id PK
        int job_id FK
        string source_path "job_id+source_path 唯一"
        bigint size_bytes
        datetime mtime
        string status "pending/running/…"
        int attempts "租约代数"
        int max_attempts "默认 5"
        datetime scheduled_at
        datetime heartbeat_at
        int processed_lines
        int matched_rows
        string error
    }
    FILE_TASK_ATTEMPT {
        int file_task_id PK "复合主键"
        int attempt_no PK "复合主键"
        string worker_id
        string worker_version
        string status
        string error
    }
    MATCH {
        bigint id PK
        int job_id
        int file_task_id
        string rule_id
        int rule_set_id
        int rule_version
        string member_path
        string file_name
        int line_no
        string raw_line "截断标记 raw_truncated"
        json fields
        decimal numeric_value
        string severity
        json tags
        datetime created_at
    }
    WORKER {
        string id PK "默认 hostname"
        string version
        string status "online/busy/draining/offline"
        bool drain_requested
        int current_task_id
        float cpu_percent
        bigint memory_bytes
        datetime last_heartbeat
        string last_error
    }
    USER {
        int id PK
        string username UK
        string password_hash "bcrypt"
        string nickname
        bool is_active
        datetime created_at
    }
    ROLE {
        int id PK
        string code UK
        string name
        bool is_system
        bool is_active
    }
    MENU {
        int id PK
        int parent_id FK "自引用"
        string name UK "权限码"
        int menu_type "0目录/1页面/2权限"
        string path
        string component
        int rank
        bool is_system
        bool is_active
    }
    OPERATION_LOG {
        bigint id PK
        int actor_id FK "SET NULL"
        string actor_name "写入时快照"
        string role_hint "写入时快照"
        string action
        string resource_type
        string resource_id
        json detail "口令永不入库"
        string origin_ip
        datetime created_at
    }
```

领域规则（与持久化同层声明）：

- `file_tasks` 上 `(job_id, source_path)` 唯一——同作业重复扫描天然幂等。
- `jobs` 上 `(created_by, idempotency_key)` 唯一——幂等键由唯一约束兜底。
- `rule_sets` 上 `(name, version)` 唯一——规则集只增版本不覆盖。
- `matches` 只保留三条索引（作业翻页 / 规则时间窗 / 任务追溯），千万级命中下每条索引都是实打实的写放大。

---

## 2. 开发视图——代码组织

```mermaid
flowchart TB
    subgraph SRC["service/src"]
        subgraph APIPKG["api"]
            apiDeps["dependencies.py"]
            apiRoutes["v1/routes_*.py ×13"]
            apiHelpers["v1/helpers.py"]
        end
        subgraph APPPKG["application"]
            appSchemas["schemas.py（接口 DTO）"]
            appServices["services/*.py ×10"]
            appProc["worker/processor.py"]
        end
        subgraph DOMPKG["domain"]
            domModels["models/ ×5 模块"]
            domRules["rules/{models,evaluator}.py"]
            domArchive["archive/reader.py"]
            domServices["services/{tokens,passwords,rate_limit}.py"]
            domGuard["source_guard.py"]
            domPerms["permissions.py"]
            domText["text.py"]
        end
        subgraph INFRA["infrastructure"]
            infraDB["database.py"]
            infraQueue["task_queue.py"]
            infraWriter["match_writer.py"]
            infraMigr["migrations.py"]
        end
        subgraph WKPKG["worker"]
            wkMain["main.py"]
        end
        subgraph CFG["config"]
            cfgSettings["settings.py"]
        end
    end

    subgraph STESTS["service/tests"]
        tApi["api/ ×15（接口契约）"]
        tApp["application/"]
        tDomain["domain/"]
        tWorker["worker/"]
        tInfra["infrastructure/"]
        tInteg["integration/（真实 PG，19 skipped）"]
    end

    subgraph WSRC["web/src"]
        wApi["api/（客户端 + 类型）"]
        wViews["views/{logharvest,rules,system}/"]
        wUtils["utils/{http,sse,auth,tree}/"]
        wStore["store/modules/user.ts"]
        wRouter["router/（动态路由注入）"]
    end

    subgraph WTESTS["web/tests（102 条 vitest 用例）"]
        wTests["utils / rules / api / system"]
    end

    APIPKG -->|"import"| APPPKG
    APPPKG --> DOMPKG
    WKPKG --> APPPKG
    APIPKG --> DOMPKG
    APPPKG --> INFRA
    WKPKG --> INFRA
    INFRA --> DOMPKG
    tApi -.-> APIPKG
    tApp -.-> APPPKG
    tDomain -.-> DOMPKG
    tWorker -.-> WKPKG
    tInfra -.-> INFRA
    tInteg -.-> INFRA
    WTESTS -.-> wUtils
    WTESTS -.-> wViews
```

工程纪律：

- 后端 `ruff`（E/F/W/I/B/UP/SIM/PERF/RUF/BLE）+ `ruff format` 全绿；`mypy` 覆盖 domain/worker/application.worker/config。
- 前端 `vue-tsc --noEmit` 门禁；纯逻辑一律抽 `.ts` 模块（`format.ts`、`rule-form.ts`、`pendingRequests.ts`、`sse.ts`），这是 102 条单测的前提。
- `agent.md` 强制：任何代码变更提交前必须前后端测试全绿。

---

## 3. 过程视图——运行时行为与并发

### 3.1 Worker 领取循环（单进程视角）

```mermaid
flowchart TD
    S(["start"]) --> REG["注册 Worker（ON CONFLICT 更新心跳/版本）"]
    REG --> CLN["清理孤儿本地复制目录（>1h）"]
    CLN --> COMP["启动补偿：recover_stale + 降级死 Worker + 作业终态扫描"]
    COMP --> MAINLOOP{"停止信号未置位?"}
    MAINLOOP -->|"否"| OFF["上报 offline，优雅退出"]
    OFF --> E(["stop"])
    MAINLOOP -->|"是"| DRAIN{"drain_requested?"}
    DRAIN -->|"是"| DR["上报 draining 心跳，不领取新任务"]
    DR --> MAINLOOP
    DRAIN -->|"否"| PERIOD{"now >= 下次周期维护?"}
    PERIOD -->|"是"| SWEEP["recover_stale（回收心跳>120s 的 running）<br/>mark_dead_workers + finalize_finished_jobs"]
    PERIOD -->|"否"| CLAIM
    SWEEP --> CLAIM["CLAIM_SQL：FOR UPDATE SKIP LOCKED<br/>领取 scheduled_at<=now 的 pending，attempts+1<br/>写 file_task_attempts，回读规划 size/mtime"]
    CLAIM -->|"无任务"| WAIT["等待 poll_seconds（默认 1s）"]
    WAIT --> MAINLOOP
    CLAIM -->|"领到"| GUARD["只读守卫 + 路径逃逸校验 + 源 stat 比对"]
    GUARD --> RETRY{"attempts > 1?"}
    RETRY -->|"是"| CLEAN["清上次残留 matches（租约栅栏内）"]
    RETRY -->|"否"| COPY
    CLEAN --> COPY["复制到本地临时盘（1MiB 分块，≥5s 一次 touch 续租）"]
    COPY --> SCAN["流式解压（ArchiveBudget 四维预算）"]
    SCAN --> ROWS{"还有行?"}
    ROWS -->|"是"| LINE["成员过滤（file_glob）→ 逐行 evaluate_line<br/>命中缓冲，每 batch_size 条批量写 matches<br/>每 20000 行 / 5s 心跳续租 + 回写进度"]
    LINE --> CANCEL{"cancel_requested?"}
    CANCEL -->|"是"| CCANCELLED["file_task 写 cancelled（当次尝试关闭）"]
    CANCEL -->|"否"| ROWS
    CCANCELLED --> COMPLETE
    ROWS -->|"否"| SUM["按成员结果汇总 succeeded / partial / failed"]
    SUM --> COMPLETE["COMPLETE_SQL 终态写回（id+attempts+status 栅栏）"]
    COMPLETE --> FAILEDQ{"failed 且未达 max_attempts 且 retryable?"}
    FAILEDQ -->|"是"| BACKOFF["退回 pending，scheduled_at += attempts×5s 线性退避"]
    FAILEDQ -->|"否"| TERM["永久失败 / 成功 / 取消"]
    BACKOFF --> FIN
    TERM --> FIN["尝试汇总作业终态"]
    FIN --> RM["删除本地复制目录"]
    RM --> MAINLOOP
```

### 3.2 多 Worker 并发（多进程视角）

```mermaid
sequenceDiagram
    autonumber
    participant W1 as Worker-1
    participant W2 as Worker-2
    participant W3 as Worker-3
    participant W4 as Worker-4
    participant Q as file_tasks 队列（PG）

    W1->>Q: SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1
    W2->>Q: SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1
    W3->>Q: SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1
    W4->>Q: SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1
    Note over Q: 已被锁定的行被其他事务直接跳过：<br/>四个 Worker 各拿各的任务，无争用、无重复
    Q-->>W1: task #101（attempts 0→1）
    Q-->>W2: task #102
    Q-->>W3: task #103
    Q-->>W4: task #104
```

并发要点：

- 单进程一次只处理一个任务；单机并行 = 多进程（默认 4，最多 6）。
- **租约栅栏**：所有写路径（matches 批量写、清场、终态）都必须先 `touch`（`id + attempts + status='running'`）成功；丢租约立即 `LeaseLost` 中止且不写终态——僵死回收后旧 Worker 的迟到写入全部失效。
- 同机多进程必须唯一 `LOGHARVEST_WORKER_ID`（容器化下取容器 hostname 天然唯一）。

### 3.3 前端令牌刷新（无感续期）

```mermaid
sequenceDiagram
    autonumber
    participant Caller as 业务代码
    participant ITC as 请求拦截器
    participant Q as pendingRequests 队列
    participant Store as user store
    participant Srv as Server

    Caller->>ITC: request（token 已过期）
    ITC->>ITC: expires <= now?
    ITC->>Q: enqueuePendingRequest(config)
    ITC->>Store: handRefreshToken(refreshToken)
    alt 刷新成功
        Store->>Srv: POST /api/refresh-token
        Srv-->>Store: 新 accessToken / refreshToken
        Store-->>ITC: res
        ITC->>Q: flushPendingRequests(token)
        Q-->>ITC: 逐个补 Bearer 后 resolve
        ITC-->>Caller: 原请求继续发出
    else 刷新失败
        Store->>Srv: POST /api/refresh-token
        Srv-->>Store: 401
        ITC->>Q: rejectPendingRequests(err)
        Q-->>Caller: 排队请求逐个 reject（绝不永久挂起）
        ITC->>Store: logOut() + 提示
    end
```

---

## 4. 物理视图——部署拓扑

```mermaid
flowchart TB
    subgraph HOST["宿主机（Linux + Docker）"]
        subgraph DBSTACK["栈 logharvest-db（deploy/postgres）"]
            PGC["postgres 容器（PG16）<br/>logharvest-postgres"]
            PGDATA[("/usr1/logharvest/postgres/data<br/>PGDATA bind mount")]
            BACKUP[("/usr1/logharvest/postgres/backup<br/>pg_dump 归档，含 Jenkins 升级前备份")]
        end

        subgraph APPSTACK["栈 logharvest-app（deploy/server）"]
            WEB["web 容器（nginx:1.27）:80<br/>前端静态资源 dist/<br/>nginx.conf：/api 反代、SSE 禁缓冲"]
            SERVER["server 容器（uvicorn :8000）<br/>FastAPI 应用（启动即迁移+种子）"]
            SLOG[("/usr1/logharvest/server/logs<br/>日志归档位")]
        end

        subgraph WKSTACK["栈 logharvest-worker（deploy/worker，可 --scale 到 6）"]
            WORKER["worker 容器 ×N<br/>python -m src.worker"]
            TMP[("/usr1/logharvest/worker/tmp<br/>解压临时盘，任务结束自动清理")]
        end

        NET["logharvest-net（外部网络）"]
    end

    NAS[("企业 NAS<br/>/mnt/nas/applogs<br/>只读挂载 → 容器 /nas:ro")]
    JENKINS["Jenkins<br/>Jenkinsfile：门禁→镜像→备份→三栈发布"]

    WEB -->|"/api/**（HTTP/1.1，代理）"| SERVER
    PGC --- NET
    SERVER --- NET
    WORKER --- NET
    SERVER -->|"SQLAlchemy（同步会话），经外部网络容器名直连"| PGC
    WORKER -->|"SKIP LOCKED / 批量写"| PGC
    NAS -.只读复制.-> WORKER
    TMP -.挂载为 LOGHARVEST_WORK_DIR.-> WORKER
    PGDATA --- PGC
    BACKUP --- PGC
    SLOG --- SERVER
    JENKINS -.->|"发布 0-2（引导/备份/--wait）"| DBSTACK
    JENKINS -.->|"发布 3（直换）"| APPSTACK
    JENKINS -.->|"发布 4（滚动换新+扩容）"| WKSTACK
```

三个栈通过外部网络 `logharvest-net` 互通、互相独立升级；数据库栈以固定容器名 `logharvest-postgres` 提供 DNS。所有可归档数据（PG 数据、备份、Worker 临时盘、日志归档位）统一规划在数据盘 `/usr1/logharvest/`，由 `deploy/init-dirs.sh` 幂等创建。

物理节点的容量与网络要求：

| 节点 | 要求 |
|---|---|
| NAS | 只读导出（NFS/SMB）；LogHarvest 对它零写入 |
| Worker 宿主机 | 本地临时盘 ≥ 20 × 最大压缩包解压量；`/proc/self/mounts` 可读（Linux）以使只读守卫真实生效 |
| PostgreSQL | 命中量千万级：`matches` 按月分区与保留期为后续演进项（设计 §6.6） |
| 网络处理 | 浏览器 → Nginx(80) → server(8000)；SSE 需禁 `proxy_buffering` |

---

## 5. 视图一致性检查表

| 场景 | 逻辑 | 开发 | 过程 | 物理 |
|---|---|---|---|---|
| 创建作业 | Job/FileTask 建模 | `services/jobs.py` + `routes_jobs.py` | §core-logic 规划时序 | server → PG |
| Worker 消费 | FileTask 状态机 + 尝试历史 | `worker/main.py` + `processor.py` | §3.1/§3.2 活动/并发 | worker → NAS/tmp/PG |
| 实时进度 | —（读聚合） | `routes_events.py` + `utils/sse.ts` | SSE 分帧与降级轮询 | Nginx 禁缓冲 |
| 权限控制 | Menu/Role/User 建模 | `permissions.py` 单一来源 | 登录/刷新时序 | server → PG |
| 滚动升级 | Worker.drain_requested | `routes_workers.py` | Jenkins 流水线阶段 | 双版本并存 → 旧清零 |
