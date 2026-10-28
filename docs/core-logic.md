# LogHarvest 核心逻辑说明

> 逐条解释系统的八块核心逻辑：**为什么这样设计、边界在哪、失败时发生什么**。配套代码位置均可直接点击跳转。所有图均为 PlantUML。

## 目录

1. [作业创建与规划（含幂等）](#1-作业创建与规划)
2. [任务队列与租约栅栏](#2-任务队列与租约栅栏)
3. [Worker 执行与容错（成员级 partial）](#3-worker-执行与容错)
4. [解压预算（压缩炸弹防护）](#4-解压预算)
5. [规则引擎](#5-规则引擎)
6. [作业终态汇总与取消/重试语义](#6-作业终态汇总与取消重试语义)
7. [SSE 实时进度](#7-sse-实时进度)
8. [认证、RBAC 与审计](#8-认证rbac-与审计)

---

## 1. 作业创建与规划

代码：`application/services/jobs.py`（服务）+ `api/v1/routes_jobs.py`（HTTP 映射 + 审计）。

```plantuml
@startuml
title POST /api/jobs 创建作业（活动图）

start
:读取 Idempotency-Key 请求头;
if (键存在且非空?) then (是)
  :查 (created_by, idempotency_key) 已有作业;
  if (命中?) then (是)
    if (name/source_path/rule_set_id/file_filter\n与请求体一致?) then (是)
      :返回已有作业（200，不重复审计）;
      stop
    else (否)
      :409 幂等键冲突;
      stop
    endif
  else (未命中)
  endif
elseif (键为空白串?) then (是)
  :400 Idempotency-Key must not be blank;
  stop
else (无键)
endif

:校验数据源（存在且 enabled）与规则集（404）;
:source_path 必须 ⊆ 数据源 root_path（400 路径逃逸）
且必须是已存在目录（400）;
:只读守卫 guard_source(enforce)——判可写即 409（规格 §7.2）;
note right
  扫描前先 session.rollback() 结束读事务，
  避免长扫描期间占用连接池
end note

:同步递归扫描（10s deadline，max_files=20000 触顶即停，
  max_depth 静默截断，跳过符号链接与点目录）;
if (扫描超时?) then (是)
  :503 缩小目录或过滤器;
  stop
elseif (文件数超限?) then (是)
  :400 too many files;
  stop
else (正常)
endif
:每个压缩文件 stat（size/mtime 快照，供 Worker 核对）;

:写 jobs（空目录直接 succeeded；有文件则 running）
+ 每压缩包一条 file_tasks（pending）——同一事务;
if (IntegrityError（并发同键）?) then (是)
  :回查幂等键——命中则返回已有作业，未命中 re-raise;
else (否)
  :201 返回新作业 + 审计留痕（同事务收尾）;
endif
stop
@enduml
```

设计要点：

- **幂等三态**：同键同体 → `200` 回放；同键不同体 → `409`；无键 → 正常创建。并发插入靠 `jobs(created_by, idempotency_key)` 唯一约束兜底，冲突后回查。
- **`file_filter.suffixes` 求交**：显式传入的后缀集合先与压缩包后缀集合（zip/tar 系/gz/bz2/xz）求交，交集为空（例如只传 `.log`）返回 `400`——原始文本不会混进压缩任务。
- **规划快照**：`size_bytes`/`mtime` 在规划时落库；Worker 执行前核对，不一致判永久失败（重试只会同样失败）。

---

## 2. 任务队列与租约栅栏

代码：`infrastructure/task_queue.py`（全部 PostgreSQL 专有 SQL）。

### 2.1 file_tasks 状态机

```plantuml
@startuml
title file_tasks 状态机（attempts 栅栏贯穿全部迁移）

[*] --> pending : 作业规划写入

pending --> running : CLAIM_SQL\n(Skip Locked, attempts+1)
running --> running : touch/heartbeat\n(id+attempts+status 栅栏)
running --> succeeded : COMPLETE_SQL(成功)
running --> partial : COMPLETE_SQL(成员级部分失败)
running --> failed : COMPLETE_SQL(失败且\nattempts>=max_attempts 或 !retryable)
running --> cancelled : JobCancelled / COMPLETE_SQL\n(作业已请求取消)
running --> pending : COMPLETE_SQL(失败可重试)\nscheduled_at += attempts*5s
running --> pending : recover_stale\n(心跳 >120s 的僵死任务回收)
pending --> cancelled : POST /jobs/{id}/cancel

failed --> pending : POST /jobs/{id}/files/retry-failed\n(attempts 不重置，仍受 max_attempts 约束)
succeeded --> [*]
partial --> [*]
failed --> [*]
cancelled --> [*]
@enduml
```

### 2.2 领取与租约栅栏（时序）

```plantuml
@startuml
title 领取、心跳续租与迟到写入失效（时序图）

participant "Worker-A" as wa
participant "Worker-B" as wb
database "PostgreSQL" as pg

wa -> pg : BEGIN; SELECT ... FOR UPDATE SKIP LOCKED\n(task#7, attempts 0→1) RETURNING
wa -> pg : COMMIT（写 file_task_attempts#1）
wa -> pg : touch(task#7, attempts=1) → true\n（复制开始，此后每 5s 续租）

... Worker-A 进程僵死（心跳停止）...

note over pg
  120 秒后：recover_stale
  把 task#7 置回 pending（attempts=1）
end note
wb -> pg : CLAIM task#7（attempts 1→2）
wb -> pg : touch(task#7, attempts=2) → true

wa -> pg : 迟到的批量写 matches\n（先 touch(task#7, attempts=1)）
pg --> wa : 0 行更新 → LeaseLost
note right of wa
  栅栏失败：不写 matches、不写终态，
  中止当次尝试——旧 Worker 永远
  覆盖不了新 Owner 的结果
end note
@enduml
```

要点：

- **`attempts` 是租约代数**：所有写路径（清场、批量写、终态）前置 `touch`（`id + attempts + status='running'` 三元栅栏）；回收/接管后代数变化，旧代写入全部失效。
- **线性退避**：可重试失败回 `pending` 时 `scheduled_at = now() + attempts × 5s`；`CLAIM_SQL` 只领取 `scheduled_at <= now()` 的任务。
- **回收兜底**：Worker 启动时 + 每 300 秒 + 服务端 `POST /api/system/maintenance` 都会执行 `recover_stale`，覆盖"进程不在了没人清场"的场景。

---

## 3. Worker 执行与容错

代码：`application/worker/processor.py`（单任务处理器）+ `worker/main.py`（领取循环）。

### 3.1 单任务处理

```plantuml
@startuml
title TaskProcessor.process（活动图）

start
:guard_source（只读守卫，嵌套挂载各自判定）;
:_check_root——真实路径必须仍在数据源根下\n（防规划后被替换成外指符号链接）;
:_check_source——stat 比对 size/mtime;
if (不一致 / 不可用?) then (是)
  :PermanentTaskFailure（retryable=False）\n不烧租约不复制，人工确认后手动重试;
  stop
else (否)
endif
:_verify_lease（栅栏）;
if (attempts > 1?) then (是)
  :delete_for_task 清上次残留命中;
else (否)
endif
:mkdir 隔离目录 + 复制到本地（1MiB 分块，≥5s touch）;
if (续租返回假?) then (是)
  :LeaseLost——中止，不写终态;
  stop
else (否)
endif
:scan_archive 流式解压（预算见 §4）;
while (下一行?) is (有)
  :成员切换时算一次 file_glob 匹配（缓存后 ~60ns/行）;
  if (成员命中且 cancel_requested?) then (是)
    :JobCancelled——file_task 置 cancelled;
    stop
  else (否)
  endif
  :evaluate_line 求值;
  if (有命中?) then (是)
    :raw_line 截断（64KiB + truncated 标记）;
    :入批量缓冲（batch_size=1000）;
    if (缓冲满?) then (是)
      :touch 栅栏 → 批量 INSERT matches;
    else (否)
    endif
  else (否)
  endif
  :每 20000 行或 5s：heartbeat 续租 + 回写进度;
endwhile (无)
:按匹配成员结果汇总 succeeded/partial/failed;
:COMPLETE_SQL 终态写回（同栅栏）;
:删除本地复制目录;
stop
@enduml
```

### 3.2 成员级容错（为什么会有 partial）

| 场景 | 行为 |
|---|---|
| zip 单成员损坏 | 记录 `ArchiveMemberError` 后**继续读剩余成员**；匹配成员至少一个完整读完 → `partial` |
| 流式 tar 成员损坏 | 无法 seek 越过 → 该处停止（`scan_incomplete`）；之前已读的成员有效 → `partial` |
| tar 缺零结束块 | tarfile 会把截断当 EOF，扫描结束额外校验零块，缺失标记 `scan_incomplete` 防漏读 |
| 被排除成员损坏且不影响读取 | 不计入状态（zip 可跳过）；流式 tar 停止点之后可能还有匹配成员，因此始终计入 |
| 所有匹配成员失败 | `failed`；全部以 `retryable=False` 写终态，**不自动重试**（重试同样失败，只烧 max_attempts） |

错误摘要写入 `file_tasks.error`，每次尝试独立记录在 `file_task_attempts`（尝试历史抽屉可查）。

---

## 4. 解压预算

代码：`domain/archive/reader.py` 的 `ArchiveBudget` + `_BudgetTracker`。

```plantuml
@startuml
title ArchiveBudget 四维防护（活动图，单位均为"解压后"字节）

start
:tracker.begin_member(member);
if (成员数 > max_members(10000)?) then (是)
  :ScanLimitError(reason="max_members")\n→ 整包停止;
  stop
else (否)
endif
while (读到下一行?) is (有)
  :累计成员字节与整包字节;
  if (单成员 > max_member_bytes(1GiB)?) then (是)
    :该成员记错误（只牺牲它），其余成员照常 → partial;
  else (否)
  endif
  if (整包累计 > max_total_bytes(4GiB)?) then (是)
    :整包停止扫描（failed，不重试）;
    stop
  else (否)
  endif
  if (整包 ≥ 64MiB 且 累计/源大小 > max_ratio(500)?) then (是)
    :膨胀比超限 → 整包停止;
    stop
  else (否)
  endif
endwhile (无)
stop
@enduml
```

动机：zip 声明的 `file_size` 可任意伪造，**只有实际解压出来的字节算数**——预算在流式读取路径上实时计数，这是唯一能在"读到一半"发现炸弹的位置。低于 64 MiB 不参与膨胀比判定，避免误伤正常小文件。

---

## 5. 规则引擎

代码：`domain/rules/models.py`（配置模型）+ `domain/rules/evaluator.py`（编译与求值）。

```plantuml
@startuml
title 规则求值管线（每行、每规则）

start
:RuleSetConfig.model_validate（pydantic 判别联合）;
:compile_ruleset;
note right
  正则规则在编译期完成：
  - regex.compile(pattern, flags)
  - flags 仅允许 i/m/s
  - threshold.field 必须是命名分组
  编译失败的规则集在创建 API 即 422
end note

while (还有规则?) is (有)
  if (规则 disabled?) then (是)
    :跳过;
  else (否)
  endif
  if (match.type == keyword?) then (是)
    :any：任一关键字命中（大小写按 case_sensitive）
    all：全部命中;
    if (未命中?) then (是)
      :continue;
    else (否)
      :命中 → fields 留空，severity/tags 生效;
    endif
  else (regex)
    :item.regex.search(line, timeout=timeout_ms/1000);
    if (TimeoutError?) then (是)
      :告警日志 + 跳过该规则（该行漏命中，
      已登记为已知限制 P1-7，见核心链路评估）;
    else (否)
      :提取命名分组 → fields;
      if (threshold 存在?) then (是)
        :Decimal(fields[field]) 与 threshold.value
        按 op（gt/gte/lt/lte/eq）比较;
        note right
          非有限值 / 不可解析 / 比较异常
          一律按"未命中"处理，绝不抛出
        end note
      else (否)
      endif
      if (阈值不满足?) then (是)
        :未命中;
      else (否)
        :命中（记录 numeric_value）;
      endif
    endif
  endif
endwhile (无)
:返回命中的 RuleMatch 列表（含 rule_id/version/severity/tags/fields）;
stop
@enduml
```

规则配置形状（`RuleSetConfig`）：

```jsonc
{
  "name": "生产错误捕获",
  "file_glob": "**/*.log",            // gitwildmatch，成员级过滤
  "encoding": "utf-8",                // 首选编码
  "fallback_encoding": "gb18030",     // 严格解码失败后的回退
  "rules": [
    { "id": "err-500", "match": { "type": "keyword", "any": ["FATAL", "ERROR"],
      "all": [], "case_sensitive": false } },
    { "id": "cpu-high", "match": { "type": "regex", "pattern": "cpu=(?P<cpu>\\d+)",
      "flags": "i", "timeout_ms": 50 },
      "threshold": { "field": "cpu", "op": "gt", "value": "90" } }
  ]
}
```

`keyword.any` 与 `keyword.all` 互斥；正则阈值依赖命名分组提取；字符串形式的 `value` 避免 Decimal 精度丢失。

---

## 6. 作业终态汇总与取消/重试语义

- **由谁汇总**：完成最后一个任务的 Worker 通过 `finalize_job` 收尾；每 300 秒与服务端维护入口做周期兜底（覆盖无完成回调的路径）。
- **汇总规则**：全部任务 `succeeded` → `succeeded`；混有 `failed/partial` → `failed`/`partial`；`cancel_requested` 为真 → `cancelled`；存在 `pending/running` → 继续 `running`。
- **取消**：`POST /jobs/{id}/cancel` 把 `pending` 置 `cancelled` + `cancel_requested=true`；无 running 任务立即终态，否则等 Worker 在行循环里感知（每行一次查询）。终态作业的取消是幂等空操作。
- **手动重试**：`retry-failed` 只搬 `failed` 行（单次上限 200，`remaining_failed` 可见）、`attempts` 不重置（防死循环刷库）、已取消作业直接 `409`（`CLAIM_SQL` 永不再领取，重新入队只会永久滞留）。
- **Worker drain**：`drain_requested=true` 后只心跳不领取；`drain-version` 按版本批量打标（离线 Worker 只记标记不改状态），是 Jenkins 滚动升级的基础原语。

---

## 7. SSE 实时进度

代码：`api/v1/routes_events.py`（服务端）+ `web/src/utils/sse.ts`（客户端）。

```plantuml
@startuml
title SSE 作业进度（时序图）

actor 浏览器 as b
participant "FastAPI\n/routes_events" as s
database "PostgreSQL" as pg

b -> s : GET /api/jobs/{id}/events\n(Authorization: Bearer; max_events 0–100)
s -> s : 权限校验（require_permission_streaming，\n短会话校验后立即归还连接）
s -> pg : 首次快照校验（作业不存在 → 404）
s --> b : 200 text/event-stream\n(Cache-Control: no-cache; X-Accel-Buffering: no)

loop 每秒（直到终态/作业消失/达到 max_events）
  s -> pg : 每轮独立短会话快照\n(jobs.status + file_tasks 按 status 聚合)
  s --> b : event: progress\ndata: {"job_id":1,"status":"running",\n"total":100,"by_status":{...},"processed_lines":...}\n\n
end

alt 作业进入终态
  s --> b : 最后一个快照后结束流
else 作业被删除 / 代理切断
  s --> b : 流提前结束
end
note over b
  前端 fetch + getReader 手工分帧
  （EventSource 无法自定义 Authorization 头）；
  返回 undefined = 未观察到终态即结束
  → 必须回退 5 秒轮询，不能当成"已完成"
end note
@enduml
```

Nginx 反代必须 `proxy_buffering off` + 长 `proxy_read_timeout`（见 `web/nginx.conf`），否则帧会被缓冲到流结束一次性下发，前端退化为轮询。

---

## 8. 认证、RBAC 与审计

### 8.1 登录与权限解析

```plantuml
@startuml
title 登录与请求鉴权（时序图）

actor 用户 as u
participant "POST /api/login" as login
database "PostgreSQL" as pg
participant "业务请求\nrequire_permission(code)" as dep

u -> login : username + password
login -> login : 限流检查（用户名+IP，滑动窗口 10 次/300s）\n超限 → 429 + Retry-After
login -> pg : SELECT user WHERE username
note right
  未知用户也跑一次 bcrypt（预置占位哈希）：
  消除 ~450ms vs ~5ms 的用户名枚举时序侧信道
end note
alt 口令不符
  login -> pg : 审计 auth.login_failed（actor_id 空，\n用户名快照 + 来源 IP）
  login --> u : 401 invalid credentials
else 账号停用
  login --> u : 403 user is disabled\n（不计入限流——不对停用账号上反向锁）
else 成功
  login -> login : 清空该来源失败计数
  login -> pg : roles_and_permissions（单次角色查询）
  login -> pg : 审计 auth.login_success（role_hint 快照）
  login --> u : 信封 {accessToken, refreshToken,\nroles, permissions, expires}
end

u -> dep : 业务请求 + Bearer
dep -> pg : decode access JWT（type=access）
dep -> pg : SELECT user（不存在或停用 → 401）
dep -> pg : permission_codes（admin → *:*:*；\n否则查 active 角色关联的 active 权限节点）
alt 无该权限码
  dep --> u : 403 permission '<code>' required
else 通过
  dep --> u : 进入路由处理
end
@enduml
```

RBAC 三条设计纪律：

1. **权限码单一来源** `domain/permissions.py`：路由声明、启动种子（3 目录/13 页面/19 权限节点）、前端按钮权限共用一份常量；`test_every_declared_permission_code_is_seeded` 钉住"声明了却没种子"的漂移。
2. **菜单即权限**：`menus.menu_type=2` 的 `name` 就是权限码；角色授权 = role_menus；`GET /api/auth/routes` 把菜单树转成前端动态路由（无可见子页面的目录剔除）。
3. **最后一名 active admin 保护**：停用自己 / 摘除 admin 角色 / 删除 admin 均被拒绝（409/400）。

### 8.2 操作审计

```plantuml
@startuml
title 审计留痕事务口径（时序图）

participant "路由层" as r
participant "audit.py" as a
database "PostgreSQL" as pg

r -> a : record_operation(...)\n（只 session.add，不提交）
alt 与业务写同事务（更新/删除类）
  r -> pg : 业务写 + 审计行 → 同一 commit
  note right
    业务回滚 = 审计一并回滚：
    不留"业务没做成却记了案"的假案
  end note
else 创建类（业务已自提交，新 id 才存在）
  r -> a : commit_operation(session)
  a -> pg : 单独 commit
  note right
    提交失败只告警回滚：
    不把已成功的业务变成 5xx
  end note
else 审计构造/写库本身失败
  a -> a : structlog 告警（只记 action 与异常类型）
  note right
    留痕不完整可以补，
    业务因审计挂掉不可接受
  end note
end
@enduml
```

行内容：`actor_id`（删除账号时外键 SET NULL + 显式置空兜底 SQLite）、`actor_name`/`role_hint` 写入时快照（删号不抹责任链）、`action`（`user.create` / `job.cancel` / `auth.login_failed`…）、`detail`（变更前后摘要，**口令永不入库**）、`origin_ip`。表只追加：无更新接口、无删除接口。

---

## 附：已知边界（有意的取舍，均已文档化）

| 边界 | 说明 |
|---|---|
| 登录限流为进程内计数 | 多副本实际放行量 = 阈值 × 副本数（`rate_limit.py` 文件头） |
| 正则超时静默跳过且不计数 | 该行漏命中仍可能 `succeeded`（核心链路评估 P1-7，M3 议题） |
| `max_matches_per_task` 声明未强制 | 空关键字/空正则可匹配全部行（核心链路 P0-3，修复方案见评估文档） |
| `matches` 未按月分区 | 千万级命中下需按设计 §6.6 演进 |
| 规则索引查询期展平 JSON | 当前版本量级可接受，代价记在主设计 §15 |
