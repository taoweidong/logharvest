# LogHarvest 质量评估报告与分阶段优化方案（2026-10-29）

> 本报告是当前代码的复核与实施记录，不替代既有专项文档：
> `2026-10-28-code-quality-report.md`（独立全量复核）、
> `2026-10-28-quality-assessment-report-and-optimization-plan.md`（逐文件审计与 Phase 0–6 结果）、
> `2026-10-27-core-pipeline-assessment-and-e2e-test-plan.md`（核心链路真实数据 E2E 方案）。
> 本报告只记录**当前仍成立**的发现、本轮实施范围与验收证据；已闭环的旧问题不重复展开。

- 范围：`service/`（68 个 Python 源文件）、`web/` 业务代码（约 9.7k 行）、`deploy/`、`Jenkinsfile`、`agent.md`。
  vue-pure-admin 派生框架层只做契约抽查，不作为业务质量结论的依据。
- 基线：`main` @ `a7ca61c`；工作区另有未提交的 `.gitignore` 修改与未跟踪的
  `service/scripts/e2e_bench.py`，均未计入质量基线。
- 方法：全量 pytest + coverage、ruff、mypy、vitest、vue-tsc；关键路径逐文件复核；
  SQL/迁移/代理/队列配置静态核对。未启动 Docker，未做浏览器 E2E，未做 500 GB 压测。

---

## 一、结论摘要

**总体判断：基础工程质量仍然明显高于同类业务系统。** 后端分层方向正确，认证/授权/审计、
租约栅栏、解压预算、游标分页和失败重试都有真实测试；前端 API 层有明确的契约注释，
空值语义（`NULL ≠ 0`）和请求竞态防护有可回归的纯逻辑测试。

**当前没有发现新的 P0 代码缺陷。** 已排除：SQL 注入、未鉴权业务端点、路径穿越、
压缩包符号链接逃逸、`v-html`、硬编码密钥、默认口令。

**剩余风险集中在“生产路径的可验证性”和“误配/抖动时的容量与结果一致性”：**

| 排名 | 风险 | 级别 | 一句话 |
|---|---|---|---|
| 1 | `max_matches_per_task` 仍是死配置 | P1 | 空关键字或 `.*` 规则可把 `matches` 写到磁盘耗尽 |
| 2 | 失败任务的本尝试命中未清场 | P1 | 失败任务仍能查到半截结果，结果一致性受损 |
| 3 | 瞬时 NAS 错误被判永久失败 | P1 | `ESTALE`/`EIO` 抖动会把整批任务变成“假失败” |
| 4 | `file_tasks` 领取/回收查询缺索引 | P1 | 十万级任务起领取退化，34 万行实测 140 ms → 0.26 ms |
| 5 | 反向代理下限流按代理 IP 分桶 | P1 | 任意来源可定向锁死某个用户名的登录 |
| 6 | PostgreSQL 路径不在 CI 门禁 | P1 | 18 条真实 PG 用例默认跳过，迁移/队列 SQL 回归可能一路绿灯 |
| 7 | 前端只有纯逻辑测试 | P1 | 组件/E2E 仍靠手工验收；`strict: false` 削弱空值防线 |

**本轮选择先做 1–5（可独立验证、回滚边界清晰），并补前端纯逻辑测试与协作门禁。**
PG 入 CI、前端组件/E2E、容器非 root/TLS、依赖 CVE、`matches` 分区/保留期属于后续阶段，
理由与验收见 §五。

---

## 二、量化基线

### 2.1 代码与测试

| 指标 | 本轮实测 |
|---|---|
| 后端源码 | 68 个 `.py`；约 7.8k 行 |
| 后端测试 | 630 passed / 19 skipped，76.31 s |
| 后端覆盖率 | **97%**（3,517 statements，89 missed） |
| 后端静态检查 | `ruff check` 全绿；`ruff format --check` 120 文件已格式化；`mypy` 29 文件 0 错误 |
| 前端测试 | 77 passed / 7 spec，17.02 s |
| 前端类型检查 | `vue-tsc --noEmit --skipLibCheck` 0 错误（`strict: false`） |
| 前端业务代码 | 约 9,719 行（`api`/`views/{logharvest,rules,system}`/`utils`/`store/modules`） |
| 前端测试代码 | 858 行，全部为纯逻辑/工具函数，无组件挂载、无 E2E |

覆盖率最低项（非入口胶水）：

| 模块 | 覆盖 | 未覆盖性质 |
|---|---|---|
| `src/application/services/maintenance.py` | 77% | 非 PG 503 分支 |
| `src/infrastructure/task_queue.py` | 82% | PG 专有 CLAIM/RECOVER SQL，由 19 条集成用例覆盖但默认跳过 |
| `src/domain/archive/reader.py` | 94% | 损坏容器/预算边界的少量分支 |
| `src/application/services/rbac_seed.py` | 93% | 种子缺行防御分支 |
| `src/domain/services/passwords.py` | 84% | 异常/边界分支 |

### 2.2 契约与安全面

| 检查 | 结果 |
|---|---|
| 无鉴权端点 | 仅 `GET /api/health`、`POST /api/login`、`POST /api/refresh-token` |
| 应用层反向依赖 API 层 | 0 命中（`grep api.v1.schemas src/application`） |
| 服务层 `raise HTTPException` | 0 命中 |
| 前端 `v-html` / 硬编码密钥 / `console.log` | 0 / 0 / 0 |
| 前端 API 调用与后端路径 | 40 个调用与 47 个 OpenAPI 端点，0 路径不匹配（继承 2026-10-28 复核） |

---

## 三、分维度评估

### 3.1 代码结构与分层

**结论：强。**

- 后端 `api → application → domain → infrastructure` 方向真实落地；路由只做 HTTP 映射、
  权限依赖与审计，业务约束在 application/domain；domain 不依赖 FastAPI。
- Worker 端口用 Protocol 隔离（`application/worker/ports.py`），处理器可脱离数据库单测。
- 前端按 `api`（契约/类型）、`views`（页面）、`utils`（HTTP/SSE/树）、`store` 分层；
  纯逻辑刻意抽到 `.ts`（`format.ts`、`rule-form.ts`、`filters.ts`）以便单测。

**待改进：**

- `TaskProcessor.process` 168 行 / 圈复杂度 26，同时承担守卫、复制、解压、匹配、批量写、
  心跳、失败分类、终态收尾；任何修改都容易触碰多个不变量。
- 前端 9 个业务 SFC 超过 400 行（`jobs/detail.vue` 692 行等），表格/分页/抽屉逻辑重复。
- 本轮不拆大函数/SFC：行为不变的重构收益低于回归风险，先修语义缺陷。

### 3.2 命名与可读性

**结论：良好。**

- 命名普遍表达意图（`PermanentTaskFailure`、`LeaseLost`、`_derive_job_status`、
  `pendingRequests`），领域词表集中（`OPEN_TASK_STATUSES`、`FAILED_TASK_STATUSES`）。
- 注释主要解释“为什么”（登录时序侧信道、SQLite 外键顺序、UTC 会话钉扎、租约栅栏），
  不是复述代码。

**问题：**

- `helpers.py` 的 `client_ip` 注释仍把“不信任 XFF”写成固定策略，与新增可信代理配置后
  的语义需要同步。
- `service/README.md` 第 7 步仍写“取消检查每行一次”，实际是心跳预算（每 2 万行或 5 秒）；
  文档漂移会误导后续测试与排障。
- `max_matches_per_task`、`copy_to_local`、`workers.last_error` 的注释/文档与实际行为不一致
  （前者即将修复，后两者仍为死配置）。

### 3.3 重复与冗余逻辑

**结论：后端重复度低（Python 在 10 行/80 token 阈值下近似 0），前端仍有局部重复。**

| 位置 | 问题 | 影响 |
|---|---|---|
| `api/v1/routes_{users,roles,menus,workers,data_sources}.py` | 历史 `_client_ip` 已收编到 `helpers.client_ip`，但 `routes_auth.py:91` 仍内联 `request.client.host` | 代理信任策略改动会漏掉登录限流这一处 |
| 前端 8 个视图 | 窗口式分页（`hasNext = rows.length === limit`、offset 步进、按钮禁用）各写一份 | 分页边界修复要改 8 处 |
| `api/logharvest.ts`、`views/logharvest/format.ts`、`store/modules/user.ts` | 错误详情提取三套，422 展开行为不一致 | 同一错误在不同页面文案不同 |
| `views/logharvest/{datasources,jobs/create-dialog}.vue` | 数据源只读三态展示两套 | “unknown 不冒充已确认只读”不变量散落 |
| `listRuleSets` 各调用点 | page size 20/200/500 不一致，超限静默截断 | 下拉可能少项且无提示 |

本轮只修第一项（与 Q-01 同源）；其余登记为 P2，避免一次性大范围改前端。

### 3.4 错误与异常处理

**结论：整体分层清晰，Worker 终态语义有两个真实缺口。**

- 后端有三种明确范式：`RbacError.status_code` 映射、领域异常在路由层翻译、
  路由层直接 `HTTPException`；application 不再抛 `HTTPException`。
- `TaskProcessor` 区分 `LeaseLost`（不写终态）、`JobCancelled`（写 cancelled）、
  `PermanentTaskFailure`（failed + retryable=False）、普通异常（failed + retryable=True）。
- 审计写入失败不影响业务（吞异常 + 结构化告警）。

**缺口：**

1. `max_matches_per_task` 只定义不执行（`config/settings.py:31` 全仓库唯一引用）。
   空关键字 `any=[""]` 与 `.*` 正则可命中所有行；单任务命中数无上限。
2. 失败任务的本尝试命中只在**下一次尝试开头**清理（`processor.py:195-196` 仅
   `attempts > 1`），失败返回时没有删除本次已写入的 `matches`；用户在失败任务下能看到
   半截结果。
3. `_check_source` / `_check_root` 把任何 `OSError` 都转成 `PermanentTaskFailure`
   （`processor.py:126-127,142-143`）；`ESTALE`/`EIO`/`ETIMEDOUT` 等 NAS 抖动会烧光
   重试次数，整批任务变“假失败”。
4. 全 partial 任务的作业汇总原为 `failed`（`task_queue._derive_job_status` 把
   `partial` 计入 `failed`）。成员级 `partial` 是“部分成功”，作业层应保留 `partial`；
   已在 Phase 6 修正并补回归测试（见 §7.8）。

### 3.5 边界与空值

**结论：前端空值纪律突出，后端边界测试较完整。**

- 前端 `format.ts` 明确 `NULL`/`undefined`/空串映射为 `—` 或 `-`，`formatCpuPercent` 不把
  `null` 显示成 `0.0%`；SSE 解析覆盖 CRLF、残帧、非法 JSON；422 错误展开为多行。
- 后端解压预算覆盖单成员超限（partial）、整包超限（failed 不重试）、成员数/膨胀比；
  长行 4096 字节截断；tar 损坏成员停止扫描并标记 `scan_incomplete`。

**待补：**

- 前端纯逻辑未覆盖：`fromNow` 的时间分段、`formatDuration` 的 59/60/3599/3600 边界、
  `toApiDateTime` 的 epoch 0、`formatDetailError` 的空数组/缺字段。
- 后端未覆盖：命中上限边界（cap=2、单行多命中）、瞬时 `OSError` 分类、
  失败清场、可信代理头的伪造/畸形输入。

### 3.6 接口契约一致性

**结论：强，仍有两点需要同步。**

- 认证接口使用 `{code,message,data}` 信封，业务接口使用裸对象/裸数组；前端 API 层逐条注释，
  路径与后端 47 个端点无漂移。
- `response_model` 已覆盖主要业务端点；workers 三个动作端点已补 `WorkerActionRead`/
  `DrainVersionRead`。
- 新增 `GET /api/health/ready` 属于**新增**端点，不改变既有契约；前端不需要同步类型。
- 需要同步：`client_ip` 的可信代理配置进入 `.env.example` / `service/README.md`；
  `max_matches_per_task` 从“尚未强制执行”改为“已强制执行”，并说明达限语义。

### 3.7 安全隐患

**结论：认证/授权设计扎实，生产入口仍需硬化。**

| 风险 | 证据 | 影响 |
|---|---|---|
| 登录限流代理坍缩 | `helpers.py:13-15` 只认 TCP 对端；`routes_auth.py:91` 内联同一取值；`service/Dockerfile:31` 未加 `--proxy-headers`；`deploy/server/docker-compose.yml` 把 8000 发布到所有网卡 | nginx 后所有登录共享代理 IP 桶；任意来源对 `admin` 失败 10 次可让所有人 5 分钟内无法登录 |
| 依赖 CVE | `python-jose 3.5.0` / `ecdsa 0.19.2`（代码显式 HS256，已缓解但版本仍受影响）；npm 16 条 advisory | 供应链风险，当前不可直接利用 |
| 容器与入口硬化 | Dockerfile 无 `USER`；Compose 无资源上限/只读根；nginx 无 CSP/X-Frame-Options | 内网工具可接受，生产发布前需补 |
| token 存储 | 非 HttpOnly cookie + localStorage，无 CSP | XSS 可窃取 token；模板固有取舍，需部署层 CSP 配合 |
| 审计/日志 | 正则超时按行 warning；无请求 ID | 坏规则可刷爆日志，排障难 |

本轮实现可信代理 CIDR + `client_ip` 统一解析，**默认不信任任何转发头**，保持现有安全性；
生产必须显式配置 `LOGHARVEST_TRUSTED_PROXY_CIDRS` 才能按真实客户端分桶。
其余（CVE、容器、TLS、CSP）登记为后续。

### 3.8 性能瓶颈

**结论：设计层面已做对（批量写、游标分页、SSE 短会话、编译期关键字），仍有两个明确瓶颈。**

| 瓶颈 | 证据 | 实测/影响 |
|---|---|---|
| `file_tasks` 领取/回收缺索引 | `entities.py:77-101` 无 status/scheduled_at/heartbeat_at 索引；`CLAIM_SQL` 过滤 `status='pending' AND scheduled_at<=now()` 排序 `(scheduled_at,id)`；`RECOVER_STALE_SQL` 过滤 running + heartbeat | 34 万行：Seq Scan + 外部排序 **140.695 ms**；加部分索引 **0.258 ms** |
| `matches` 无单任务上限 | `processor.py:218-247` 直接累积 buffer | 一条误配规则可写满磁盘；与 Q-02 同源 |
| 前端规则集列表 N+1 | 每行一个详情请求算规则数 | 每页最多 20 次额外请求 |
| 前端主包偏大 | Element Plus 手工注册 260 行；主包 1,094,246 B / gzip 339,980 B；`chunkSizeWarningLimit: 4000` | 首屏加载与弱网体验 |
| SSE 每观看者每秒 2 条查询 | `routes_events.py` 快照轮询 | 多观看者时数据库压力线性增长 |

本轮补索引与命中上限；N+1、bundle、SSE 聚合登记为后续。

### 3.9 现有测试覆盖

**结论：后端强，前端纯逻辑已覆盖但缺组件/E2E，CI 缺 PostgreSQL。**

- 后端 630 条用例覆盖领域规则、压缩读取、队列 SQL 形状、处理器租约/预算/编码/长行、
  RBAC/认证/审计、SSE、维护入口；19 条 skip 全部是需要真实 PostgreSQL 的集成用例。
- 前端 77 条用例覆盖 SSE 帧解析、规则表单判别联合、时间/空值格式化、401 刷新队列契约、
  cookie 自愈、权限通配、角色/菜单筛选；0 组件挂载、0 E2E。
- `Jenkinsfile:63-68` 只跑 `pytest tests -q`，没有 PG 服务与 `LOGHARVEST_TEST_PG_URL`；
  `tests/integration/*` 默认整体 skip。
- `mypy` 只查 29/68 文件（`pyproject.toml:56-60`）；前端无 ESLint/Prettier/Stylelint；
  覆盖率没有 `--cov-fail-under`；Jenkins 的 `junit` 步骤没有 XML 产出。

---

## 四、问题清单（按优先级）

### P0（严重）

**本项目代码内未发现新的 P0。**

### P1（重要，本轮实施 1–5）

| ID | 问题 | 影响范围 | 证据 |
|---|---|---|---|
| Q-01 | 登录限流在 nginx 后按代理 IP 分桶，可定向锁死用户名 | 认证入口、审计来源 IP | `routes_auth.py:91`、`helpers.py:13-15`、`Dockerfile:31`、`deploy/server/docker-compose.yml` |
| Q-02 | `max_matches_per_task` 零引用；空关键字/`.*` 可无上限写 `matches` | Worker、PostgreSQL、磁盘容量 | `settings.py:31`、`processor.py:218-247`、`evaluator.py:81-100` |
| Q-03 | `file_tasks` 领取/回收无部分索引 | 队列延迟、DB CPU/IO | `entities.py:77-101`、`task_queue.py:16-39,152-158` |
| Q-04 | PostgreSQL 集成套件与迁移链不在 CI | 队列 SQL/迁移回归 | `Jenkinsfile:63-68`、`tests/integration/test_postgres_queue.py:40-43` |
| Q-05 | 失败任务半截命中可见；瞬时 NAS 错误判永久失败 | 结果一致性、人工重试成本 | `processor.py:126-127,142-143,195-196,271-312` |
| Q-06 | 前端无组件/E2E，`strict: false` | 登录/SSE/403/作业创建流程回归 | `vitest.config.ts`、`tsconfig.json:6-7` |

### P2（一般，登记后续）

| ID | 问题 | 影响 / 建议 |
|---|---|---|
| Q-07 | 依赖 CVE 与审计门禁缺失 | 评估迁移 `python-jose → PyJWT/joserfc`；`pip-audit`/`pnpm audit` 入 CI，允许显式豁免 |
| Q-08 | 静态门禁覆盖不完整 | 扩 mypy 范围；前端 lint/format；`--cov-fail-under`；JUnit XML |
| Q-09 | 容器/入口硬化不足 | 非 root、资源上限、只读根、API 回环绑定、nginx 安全头、TLS |
| Q-10 | 健康检查/可观测性不足 | 本轮补 readiness；请求 ID、Workers healthcheck、指标、超时告警限频后续 |
| Q-11 | 前端构建与性能 | Element Plus 按需、bundle 预算、`gzip_static`、规则集批量计数、分页 composable |
| Q-12 | 数据生命周期 | `matches` 分区/保留期、备份恢复演练、迁移基线漂移 |
| Q-13 | 复杂度热点 | `TaskProcessor.process`、`jobs.create_job`、`rbac.update_menu`、`reader.discover_archives`、9 个大 SFC |
| Q-14 | 一致性与死配置 | `copy_to_local`、`workers.last_error`、`prometheus-client`、`hypothesis`（仅测试用）、手动重试 attempts 语义、两种响应信封 |
| Q-15 | 作业终态汇总 | 全 `partial` 任务原汇总为 `failed`；Phase 6 已改为保留 `partial` |

---

## 五、分阶段优化方案

总原则：**每阶段独立可验证；行为变更必须带断言测试；每阶段结束跑完整门禁；
回滚边界为单阶段文件集合，可按阶段 revert。**

### Phase 0 — 基线与报告（本轮已完成）

- 内容：当前代码全量测试/覆盖率/静态检查；问题复核；本报告。
- 验收：630 passed / 19 skipped；97%；ruff/mypy 绿；前端 77 passed/typecheck 0。
- 回滚：仅新增本报告，无代码变更。

### Phase 1 — 后端可靠性与结果一致性（行为变更，带测试）

**目标：** 关掉“单条规则写爆库”“失败任务半截结果可见”“NAS 抖动假失败”三条路径。

**涉及文件：**

- `service/src/application/worker/processor.py`：新增 `max_matches_per_task` 参数与
  `MatchLimitExceeded`；达限停止、清理本任务命中、`failed + retryable=False`；
  失败终态清理本尝试命中；新增 `RetryableTaskFailure` 与瞬时 `OSError` 分类。
- `service/src/worker/main.py`：把 `settings.max_matches_per_task` 注入 `WorkerRuntime` →
  `TaskProcessor`。
- `service/tests/application/worker/test_processor.py`：命中上限、失败清场、瞬时/永久
  `OSError`、partial 不清场、达限不写 succeeded。
- `service/tests/worker/test_runtime.py`：配置透传断言。

**验收标准：**

- cap=2 + 3 行全命中：任务 `failed`、`retryable=False`、错误含 `max_matches_per_task`，
  本任务 `matches` 已删除，写入行数 ≤ cap。
- 处理中途异常：任务 `failed`，本任务 `matches` 已删除；`partial` 保留命中。
- `stat()` 抛 `EIO`/`ESTALE`：任务 `failed` 且 `retryable=True`（进入重试）；
  `ENOENT`/size/mtime 变化仍 `retryable=False`。
- 全部既有测试不改断言；新增用例自建数据、无顺序依赖。

**回滚边界：** `processor.py`、`worker/main.py` 及对应测试；无数据库/接口契约变更。
达限语义是行为变更，回滚后回到“无上限”，需同时回滚 `.env.example`/README 说明。

### Phase 2 — 后端性能与安全硬化（行为不变/新增配置）

**目标：** 队列在十万级任务下不退化为全表扫描；代理后可配置真实客户端 IP；
健康检查能区分“进程活着”和“数据库可用”。

**涉及文件：**

- `service/src/domain/models/entities.py` + `service/alembic/versions/0006_file_task_queue_indexes.py`：
  新增部分索引：
  - `ix_file_tasks_claim (scheduled_at, id) WHERE status='pending'`
  - `ix_file_tasks_recover (heartbeat_at) WHERE status='running'`
- `service/src/config/settings.py`：新增 `trusted_proxy_cidrs: str = ""` 与 CIDR 校验。
- `service/src/api/v1/helpers.py`：可信对端才解析 `X-Forwarded-For`/`X-Real-IP`；
  非可信对端忽略转发头；`routes_auth.py:91` 改用 `client_ip`。
- `service/src/api/v1/router.py`：`GET /api/health/ready`（`SELECT 1`，失败 503）。
- `service/tests/infrastructure/test_migrations.py`（索引对齐）、
  `service/tests/api/test_helpers.py`（代理矩阵）、`service/tests/test_health.py`（readiness）、
  `service/tests/test_settings.py`（CIDR 校验）。
- `.env.example`、`service/README.md`：配置与语义同步。

**验收标准：**

- `alembic upgrade head` 在 SQLite 测试库成功；`SQLModel.metadata` 与迁移的索引集合一致；
  新库/旧库升级都得到两条索引。
- 非可信对端伪造 `X-Forwarded-For`/`X-Real-IP` 不改变 `client_ip`；可信对端从右向左取
  第一个非可信地址；畸形头回退到 TCP 对端；CIDR 非法时启动失败。
- `/api/health` 保持静态 liveness；`/api/health/ready` 在 DB 正常时 200、DB 异常时 503。
- 既有认证/RBAC/队列测试零修改通过。

**回滚边界：** 新增迁移可 `downgrade` 删除索引；配置默认空 = 不信任任何代理，回滚到旧行为；
readiness 是新增端点，不影响既有契约。

### Phase 3 — 前端单元测试补全（不改业务行为）

**目标：** 把当前可纯逻辑验证的边界补到与后端同等的密度；组件/E2E 不在本阶段。

**涉及文件：**

- `web/tests/logharvest/format-time.spec.ts`：`fromNow` 时间分段、`formatDuration`
  59/60/3599/3600/86399 边界、`toApiDateTime(0)`、`formatGB/MB(0)`、`formatDetailError`
  空数组/缺字段。
- `web/tests/api/contract.spec.ts`：mock `http.request`，逐条断言作业/命中/Worker/数据源/
  规则集客户端的 method、URL、params、`Idempotency-Key`、`createJob` 60s 超时。
- `web/tests/logharvest/status.spec.ts`：`TERMINAL_JOB_STATUSES` 是 `JOB_STATUS_OPTIONS`
  子集、无重复、`SEVERITY_OPTIONS` 与 `SEVERITY_TAG` 对齐。
- 必要时补 `web/tests/utils/tree.spec.ts`：只测已被业务使用的 `handleTree`/`filterTree`/
  `collectTreeIds`，不锁定未使用函数的既有怪异行为。

**验收标准：** `pnpm test` 全绿、可重复执行（无时间/顺序依赖）、`pnpm typecheck` 0 错误；
新增用例不依赖真实网络/浏览器。

**回滚边界：** 纯新增测试文件；无业务代码变更。

### Phase 4 — 协作门禁、文档与全量验证

**目标：** 让“每次 AI 交互必须跑前后端全量单测，全绿才允许提交”成为可执行、可核对的规范；
文档与实际行为一致。

**涉及文件：**

- `agent.md`：把门禁写成强制检查清单；明确“每次 AI 交互结束前”执行；失败不得提交；
  skip 仅限环境依赖；禁止弱化断言；提交说明须记录命令与结果。
- `service/README.md`、`.env.example`：命中上限、可信代理、readiness、取消检查频率、
  索引迁移说明。
- 本报告：回填实施结果与验收证据。

**验收标准：** 后端 pytest、ruff、mypy、前端 vitest、typecheck 一次性全绿；
`grep` 确认 `max_matches_per_task` 有执行点、`api.v1.schemas`/`HTTPException` 在 application
仍为 0；OpenAPI 仅新增 readiness（如前端不需要则不新增类型）。

**回滚边界：** 文档与规范变更；回滚不影响运行时。

### 有意不做（本轮外）

- PostgreSQL 集成套件入 CI（Q-04）：已追加 Phase 5（一次性容器 + Jenkins stage，见 §7.7）。
- 前端组件/E2E、`strict: true`（Q-06）：需要浏览器栈与大量既有隐式 any 清理。
- 容器非 root/TLS/CSP、依赖 CVE 修复（Q-07/Q-09）：部署与供应链专项。
- `matches` 分区/保留期（Q-12）、大函数/大 SFC 拆分（Q-13）：行为/契约影响面大，按专项排期。
- 全 partial 作业汇总（Q-15）已追加 Phase 6（见 §7.8）。

---

## 六、验证命令（每阶段结束与提交前）

```bash
# 后端
cd service
.venv/Scripts/python.exe -m pytest tests -q
.venv/Scripts/python.exe -m ruff check src tests scripts
.venv/Scripts/python.exe -m ruff format --check src tests
.venv/Scripts/python.exe -m mypy

# 前端
cd ../web
pnpm test
pnpm typecheck
```

环境允许时追加真实 PostgreSQL：

```bash
cd service
LOGHARVEST_TEST_PG_URL="postgresql+psycopg://.../一次性库" \
  .venv/Scripts/python.exe -m pytest tests/integration -q
```

---

## 七、实施结果（本轮回填）

> 本节记录 2026-10-29 实际落地的变更与验证证据；未实施项在 §五“有意不做”与 §八列明。

### 7.1 基线对照

| 门禁 | 实施前 | 实施后 |
|---|---|---|
| 后端 `pytest tests -q` | 630 passed / 19 skipped | **666 passed / 20 skipped**（159.53 s） |
| 后端覆盖率 `--cov=src` | 97%（3,517 stmts / 89 miss） | **97%**（3,619 stmts / 94 miss，已跟踪模块）；含并发未跟踪 `aho.py` 时 96%（3,677 / 152） |
| 后端 `ruff check` / `format --check` / `mypy` | 全绿 / 120 文件 / 29 文件 | **全绿 / 123 文件 / 30 文件** |
| 前端 `pnpm test` | 77 passed / 7 files | **102 passed / 11 files** |
| 前端 `pnpm typecheck` | 0 错误 | **0 错误** |

### 7.2 Phase 1（后端可靠性与结果一致性）

| 变更 | 文件 | 验证 |
|---|---|---|
| 新增 `max_matches_per_task`（默认 20 万）并在写入每条命中前检查；达限抛 `MatchLimitExceeded`，停止扫描 | `service/src/application/worker/processor.py`、`service/src/worker/main.py` | cap=2 + 3 行全命中：任务 `failed`、`retryable=False`、`matches` 清空、错误含 `max_matches_per_task` |
| 失败终态先删本尝试命中再写 `complete`（`partial` 保留）；删除同样过租约栅栏 | 同上 | 中途写库异常：`failed`、命中删除、`retryable=True`；`partial` 用例断言 `deleted == []` |
| `OSError` 分类：`EIO`/`ESTALE`/`ETIMEDOUT` 等 → `RetryableTaskFailure`；`ENOENT`/尺寸/mtime 变化仍永久失败 | 同上 | `stat()` 注入 `EIO`、`resolve()` 注入 `ESTALE`：`retryable=True`；既有缺失文件/尺寸变化用例仍 `retryable=False` |
| 配置透传 | `WorkerRuntime` → `TaskProcessor`；`main()` 注入 `settings.max_matches_per_task` | `tests/worker/test_runtime.py` 断言 7 透传、main 接线 |

新增/修改用例：`tests/application/worker/test_processor.py`（+5）、`tests/worker/test_runtime.py`（+1 及断言扩展）。

### 7.3 Phase 2（后端性能与安全硬化）

| 变更 | 文件 | 验证 |
|---|---|---|
| `file_tasks` 两条部分索引：`(scheduled_at, id) WHERE status='pending'`、`(heartbeat_at) WHERE status='running'` | `service/src/domain/models/entities.py`、`service/alembic/versions/0006_file_task_queue_indexes.py` | 迁移在 SQLite 测试库建出两条索引；metadata 逐索引对比通过；`test_file_task_queue_indexes_are_partial` 断言列序与谓词 |
| 可信代理 CIDR：默认空；仅可信 TCP 对端解析 `X-Forwarded-For`/`X-Real-IP`，从右向左取第一个非可信地址；畸形头回退 TCP 对端 | `service/src/config/settings.py`、`service/src/api/v1/helpers.py`、`service/src/api/v1/routes_auth.py` | 9 条 helper 用例覆盖直连伪造、可信代理、代理链、IPv6、畸形头、无 client；设置校验 3 条 |
| readiness：`GET /api/health/ready` 执行 `SELECT 1`，失败 503；liveness 保持静态 | `service/src/api/v1/router.py` | 200/503 两条用例 |
| 部署接线：server 传 `LOGHARVEST_TRUSTED_PROXY_CIDRS`，worker 传 `LOGHARVEST_MAX_MATCHES_PER_TASK` | `deploy/server/docker-compose.yml`、`deploy/server/.env.example`、`deploy/worker/docker-compose.yml`、`deploy/worker/.env.example` | 静态配置核对 |

新增用例：`tests/api/test_helpers.py`（9）、`tests/test_health.py`（+2）、`tests/test_settings.py`（+4）、`tests/api/test_auth.py`（+1）、
`tests/infrastructure/test_migrations.py`（+1）、`tests/infrastructure/test_task_queue.py`（+1）。

### 7.4 Phase 3（前端单元测试）

| 文件 | 覆盖 |
|---|---|
| `web/tests/logharvest/format-time.spec.ts`（9） | `fromNow` 秒/分/时/天与 UTC；`formatDuration` 0/59/60/3599/3600/86399；真实 0 与未知值；`toApiDateTime(0)`；`formatDetailError` 非字符串边界 |
| `web/tests/api/contract.spec.ts`（7） | 作业/命中/Worker/数据源/规则集客户端的 method、URL、params；`createJob` 幂等键 + 60s 超时 |
| `web/tests/logharvest/status.spec.ts`（3） | 终态子集、无重复、全量状态列表、严重度选项与颜色映射一致 |

结果：77 → 102 条（新增 RBAC/审计/统计客户端契约 6 条），全部纯逻辑、无网络/浏览器依赖；`pnpm typecheck` 0 错误。

### 7.5 Phase 4（门禁与文档）

- `agent.md`：把门禁强化为“每次 AI 交互结束前执行、提交前再次执行”；新增静态门禁、
  失败处理顺序、测试可重复性、提交说明必须记录通过/跳过数量等条款。
- `README.md` / `service/README.md` / `.env.example`：同步命中上限、可信代理、readiness、
  迁移 0006、取消检查频率；更新测试统计。
- 本报告回填；未修改既有 `service/scripts/e2e_bench.py` 与 `.gitignore` 的前序改动。

### 7.6 未实施与残余风险

- **Q-04 PostgreSQL 入 CI**：已追加 Phase 5（见 §7.7），并已用一次性 PG 16.15 库跑通迁移与
  19 条集成用例；Jenkins 的 Docker 容器阶段本机未实跑，首次 CI 仍需确认 agent 的 Docker 权限与端口映射。
- **Q-06 前端组件/E2E、`strict: true`**：未实施；本轮只补纯逻辑。
- **Q-01 的部署侧**：代码支持可信代理，但默认不信任；未配置时仍按代理 IP 分桶。
- **Q-15 全 partial 作业汇总**：已追加 Phase 6（见 §7.8），全部 `partial` 任务现在汇总为 `partial`。
- **Q-07/Q-09/Q-12/Q-13**：依赖 CVE、容器/TLS、分区/保留期、复杂度热点均未实施。
- **并发工作区新增未跟踪模块**：`service/src/domain/rules/aho.py`（58 行，0% 覆盖）不是本轮改动；若该 Aho–Corasick 原型要合入，需补单测并接入规则编译/求值链路。

---

### 7.7 Phase 5（2026-10-29 追加）：PostgreSQL CI 门禁

| 变更 | 文件 | 验证 |
|---|---|---|
| 新增一次性 PostgreSQL 集成脚本：启动 `postgres:16`、随机宿主端口、等待 ready、空库 `alembic upgrade head`、断言 `file_tasks` 两条部分索引、运行 `tests/integration`、退出清理 | `service/scripts/run_pg_integration.sh` | `bash -n` 语法通过；本机无 Docker，未实际启动容器；已用本机一次性 PG 16.15 库手工执行同等步骤（见下） |
| Jenkins 增加「PostgreSQL 集成测试」阶段 | `Jenkinsfile` | 静态 diff 审查；阶段位于后端单元测试之后、前端测试之前 |
| 本地/CI 使用说明 | `service/README.md` | 文档同步 |

回滚边界：删除脚本 + 移除 Jenkinsfile 该 stage 即回到原状；不影响运行时与既有单元测试。
首次 CI 运行需确认 Jenkins agent 具备 Docker 权限、可拉取 `postgres:16`，且宿主端口可随机映射。

本机验证（2026-10-29，一次性库，验证后已删除）：

```text
temporary database: logharvest_quality_ci_<unique>
PostgreSQL migration smoke passed   # alembic upgrade head + 0006 索引断言
19 passed in 7.51s                  # tests/integration 全量
```

### 7.8 Phase 6（2026-10-29 追加）：全 partial 作业终态修正

| 变更 | 文件 | 验证 |
|---|---|---|
| `_derive_job_status`：存在 `partial` 即返回 `partial`（即使没有 `succeeded`）；无 `partial` 时才按 `succeeded`/`failed` 组合判定 | `service/src/infrastructure/task_queue.py` | 新增 2 条用例：全部 `partial` → `partial`；`partial + failed` → `partial`；既有 mixed/all-failed/cancelled 用例不改 |
| 文档同步 | `service/README.md`、`docs/core-logic.md` | 作业终态表与汇总规则更新 |
| PG 回归 | 一次性 PG 16.15 库 | `alembic upgrade head` + `tests/integration` **19 passed** |

回滚边界：仅 `task_queue.py` 的 Python 判定 + README/core-logic + 2 条测试；无迁移、无接口契约变更。
`partial` 的前端筛选与展示已支持，无需改前端。

## 八、残余风险与后续建议

1. **PG 路径已进 CI，但仍需首次实跑**：Jenkins 已新增一次性 `postgres:16` 阶段；本机已用
   一次性 PG 16.15 库验证 `alembic upgrade head` + 19 条集成用例。首次 Jenkins 运行仍需确认
   agent 的 Docker 权限、镜像拉取与端口映射。
2. **可信代理需要部署配置**：默认空 = 不信任转发头；未配置时 Q-01 仍存在（但不会比现状更差）。
   生产需把 nginx 所在网段/地址写入 `LOGHARVEST_TRUSTED_PROXY_CIDRS`，并停止把 8000 直接发布到公网。
3. **前端仍无组件/E2E**：本轮只补纯逻辑；登录刷新、SSE 断流、403 降级、创建作业 503
   仍需手工验收或后续 Playwright。
4. **全 partial 作业汇总已修正**：`_derive_job_status` 现在保留 `partial`，新增 2 条用例并通过
   PG 集成回归；前端已支持 `partial` 筛选/展示。
5. **命中上限是单任务级**：单作业仍可能由多个任务累计写满磁盘；作业级上限/采样/分区
   需与 Q-12 一起排期。
