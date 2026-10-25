# LogHarvest 实现进度盘点与代码质量评估报告

- 日期：2026-10-26
- 范围：`docs/superpowers/**` 全部 10 份设计/计划文档 vs `service/`（后端 49 个源文件）+ `web/`（前端 138 个 ts/vue）实际代码
- 基线：后端 `pytest -q` **336 passed / 16 skipped**，`ruff check` 全绿，`mypy` 24 文件无问题；前端 `pnpm typecheck` 通过
- 术语：P0 = 会导致进程崩溃 / 资源耗尽 / 安全边界被突破；P1 = 明确偏离文档或存在硬性能/功能缺口；P2 = 文档债、死配置、整洁度

---

## 一、进度总结

### 1.1 项目核心目标（提炼自主设计 §1、§2）

LogHarvest 是**压缩日志规则提取平台**：面向企业 NAS 上的海量压缩日志（单批 500 GB / 1000+ 个压缩包 / 千万级命中），让管理员在 Web 上配置数据源与规则集、指定本次要分析的 NAS 目录，由 Server 递归扫描拆成文件任务，多 Worker 并发复制 → 流式解压 → 逐行规则匹配 → 结果入库，最后在 Web 上查进度、重试历史、失败原因与命中结果。

三条不可妥协的设计意图：

1. **PostgreSQL 是唯一事实源**——元数据、任务队列（`file_tasks` + `SKIP LOCKED`）、命中结果同库，不引入 Celery/ES/ClickHouse。
2. **NAS 原始数据全程只读**——不删、不移、不重命名、不写入；所有写操作只发生在本地临时盘与数据库。
3. **任务可观测、可重试、可排障**——尝试历史、心跳、退避、僵死回收、幂等栅栏，全部落在数据库表里。

### 1.2 里程碑完成情况

| 里程碑 | 计划文档 | 状态 | 说明 |
|---|---|---|---|
| M0 核心引擎 | `plans/2026-10-24-logharvest-m0-core.md` | ✅ 已完成 | 规则引擎、归档读取、Worker 处理器、7 张表模型 |
| M1a 控制面 API | `plans/2026-10-25-...-m1a-control-plane-api.md` | ✅ 已完成 | 数据源/规则集/作业/文件任务/结果/系统状态 + 幂等键 |
| M1b Worker/队列 | `plans/2026-10-25-...-m1b-worker-queue.md` | ✅ 已完成 | PG 队列、常驻循环、SSE、drain/resume |
| M2a 后端认证/运维 | `plans/2026-10-25-...-m2a-backend-auth-ops.md` | ✅ 已完成 | JWT/RBAC、Alembic、取消/永久失败/手动重试、概览/维护 |
| M2b 前端五屏 | `plans/2026-10-25-...-m2b-frontend-pure-admin.md` | ✅ 已完成 | 登录、概览、作业+SSE、规则集、数据源、结果游标、Worker |
| M2c 安全与可观测 | `plans/2026-10-25-...-m2c-safety-observability.md` | ✅ 已完成 | 只读守卫、尝试历史、资源上报、structlog、前端抽屉与资源列 |
| M3 多行记录 | `specs/2026-10-26-logharvest-m3-multiline-records-design.md` | ⬜ 未开始 | 设计文档已产出（未评审、未入库），代码零实现 |

### 1.3 已完成且**超出计划**的实现

代码里有若干文档没要求、但明显提高正确性的实现，盘点时一并认可：

| 实现 | 位置 | 超出点 |
|---|---|---|
| 正则 flags 白名单校验 | `domain/rules/evaluator.py:44` | 拒绝 `i/m/s` 之外的 flag，避免静默忽略 |
| threshold 字段必须来自命名分组 | `evaluator.py:68` | 编译期拒绝，而不是运行时不命中 |
| threshold 必须配 regex | `domain/rules/models.py:45` | 关键字规则配 threshold 直接 422 |
| 非有限 Decimal 防护 | `evaluator.py:82,91` | `NaN`/`Infinity` 不参与门限比较 |
| zip 内符号链接成员跳过 | `domain/archive/reader.py:187` | 防止压缩包内的 symlink 成员被当普通文件读 |
| tar 结束标记校验 | `reader.py:275-284` | tarfile 把坏头当 EOF，这里显式识别"提前结束" |
| 成员级 `partial` 终态 | `processor.py:131-144` | 主设计只有文件级状态，这是后续补的 |
| 扫描 deadline（10 秒） | `services/jobs.py:27,128` | 防止大目录扫描拖死 API |
| 登录时序均衡 | `api/v1/routes_auth.py:40` | 未知用户也付一次 bcrypt 成本，防用户名枚举 |
| Alembic 迁移 advisory lock | `infrastructure/migrations.py:25` | 多副本并发启动不会撞 duplicate-table |
| 复制期租约续期 | `processor.py:317-330` | 大包复制超时不会被误判为僵死 |
| SSE 不占用请求级连接 | `api/dependencies.py:99` | 专门的 `AdminStreamDep` + 短会话 |

### 1.4 与文档存在偏差 / 尚未实现的部分

| # | 文档要求 | 位置 | 实际状态 |
|---|---|---|---|
| D1 | 压缩炸弹限额（单成员/单包/压缩比/成员数） | 主设计 §11 | ⬜ 完全无代码路径（唯一一条 §11 空白项） |
| D2 | `matches` 按月分区 + `(rule_id, created_at)` 索引 | §6.6 / §8.3 | ⚠️ 无分区；`rule_id` 无索引（`entities.py:119-121`） |
| D3 | 单机 4–6 Worker、内存压力自适应降并发 | §7.2 / §8.2 | ⚠️ 只有部署进程数约定，代码不检查内存 |
| D4 | `max_matches_per_task` 单任务命中上限 | §8 / §11 | ⚠️ `settings.py:25` 有字段，代码零引用 |
| D5 | Prometheus 指标 | §3 选型 / §10.2 | ⚠️ `pyproject.toml:16` 声明依赖，代码零引用 |
| D6 | `data_sources.copy_to_local` 开关 | §6.1 | ⚠️ 列可读写，Worker 无条件复制，开关无效 |
| D7 | `workers.last_error` | §6.7 | ⚠️ 列存在，无任何写入方 |
| D8 | 样本试跑 `POST /api/rule-sets/test` | §5.3 / §9.2 | ⬜ 未实现 |
| D9 | `GET /api/workers/{id}` Worker 详情 | §9.2 | ⬜ 未实现（只有列表） |
| D10 | Monaco / JSON 模式规则编辑 | §5.3 | ⬜ 未实现（前端只做结构化表单，M2b 计划已确认有意取舍） |
| D11 | 结果保留期 + 按分区删除 | §8.3 | ⬜ 未实现 |
| D12 | 告警（最老 pending >10min 等） | §7.4 | ⬜ 未实现（依赖 Prometheus，而 Prometheus 也没接） |
| D13 | 前端 `DataSourceTestResult` 含 `writability` / `read_only_confirmed` | M2c Task 1 | ⚠️ 后端已返回（`schemas.py:42-43`），前端类型与界面均未同步（`logharvestTypes.ts:242-247`、`datasources/index.vue:130-138`） |
| D14 | 前端注释与实现一致 | — | ⚠️ 三处过期注释（见 P2-4） |
| D15 | 分页信封 `{items,total}` | §9.2 / M2a | ✅ 有意推迟，M2b 补记已确认以裸数组为准 |

---

## 二、问题清单（按严重程度从高到低）

### P0-1 压缩炸弹防护完全缺失

- **位置**：主设计 §11「压缩炸弹 | 限制单成员解压大小、单包总解压大小、压缩比、成员数；超限终止」；代码缺口在 `service/src/domain/archive/reader.py:152-184`（`_iter_binary_lines`）、`:191-220`（`_scan_zip`）、`:228-295`（`_scan_tar`）
- **现状**：唯一的体量约束是单行 1 MiB 截断（`reader.py:368`）。一个 10 MB 的 zip 可以声明解压出数十 GB 文本，Worker 会持续 `readline` 直到本地临时盘写满或进程被 OOM 杀掉。§7.2 里"单任务超过 2GB 记录 `memory_limit` 并失败"同样没有代码路径。
- **影响**：单台 Worker 主机的临时盘被一个恶意/损坏压缩包占满，进而拖垮同机其他 Worker；且失败方式是 OOM Kill 而非受控失败，只能靠 120 秒心跳超时兜底回收。

### P0-2 Worker 主循环没有异常隔离，一次数据库抖动就杀死整个进程

- **位置**：`service/src/worker/main.py:83`（`is_draining`）、`:88`（`claim_next`）、`:90/93/166`（`heartbeat_worker`）——全部在 `try` 之外；`run_forever` 的 `while` 循环（`:177-187`）也没有包 `try`
- **现状**：这三条都是数据库调用。任一条抛出 `OperationalError`（PG 重启、网络闪断、连接池打满），异常会穿过 `run_once` → `run_forever` → `main()`，进程直接退出，只剩 `finally` 里的一次 `offline` 上报。
- **影响**：与 §7.2「Worker 持续领取，直到队列为空或收到停止信号」直接冲突；§7.3 把"数据库短暂不可用"列为可重试错误，但对 Worker **进程本身**没有对应保护。4 个 Worker 可能因为同一次 PG 闪断同时全部退出，无人接任务，直到外部重启。

### P1-1 执行期缺少 realpath 越界校验，存在符号链接逃逸

- **位置**：`service/src/application/worker/processor.py:106-129`（`_check_source`）、`:303-315`（`_copy_to_local`）；规划期校验在 `service/src/application/services/jobs.py:101-106`
- **现状**：规划期用 `Path.resolve()` 校验 `source_path` 在 `root_path` 下、扫描时 `discover_archives` 跳过符号链接（`reader.py:117,131,137`）——这两处都对。但 `file_tasks.source_path` 只是规划时记下的字符串；执行期 `_check_source` 用 `source.stat()`（**跟随**符号链接）、`_copy_to_local` 直接 `open()`。规划与执行之间若有人把该文件替换成指向 `root_path` 之外的符号链接，Worker 会照抄照解析，把越界内容写进 `matches`。
- **影响**：§11「符号链接逃逸：扫描和复制时使用 realpath 校验，禁止跟随指向根路径外的符号链接」只实现了扫描侧。

### P1-2 逐行执行 pathspec 匹配，O(行数) 的重复计算

- **位置**：`service/src/application/worker/processor.py:180`（`if self._member_spec.match_file(line.member_path)`）——在每一行上都跑一次 glob 匹配
- **实测**：`**/*.log` 模式，`match_file` 702 ns/次；按成员缓存后 60 ns/次，**单文件 10 万行就差约 64 ms，单批 1000 个文件、每文件 10 万行约差 64 秒纯 CPU**
- **影响**：`member_path` 只在成员切换时变化，这个判断本质上应该是"每成员一次"。当前实现把它变成了与总行数成正比的热路径开销，直接吃掉 §8.1 想要留给规则匹配的 CPU。

### P1-3 `matches` 缺 `(rule_id, created_at)` 索引，且未按规格分区

- **位置**：主设计 §6.6「索引只保留 `(job_id, created_at, id)`、`(file_task_id)`、`(rule_id, created_at)`」、§8.3「按月分区」；代码 `service/src/domain/models/entities.py:119-121`
- **现状**：只建了 `ix_matches_job_created_id` 与 `file_task_id` 单列索引。`GET /api/matches?rule_id=` 是规格里的正式过滤项（`routes_matches.py:23`），当前会全表扫。分区完全没有。
- **影响**：千万级命中下，按规则过滤的查询退化为顺序扫描。

### P1-4 登录接口无限流，可暴力破解

- **位置**：`service/src/api/v1/routes_auth.py:43-56`；对照主设计 §11「越权 | 复用现有 JWT 双令牌 + RBAC；v1 只使用管理员角色；接口级校验」
- **现状**：`/api/login` 没有任何频率限制。bcrypt（cost 12）提供约 450 ms/次的自然成本，但仍允许无限次尝试；也没有失败计数、锁定或审计日志。M2a 计划显式把"token 黑名单"和"Redis 限流"列为不做，但登录限流不在其推迟清单里，属于遗漏。
- **影响**：管理员口令若偏弱，可被离线在线爆破；且登录失败不落审计，事后无法追溯。

### P1-5 前端未消费后端已上线的只读判定结果

- **位置**：后端 `service/src/api/v1/schemas.py:42-43`（`writability`、`read_only_confirmed`）；前端 `web/src/api/logharvestTypes.ts:242-247`（类型只有 3 个字段）、`web/src/views/logharvest/datasources/index.vue:130-138`（只渲染 `exists/is_dir/readable`）
- **现状**：M2c Task 1 给 `/api/data-sources/{id}/test` 加了三态判定，前端完全没跟上。用户在"测试路径"里只能看到三个布尔，看不到"挂载是否真的只读"。
- **影响**：M2c 的核心交付（把"我不能确认只读"如实说出来）在界面上丢失——正是 `schemas.py:34-36` 注释里明确要避免的"假的安心感"。

### P2-1 四个"声明但无行为"的死配置

| 项 | 位置 | 说明 |
|---|---|---|
| `prometheus-client` | `service/pyproject.toml:16` | 依赖已装，全仓零 import |
| `max_matches_per_task` | `service/src/config/settings.py:25` | 有字段、`.env.example` 注明"尚未强制执行"，代码零引用 |
| `data_sources.copy_to_local` | `entities.py:26` / `schemas.py:14,25` | 列可读写，但 Worker 无条件复制，开关不生效 |
| `workers.last_error` | `entities.py:180` / `schemas.py:179` | 列存在、API 暴露，无任何写入方 |

### P2-2 `ruff format` 有 4 个文件未格式化

- **位置**：`service/` 下 4 个文件（`ruff format --check` 报告），`ruff check` 通过说明只是换行/ joined-str 差异
- **影响**：CI 只跑 `ruff check` 就会漏掉，下一个改这些文件的人会带上无关 diff。

### P2-3 `GET /api/workers` 归属错层

- **位置**：`service/src/api/v1/routes_system.py:16-23` 定义了 `/workers` 列表，而 `routes_workers.py` 只有 drain/resume/drain-version
- **影响**：同一资源的读写分散在两个文件；`router.py:26-27` 两个 router 都挂 `/workers` 前缀，靠 include 顺序才不冲突，很脆。

### P2-4 前端三处注释与实现不符

| 位置 | 过期内容 | 实际 |
|---|---|---|
| `logharvest.ts:78` | "该端点在当前运行的后端上**尚未存在**" | `/jobs/{id}/files/{tid}/attempts` 已实现（M2c Task 3） |
| `logharvest.ts:89` | "该端点由后端 M2a 并行轨道补充实现" | `retry-failed` 已实现 |
| `logharvestTypes.ts:22` | "`partial`（成员级失败）仍未实现" | 已实现（commit `5227606`） |

### P2-5 作业终态判定语义在两处 SQL 里各写一遍

- **位置**：`service/src/infrastructure/task_queue.py:246-261`（`FINISH_JOB_SQL` 的 `NOT EXISTS`）与 `:267-280`（`CANDIDATE_JOBS_SQL` 的 `HAVING SUM(CASE...)`），语义完全等价
- **影响**：改一处漏一处就会出现"作业永远 running"或"作业提前收尾"，且这类 bug 只在补偿路径上出现，测试很难覆盖。

### P2-6 工作区残留产物

- `service/logharvest.db`（114 KB，开发库）、`web/dist/`（构建产物）——都在 `.gitignore` 里，但按"测试/构建后清理产物"的约定应清掉。

### P2-7 已知平台限制未在文档说明

- **位置**：`service/src/domain/source_guard.py:49`（非 POSIX 返回 `None`）
- **现状**：Windows 上 `read_mount_table()` 恒返回 `None`，退化为 `st_mode` 弱判定，目录几乎总有写位 → 结果恒为 `unknown`。也就是说 **`enforce` 策略在 Windows 上实际从不拦截**。`main.py` 里已装 psutil 但 `source_guard` 没用。
- **影响**：部署到 Windows 挂载 SMB 的场景下，§7.2 的"可写即拒绝"形同虚设，但文档没有写清这一点。

---

## 三、优化方案与实施优先级

| 优先级 | 问题 | 方案 | 落地位置 | 风险 |
|---|---|---|---|---|
| **P0-1** | 压缩炸弹 | 引入 `ArchiveBudget`（单成员字节、整包总字节、成员数、压缩比），在 `_iter_binary_lines` 累计并判定；单成员超限记该成员错误 → `partial`，整包超限抛 `ArchiveFormatError` → `failed` 不重试；4 个阈值进 `Settings` 并接线到 Worker | `domain/archive/reader.py`、`config/settings.py`、`application/worker/processor.py`、`worker/main.py` | 低（默认阈值远大于现有测试样本） |
| **P0-2** | 主循环无隔离 | `run_once` 整体包一层：DB/未知异常记 `worker_loop_error` 后返回 `False`，由 `run_forever` 退避等待；只有 `stop_event` 能终止循环。`run_forever` 再加一道兜底 `try` 保证 `offline` 能上报 | `worker/main.py` | 低（不改变成功路径） |
| **P1-1** | 符号链接逃逸 | `ClaimedTask` 增加 `root_path`；`_check_source` 里用 `source.resolve()` 校验仍在 root 之下，并且比对 `resolve()` 与规划路径一致（防中间被换成 symlink） | `worker/ports.py`、`infrastructure/task_queue.py`、`worker/processor.py` | 中（需要 PG SQL 多返回一个字段，改 `CLAIM_SQL`；需同步 fake 与测试） |
| **P1-2** | 逐行 pathspec | 在 `process` 里维护 `current_member` + `member_matched`，只在成员路径变化时匹配一次 | `application/worker/processor.py` | 低 |
| **P1-3** | 缺索引 | 新增 Alembic 增量迁移 `0003_matches_rule_index`，建 `ix_matches_rule_created (rule_id, created_at)`；模型 `__table_args__` 同步 | `domain/models/entities.py`、`alembic/versions/0003_*.py` | 低（增量、向后兼容） |
| **P1-4** | 登录限流 | 增加一个进程内滑动窗口限流器（按 username + 客户端 IP），默认 10 次/5 分钟，超限 429 并记录审计日志；阈值进 `Settings` | 新增 `domain/services/login_rate_limit.py`、`routes_auth.py`、`settings.py` | 中（需要拿到客户端 IP，要用 `Request`） |
| **P1-5** | 前端只读判定 | `DataSourceTestResult` 补 `writability` / `read_only_confirmed`；数据源页增加"只读确认"标签，三态用不同颜色，`unknown` 显示警告色并说明"平台无法判定" | `logharvestTypes.ts`、`datasources/index.vue` | 低 |
| **P2-2** | 格式漂移 | `ruff format` 这 4 个文件 | — | 无 |
| **P2-3** | 归属错层 | 把 `GET /workers` 从 `routes_system.py` 迁到 `routes_workers.py` | 两个 route 文件 + `router.py` | 低 |
| **P2-4** | 过期注释 | 三处注释改为反映现状 | 前端两个文件 | 无 |
| **P2-5** | SQL 重复 | 抽出"作业是否还有未完成任务"的判定常量/注释互引，至少让两处显式引用同一谓词定义 | `task_queue.py` | 低 |
| **P2-6** | 残留产物 | 删除 `service/logharvest.db`、`web/dist/` | — | 无 |
| **P2-7** | Windows 限制 | 在 `service/README.md` 明确写清"非 POSIX 平台只读判定恒为 unknown，enforce 不拦截；生产应在 Linux 只读挂载上部署" | `service/README.md` | 无 |
| P2-1 | 死配置 | **不删除**（列与依赖都在文档里有位置），改为在 `.env.example` 与 README 显式标注 + 记录到本报告 §1.4，作为 M3 待办 | 文档 | 无 |

### 实施顺序

1. P0-1 压缩炸弹限额（安全 + 可用性，最高）
2. P0-2 Worker 主循环异常隔离（可靠性）
3. P1-2 逐行 pathspec → 按成员缓存（改动最小、收益明确）
4. P1-1 执行期 realpath 越界校验（安全）
5. P1-3 `(rule_id, created_at)` 索引迁移
6. P1-4 登录限流
7. P1-5 前端只读判定展示
8. P2-2 / P2-3 / P2-4 / P2-5 / P2-6 / P2-7 收尾

每一步都先跑 `pytest -q` 确认基线不变，再进入下一步。

---

## 四、实施结果（2026-10-26）

§三 的问题清单已全部落地，**按报告顺序逐项实施，每项改完先跑全量 `pytest -q` 再进下一项**。

| 基线 | 收尾 |
|---|---|
| 后端 `pytest -q` **336 passed, 16 skipped** | **373 passed, 16 skipped**（净增 37 条用例） |
| `ruff check` 通过、`ruff format --check` 4 文件未格式化、`mypy` 24 文件 Success | `ruff check` / `ruff format --check` 全绿、`mypy` 25 文件 Success |
| 前端 `pnpm typecheck` / `pnpm build` 通过 | 同左（另新增只读三态标签与失败重试的剩余量提示） |

| 项 | 落地内容 |
|---|---|
| **P0-1** | `ArchiveBudget`（单成员字节 / 整包总字节 / 成员数 / 膨胀比）在流式读取中按**实际读出字节**记账；单成员超限只丢该成员 → `partial`，整包级超限停止扫描 → `failed` 且不重试。4 个阈值进 `Settings` 并接线到 `WorkerRuntime` → `TaskProcessor`。压缩比设 64 MiB 起效门槛，避免误伤正常小文件 |
| **P0-2** | `run_once` 的 drain/claim/心跳段进护栏（记 `worker_loop_error` + `stage` 后返回 `False`）；周期清扫独立护栏；`run_forever` 的 `while` 整体兜底，`BaseException`（SIGINT/SIGTERM）放行；心跳统一收口到 `_report_status`，管理视图心跳失败不再杀死正在跑的任务 |
| **P1-1** | `CLAIM_SQL` 带出 `data_sources.root_path` → `ClaimedTask.root_path` → `_check_root()` 在执行期按 realpath 复核，位置排在 `_check_source` 与本地复制之前 |
| **P1-2** | `process()` 内维护 `current_member` / `member_matched`，`file_glob` 只在成员切换时求值一次（实测 702ns/行 → 60ns/行） |
| **P1-3** | 模型补 `ix_matches_rule_created (rule_id, created_at)`；新增幂等迁移 `0003_matches_rule_index`；迁移测试从"逐表逐列"升级为"逐表逐列**逐索引**"比对 |
| **P1-4** | 新增 `domain/services/rate_limit.py`（滑动窗口，只统计失败、成功清零、key 上界 + LRU 淘汰）；`GET`/`POST /api/login` 超限返回 429 + `Retry-After`；阈值进 `Settings`；conftest 加 autouse 夹具隔离单例状态 |
| **P1-5** | `DataSourceTestResult` 补 `writability` / `read_only_confirmed`；数据源页加三态标签（已确认只读 / 可写：禁止处理 / 只读未确认），`unknown` 不再显示成绿勾 |
| **P2-2** | 4 个文件格式化（均为本次改动文件，无无关 diff） |
| **P2-3** | `GET /api/workers` 迁到 `routes_workers.py`，路径 `/api/workers` 不变，OpenAPI tag 由 `system` 归位为 `workers` |
| **P2-4** | 4 处过期注释改写（`logharvest.ts` ×2、`logharvestTypes.ts`、`logharvestAdmin.ts`），并顺手补上 `retry-failed` 返回体里一直被前端忽略的 `remaining_failed` |
| **P2-5** | 抽出 `_open_task_predicate()`，`FINISH_JOB_SQL` / `CANDIDATE_JOBS_SQL` / `_derive_job_status` 三处共用同一组状态常量；新增"两条 SQL 与 Python 判定结果一致"的用例钉住不漂移 |
| **P2-6** | 删除 `service/logharvest.db` 与 `web/dist/`（`.gitignore` 内的产物） |
| **P2-7** | `service/README.md` 写明"非 POSIX 平台只读判定恒 `unknown`、`enforce` 实际不拦截"，并给出只能靠 Linux 只读挂载环境验证的结论 |
| **P2-1** | 按"不删除"处理：`copy_to_local` / `workers.last_error` 的"当前无读写方"写进 README"明确不包含"；新增的 `NAS_*` / `ARCHIVE_*` / `LOGIN_*` 变量同时补进 `.env.example` 与 README 配置表 |

### 与方案书的三处有意偏差

1. **P1-1 判定放宽**：方案写的是"比对 `resolve()` 与规划路径一致"。实现改为只校验 realpath 仍在数据源根下——同一个文件本来就可能经由不同符号链接路径到达，比字符串会把正常数据源判死。
2. **P1-4 不引入 `slowapi`**：只有一个端点需要限流，且必须精确控制"只统计失败 / 成功清零 / key 数量有上界"三条语义，60 行进程内实现比一个通用依赖更容易验证。
3. **P1-3 迁移做成幂等**：`0003` 建索引前先 `inspect`——`0001` 用的是 `create_all` 捷径，新库在基线就带上了这条索引，直接 `create_index` 会撞 "already exists"。已在临时 SQLite 上分别验证"新库跳过"与"旧库补齐"两条路径。

### 仍然开放（属于 M3，见 §1.4）

多行堆栈规则、`matches` 按月分区、内存自适应并发、`max_matches_per_task` 强制执行、Prometheus 指标接入、`copy_to_local` 开关生效、`last_error` 写入、样本试跑端点、Worker 详情端点、Monaco 规则编辑器、结果保留期与告警。**P1-4 的登录限流是进程内计数**：多副本部署时实际放行量是阈值 × 副本数，届时要么按副本数折算阈值，要么换成共享计数（已在 README 与 `.env.example` 标注）。
