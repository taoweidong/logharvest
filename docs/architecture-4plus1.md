# LogHarvest 4+1 架构视图

> 4+1 模型（Kruchten, 1995）：**场景视图**（+1）驱动，**逻辑 / 开发 / 过程 / 物理**四个视图分别面向功能分解、代码组织、运行时行为与部署拓扑。所有图均为 PlantUML。

---

## 0. 场景视图（+1）——驱动其他四个视图

六个核心场景，每个场景都可在前端界面直接操作：

```plantuml
@startuml
left to right direction
skinparam packageStyle rectangle
title 场景视图（用例图）

actor 管理员 as admin
actor "运维 / operator" as ops
actor "只读用户 / viewer" as viewer
actor "Jenkins（CI/CD）" as jenkins

rectangle LogHarvest {
  usecase "登录 / 令牌刷新" as UC1
  usecase "登记数据源\n并验证只读挂载" as UC2
  usecase "维护规则集（版本化）\n与规则跨版本检索" as UC3
  usecase "创建作业（目录扫描规划）\n并监控实时进度" as UC4
  usecase "浏览/检索命中结果\n（游标分页）" as UC5
  usecase "取消作业 / 重试失败任务" as UC6
  usecase "Worker 管理\n（drain / resume / 按版本排空）" as UC7
  usecase "查看任务统计与运行概览" as UC8
  usecase "用户 / 角色 / 菜单管理\n（RBAC）" as UC9
  usecase "检索操作审计日志" as UC10
  usecase "执行系统维护\n（僵死回收 / 降级 / 收尾）" as UC11
  usecase "滚动升级 Worker" as UC12
}

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
UC4 ..> UC2 : <<include>>
UC4 ..> UC3 : <<include>>
UC7 ..> UC12 : <<include>>
@enduml
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

```plantuml
@startuml
skinparam classAttributeIconSize 0
title 逻辑视图——领域模型（类图）

class DataSource {
  +id
  +name : unique
  +root_path
  +read_only = true
  +copy_to_local
  +enabled
}

class RuleSet {
  +id
  +name
  +version
  +config : JSON
  +created_by
}

class Job {
  +id
  +name
  +data_source_id >> FK
  +source_path
  +file_filter : JSON
  +rule_set_id >> FK
  +status : pending/running/…
  +total_files / total_bytes
  +cancel_requested
  +idempotency_key : nullable
}

class FileTask {
  +id
  +job_id >> FK
  +source_path
  +size_bytes / mtime
  +status : pending/…
  +attempts / max_attempts = 5
  +scheduled_at / heartbeat_at
  +processed_lines / matched_rows
  +error
}

class FileTaskAttempt {
  +file_task_id >> PK/FK
  +attempt_no >> PK
  +worker_id / worker_version
  +status / error
}

class Match {
  +id : BigInteger
  +job_id / file_task_id
  +rule_id / rule_set_id / rule_version
  +member_path / file_name / line_no
  +raw_line / raw_truncated
  +fields : JSON / numeric_value
  +severity / tags : JSON
}

class Worker {
  +id : str（hostname 默认）
  +version
  +status : online/busy/…
  +drain_requested
  +cpu_percent / memory_bytes
  +last_heartbeat / last_error
}

class User {
  +id
  +username : unique
  +password_hash : bcrypt
  +is_active
}

class Role {
  +id
  +code : unique
  +is_system
  +is_active
}

class Menu {
  +id
  +parent_id >> FK(自引用)
  +name : unique（权限码）
  +menu_type : 0目录/1页面/2权限
  +path / component / rank
  +is_system / is_active
}

class OperationLog {
  +id : BigInteger
  +actor_id >> FK(users) SET NULL
  +actor_name / role_hint（快照）
  +action / resource_type / resource_id
  +detail : JSONB / origin_ip
}

DataSource "1" -- "0..*" Job
RuleSet "1" -- "0..*" Job
Job "1" *-- "0..*" FileTask
FileTask "1" *-- "0..*" FileTaskAttempt
Job "1" -- "0..*" Match
FileTask "1" -- "0..*" Match
User "0..*" -- "0..*" Role : user_roles
Role "0..*" -- "0..*" Menu : role_menus
Menu "0..1" o-- "0..*" Menu : parent
User "1" -- "0..*" OperationLog : actor_id\n（删除置空）
@enduml
```

领域规则（与持久化同层声明）：

- `file_tasks` 上 `(job_id, source_path)` 唯一——同作业重复扫描天然幂等。
- `jobs` 上 `(created_by, idempotency_key)` 唯一——幂等键由唯一约束兜底。
- `rule_sets` 上 `(name, version)` 唯一——规则集只增版本不覆盖。
- `matches` 只保留三条索引（作业翻页 / 规则时间窗 / 任务追溯），千万级命中下每条索引都是实打实的写放大。

---

## 2. 开发视图——代码组织

```plantuml
@startuml
skinparam packageStyle folder
title 开发视图——模块与测试映射

package "service/src" {
  package "api" {
    [dependencies.py]
    [v1/routes_*.py ×13]
    [v1/helpers.py]
  }
  package "application" {
    [schemas.py]
    [services/*.py ×10]
    [worker/processor.py]
  }
  package "domain" {
    [models/ ×5 模块]
    [rules/{models,evaluator}.py]
    [archive/reader.py]
    [services/{tokens,passwords,rate_limit}.py]
    [source_guard.py]
    [permissions.py]
    [text.py]
  }
  package "infrastructure" {
    [database.py]
    [task_queue.py]
    [match_writer.py]
    [migrations.py]
  }
  package "worker" {
    [main.py]
  }
  package "config" {
    [settings.py]
  }
}

package "service/tests" {
  [api/ ×15] 
  [application/]
  [domain/]
  [worker/]
  [infrastructure/]
  [integration/（真实 PG，19 skipped）]
}

package "web/src" {
  [api/（客户端 + 类型）]
  [views/{logharvest,rules,system}/]
  [utils/{http,sse,auth,tree}/]
  [store/modules/user.ts]
  [router/（动态路由注入）]
}

package "web/tests" {
  [77 条 vitest 用例]
}

[api] ..> [application] : import
[application] ..> [domain] : import
[worker] ..> [application]
[api] ..> [domain]
[api/ ×15] ..> [api] : 接口契约
[application/] ..> [application]
[domain/] ..> [domain]
[integration/（真实 PG，19 skipped）] ..> [infrastructure]
[77 条 vitest 用例] ..> [utils/{http,sse,auth,tree}/]
[77 条 vitest 用例] ..> [views/{logharvest,rules,system}/]
@enduml
```

工程纪律：

- 后端 `ruff`（E/F/W/I/B/UP/SIM/PERF/RUF/BLE）+ `ruff format` 全绿；`mypy` 覆盖 domain/worker/application.worker/config。
- 前端 `vue-tsc --noEmit` 门禁；纯逻辑一律抽 `.ts` 模块（`format.ts`、`rule-form.ts`、`pendingRequests.ts`、`sse.ts`），这是 77 条单测的前提。
- `agent.md` 强制：任何代码变更提交前必须前后端测试全绿。

---

## 3. 过程视图——运行时行为与并发

### 3.1 Worker 领取循环（单进程视角）

```plantuml
@startuml
title 过程视图——Worker 常驻循环（活动图）

start
:注册 Worker（ON CONFLICT 更新心跳/版本）;
:清理孤儿本地复制目录（>1h）;
:启动补偿：recover_stale + 降级死 Worker + 作业终态扫描;

while (停止信号未置位?) is (是)
  if (drain_requested?) then (是)
    :上报 draining 心跳，不领取新任务;
  else (否)
    if (now >= 下次周期维护) then (是)
      :recover_stale（回收心跳>120s 的 running 任务）;
      :mark_dead_workers + finalize_finished_jobs;
    endif
    :CLAIM_SQL：FOR UPDATE SKIP LOCKED
    领取 scheduled_at<=now 的 pending，attempts+1，
    写 file_task_attempts，回读规划 size/mtime;
    if (无任务) then (是)
      :等待 poll_seconds（默认 1s）;
    else (否)
      :只读守卫 + 路径逃逸校验 + 源 stat 比对;
      if (attempts > 1?) then (是)
        :清上次残留 matches（租约栅栏内）;
      endif
      :复制到本地临时盘（1MiB 分块，≥5s 一次 touch 续租）;
      :流式解压（ArchiveBudget 四维预算）;
      while (还有行?) is (是)
        :成员过滤（file_glob）→ 逐行 evaluate_line;
        :命中缓冲；每 batch_size 条批量写 matches;
        :每 20000 行 / 5s 心跳续租 + 回写进度;
        :每行检查 cancel_requested;
      endwhile (否)
      :按成员结果汇总 succeeded/partial/failed;
      :COMPLETE_SQL 终态写回（id+attempts+status 栅栏）;
      if (failed 且未达 max_attempts 且 retryable?) then (是)
        :退回 pending，scheduled_at += attempts*5s 线性退避;
      else (否)
        :永久失败 / 成功 / 取消;
      endif
      :尝试汇总作业终态;
      :删除本地复制目录;
    endif
  endif
endwhile (否)
:上报 offline，优雅退出;
stop
@enduml
```

### 3.2 多 Worker 并发（多进程视角）

```plantuml
@startuml
title 过程视图——并发模型（同一队列，N 个进程互不冲突）

database "file_tasks 队列" as q
worker "Worker-1" as w1
worker "Worker-2" as w2
worker "Worker-3" as w3
worker "Worker-4" as w4

w1 -> q : SELECT ... FOR UPDATE SKIP LOCKED\nLIMIT 1
w2 -> q : SELECT ... FOR UPDATE SKIP LOCKED\nLIMIT 1
w3 -> q : SELECT ... FOR UPDATE SKIP LOCKED\nLIMIT 1
w4 -> q : SELECT ... FOR UPDATE SKIP LOCKED\nLIMIT 1
note right of q
  已被锁定的行被其他事务直接跳过：
  四个 Worker 各拿各的任务，无争用、无重复
end note
q --> w1 : task #101（attempts 0→1）
q --> w2 : task #102
q --> w3 : task #103
q --> w4 : task #104
@enduml
```

并发要点：

- 单进程一次只处理一个任务；单机并行 = 多进程（默认 4，最多 6）。
- **租约栅栏**：所有写路径（matches 批量写、清场、终态）都必须先 `touch`（`id + attempts + status='running'`）成功；丢租约立即 `LeaseLost` 中止且不写终态——僵死回收后旧 Worker 的迟到写入全部失效。
- 同机多进程必须唯一 `LOGHARVEST_WORKER_ID`（容器化下取容器 hostname 天然唯一）。

### 3.3 前端令牌刷新（无感续期）

```plantuml
@startuml
title 过程视图——axios 令牌刷新队列（时序图）

participant "业务代码" as caller
participant "请求拦截器" as itc
participant "pendingRequests\n队列" as q
participant "user store" as store
participant "Server" as srv

caller -> itc : request(Authorization 需要刷新)
itc -> itc : expires <= now?
itc -> q : enqueuePendingRequest(config)
itc -> store : handRefreshToken(refreshToken)
alt 刷新成功
  store -> srv : POST /api/refresh-token
  srv --> store : 新 accessToken/refreshToken
  store --> itc : res
  itc -> q : flushPendingRequests(token)
  q --> itc : 逐个补 Bearer 后 resolve
  itc --> caller : 原请求继续发出
else 刷新失败
  store -> srv : POST /api/refresh-token
  srv --> store : 401
  itc -> q : rejectPendingRequests(err)
  q --> caller : 排队请求逐个 reject（绝不永久挂起）
  itc -> store : logOut() + 提示
end
@enduml
```

---

## 4. 物理视图——部署拓扑

```plantuml
@startuml
title 物理视图——三个独立 compose 栈部署（deployment diagram，数据归档 /usr1）

node "宿主机（Linux + Docker）" {

  node "栈 logharvest-db\n（deploy/postgres）" as dbstack {
    node "postgres 容器（PG16）\nlogharvest-postgres" as pgc
    folder "/usr1/logharvest/postgres/data\n（PGDATA bind mount）" as pgdata
    folder "/usr1/logharvest/postgres/backup\n（pg_dump 归档，含 Jenkins 升级前备份）" as backup
  }

  node "栈 logharvest-app\n（deploy/server）" as appstack {
    node "web 容器（nginx:1.27）:80" as web {
      artifact "前端静态资源 dist/" as dist
      artifact "nginx.conf（/api 反代、SSE 禁缓冲）" as nconf
    }
    node "server 容器（uvicorn :8000）" as server {
      artifact "FastAPI 应用（启动即迁移+种子）" as app
    }
    folder "/usr1/logharvest/server/logs\n（日志归档位）" as slog
  }

  node "栈 logharvest-worker\n（deploy/worker，可 --scale 到 6）" as workerstack {
    node "worker 容器 ×N" as worker {
      artifact "python -m src.worker" as wproc
      folder "/usr1/logharvest/worker/tmp\n（解压临时盘，任务结束自动清理）" as tmp
    }
  }

  network "logharvest-net（外部网络）" as net
}

node "企业 NAS" as nas {
  folder "/mnt/nas/applogs\n（只读挂载 → 容器 /nas:ro）" as logs
}

node "Jenkins" as jenkins {
  artifact "Jenkinsfile\n门禁→镜像→备份→三栈发布" as jf
}

web --> server : /api/**（HTTP/1.1，代理）
net <-right- pgc
net <-right- server
net <-right- worker
server --> pgc : SQLAlchemy（同步会话）\n经外部网络容器名直连
wproc --> pgc : SKIP LOCKED / 批量写
logs ..> worker : 只读复制
tmp ..> worker : 挂载为 LOGHARVEST_WORK_DIR
pgdata --- pgc
backup --- pgc
jenkins ..> dbstack : 发布 0-2（引导/备份/--wait）
jenkins ..> appstack : 发布 3（直换）
jenkins ..> workerstack : 发布 4（滚动换新+扩容）
@enduml
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
