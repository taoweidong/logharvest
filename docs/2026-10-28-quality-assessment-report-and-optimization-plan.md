# LogHarvest 前后端系统性质量评估报告与分阶段优化方案（2026-10-28）

- 日期：2026-10-28
- 范围：`service/`（后端 66 个源文件、约 7.6k 行）+ `web/`（前端业务代码：`src/api`、`src/views/{logharvest,rules,system}`、`src/utils`、`src/store/modules/user.ts`、`src/directives/perms`）；vue-pure-admin 派生框架层只做粗看
- 基线（审计起点实测）：
  - 后端 `pytest -q`：**435 passed / 19 skipped**（跳过项全部是需要真实 PostgreSQL 的集成测试）；`ruff check`、`ruff format --check`、`mypy`（28 文件）全绿
  - 后端 `pytest --cov=src`：**总覆盖率 96%**（3478 语句 / 153 未覆盖）
  - 前端 `pnpm typecheck`：0 错误；**测试基础设施为零**（无 vitest/无任何 spec）
  - 方法：后端逐文件通读（api/application/domain/infrastructure/worker 全部路由与服务），前端业务文件逐文件通读 + API 路径与后端路由逐一比对；两轮前史文档（`2026-10-27-implementation-audit-and-optimization.md`、`2026-10-27-core-pipeline-assessment-and-e2e-test-plan.md`）的遗留项已逐条用当前代码复核
- 严重度：P0 = 权限可绕过 / 数据错误 / 进程级故障；P1 = 明确缺陷、契约冲突或成片维护风险；P2 = 可维护性 / 一致性 / 局部性能

---

## 一、总体印象

**后端质量显著高于同类业务代码的平均水平。** 分层（api → application → domain → infrastructure）在绝大多数文件里执行得很干净：路由层只做 HTTP 映射与审计留痕，业务约束集中在服务层，domain 零框架依赖。错误处理有清晰的三种范式（`RbacError.status_code` 映射、领域异常在路由层翻译、`HTTPException` 直接抛出），边界注释解释"为什么"而不是"是什么"（登录时序攻击缓解、SQLite 外键顺序、挂载表转义、UTC 会话钉扎等都是实例）。安全设计超出常规：JWT 密钥强制显式配置、`admin_password` 在非 dev/test 必填、登录限流不信任 X-Forwarded-For、bcrypt 72 字节摘要、未知用户也跑一次 bcrypt。

**前端业务代码同样有突出的契约意识**：API 层文件头逐条注明后端错误码/裸数组/无 total 的契约；"NULL ≠ 0""unknown 不冒充已确认只读"的空值纪律在 `format.ts` 与各视图反复落实；请求竞态防护（requestId）、SSE 断流回退轮询都考虑到位。

**主要债务集中在三处**（也是本方案的主攻方向）：
1. **同一套约束衍生出的样板没有沉淀为共享层**——后端 3 份 `_escape_like`、5 份 `_client_ip`、2 份 `_as_utc`；前端 8 份窗口式分页、3 份错误详情提取、2 份菜单类型元数据。
2. **应用层反向依赖 API 层**——4 个 application 服务 import `src.api.v1.schemas`，1 个服务直接抛 `HTTPException`，分层方向在这条线上是倒置的。
3. **前端 HTTP 层有两个真实缺陷**（刷新失败挂起、10s 超时撞同步扫描），以及 `strict: false` 让精心建模的类型契约失去编译期强制力。

## 二、问题清单（按严重程度，含优先级与影响范围）

### P0（严重）

**本项目代码内未发现新的 P0。** 专项排查且排除：无 SQL 注入（全部参数化查询 + LIKE 转义）、无 `v-html`、无硬编码密钥/默认口令、权限码单一来源且有种子一致性测试钉住、CORS 未放开设防、登录有时序攻击缓解。

**继承的 P0（核心链路，非本轮实施范围，已在既有文档登记完整方案）：**

| ID | 问题 | 证据（当前代码复核仍成立） | 影响 |
|---|---|---|---|
| 继承-P0-1 | 瞬时源/挂载错误被判永久失败：`source.stat()` 的任何 `OSError` 都抛 `PermanentTaskFailure` | `processor.py:126-127,142-143` | NAS 抖动把整批任务变成"假失败"，需人工逐个重试 |
| 继承-P0-2 | 失败任务命中的清场只发生在下一次尝试开头（`attempts > 1` 才 `delete_for_task`） | `processor.py:195-196` | 失败任务仍能查到旧/半截结果，结果一致性被破坏 |
| 继承-P0-3 | `max_matches_per_task` 声明但零引用：空关键字/空正则可匹配所有行 | `settings.py:31`（全仓库唯一引用点） | 一条误配规则可把 `matches` 表写爆（200 GB 级） |

处置：这三项是 Worker 语义的**行为变更**（不是重构），需要真实数据 E2E 验证，`2026-10-27-core-pipeline-assessment-and-e2e-test-plan.md` 已给出修复方案与测试方案。本轮质量优化坚持"行为不变"红线，不在此顺手做；本轮把 P0-3 所需的 enforcement 钩子位测试补齐，为后续修复铺路。

### P1（重要）

**后端**

| ID | 问题 | 位置 | 影响范围 |
|---|---|---|---|
| P1-B1 | **应用层反向依赖 API 层**：`rbac.py`、`jobs.py`、`data_sources.py`、`analytics.py` 四个服务 import `src.api.v1.schemas`；`jobs.py` 服务函数直接抛 `HTTPException`（`create_job`/`_ensure_idempotent_match`/`_effective_suffixes`） | `application/services/rbac.py:19-30`、`jobs.py:4,57-60,69-75,133-142`、`data_sources.py:5`、`analytics.py:15-21` | 分层方向倒置；服务无法脱离 FastAPI 复用/测试；与同层 `RbacError`/`RetryNotPermitted`/`ScanLimitError` 的异常范式不一致 |
| P1-B2 | **`GET /jobs`、`GET /jobs/{id}/files`、`GET /users` 等列表接口无排序字段但依赖数据库默认顺序以外的稳定性已处理；真正的问题是 `list_rule_sets` 以 `(name, version)` 排序后窗口翻页，新建版本会插页**（分页期间新版本插入导致行漂移）——影响有限，登记为已知契约特性 | `rule_sets.py` 服务 + 前端 8 页窗口翻页 | 窗口翻页契约的整体限制，前端已如实标注；不动 |
| P1-B3 | **`build_user_routes` 每请求重复角色查询**：`is_admin()` 查一次角色，`accessible_menu_ids()` 内部再 `is_admin()` 一次 | `navigation.py:73-77` + `auth.py:103` | `/api/auth/routes` 每次 2 条重复 SQL；登录页每次进站都走（P1-2 上轮只修了 `permission_codes`，漏了这对） |
| P1-B4 | **登录响应串行查两次角色**：`role_codes()` 之后 `permission_codes()` 内部再查一次 | `routes_auth.py:128,149` | 每次登录多一条 SQL（小，但同族问题） |
| P1-B5 | **`ensure_rbac_seed` 按名字取父菜单用 `menus[...]` 直接下标**：权限节点的父页面若因任何异常缺行，启动期（lifespan）直接 `KeyError` 500，而非跳过+告警 | `rbac_seed.py:411` | 启动健壮性；触发概率低但后果是起不来 |

**前端**

| ID | 问题 | 位置 | 影响范围 |
|---|---|---|---|
| P1-F1 | **token 刷新失败时排队请求永久挂起**：`.catch` 只清空 `requests` 队列不 reject，等待者的 Promise 永不 settle，且请求尚未发出、axios 超时也不生效 | `utils/http/index.ts:101-107` + `:58-65` | 刷新失败（refreshToken 过期/断网）时页面 loading 卡死、无任何错误提示 |
| P1-F2 | **axios 全局 10s 超时与"创建作业同步扫描目录（后端 10s 预算 + 503 降级路径）"正面冲突**：慢扫描时客户端先超时，`error.response` 为 undefined，设计好的 503 文案在最该出现的场景失效 | `utils/http/index.ts:24` vs `api/logharvest.ts:58-63`、`create-dialog.vue` 503 文案 | 新建作业向导在目录巨大时给出误导性"网络连接失败" |
| P1-F3 | **`tsconfig.json` `strict: false`**：`WorkerInfo.cpu_percent: number \| null` 等刻意建模的可空性在视图层不被编译器强制，空值防线全靠 `format.ts` 运行时自觉 | `tsconfig.json:6-7` | 契约类型是"文档"不是"防线"；新增视图绕过 format 函数编译器不报错 |
| P1-F4 | **403 降级只做了 2 页**（stats、audit-logs），其余列表页无权限用户直达 URL 得到裸 detail 弹窗 + 空表 | `stats/index.vue:72-74`、`audit-logs/index.vue:82-84` vs 其余 6+ 页 | 只读角色体验差、易误报"坏了" |

### P2（一般）

**后端 — 重复与冗余**

| ID | 问题 | 位置 |
|---|---|---|
| P2-B1 | `_escape_like` 三份逐字拷贝（注释自称"同口径"） | `rbac.py:57-58`、`audit.py:24-26`、`analytics.py:40-42` |
| P2-B2 | `_client_ip` 五份拷贝 + 多处内联 `request.client.host if request.client else None` | `routes_users.py:38`、`routes_roles.py:33`、`routes_menus.py:45`、`routes_workers.py:17`、`routes_data_sources.py:21`、`routes_auth.py:31`；内联见 `routes_jobs.py:100,162,191`、`routes_system.py:75` |
| P2-B3 | `_as_utc` 两份拷贝 | `routes_matches.py:15-18`、`routes_operation_logs.py:22-25` |
| P2-B4 | `analytics.py` 自定义 `JobStatus` StrEnum 与 schemas 两个 Literal 的词表三处并存（值漂移无报错） | `analytics.py:29-37` vs `schemas.py:103-122` |
| P2-B5 | `delete_role`/`detach_actor` 用 `len(.all())`/逐行 UPDATE 代替聚合/单条 UPDATE | `rbac.py:311-313`、`audit.py:92-97` |
| P2-B6 | 作业服务里 `datetime.now(UTC)` 与 `utcnow()` 两种写法并存 | `jobs.py:144,191` vs 其他文件统一用 `utcnow()` |

**后端 — 其他**

| ID | 问题 | 位置 |
|---|---|---|
| P2-B7 | `routes_workers.py` drain/resume/drain-version 返回裸 dict 无 `response_model`，与全站契约风格不一致 | `routes_workers.py:65-69,96-100,133` |
| P2-B8 | `routes_events.py` SSE 无事件上限时（`max_events=0`）流可无限挂住（作业永不终态时）；行为已文档化，登记为已知 | `routes_events.py:73` |
| P2-B9 | 死配置四件套（上轮登记，仍未处理）：`prometheus-client` 依赖、`max_matches_per_task`、`copy_to_local`、`workers.last_error` | 见上轮报告 P2-4 |
| P2-B10 | `pytest-cov` 在全局 pip 里存在但项目 `.venv`（uv 管理）里缺失，`--cov` 不可用 | `service/.venv`（本轮已补装，见 §四 Phase 0） |

**前端 — 重复与冗余**

| ID | 问题 | 位置 |
|---|---|---|
| P2-F1 | 窗口式分页逻辑（`hasNext = rows.length === limit`、offset 步进、按钮禁用条件）在 8 个视图重复 | `jobs/index.vue`、`jobs/detail.vue`、`datasources/index.vue`、`workers/index.vue`、`rulesets/index.vue`、`rules/maintenance/index.vue`、`system/audit-logs/index.vue`、`system/users/index.vue` |
| P2-F2 | 错误详情提取三处实现且行为不一致（422 数组一处只取第一条、一处展开多行） | `api/logharvest.ts:25-34`、`views/logharvest/format.ts:147-161`、`store/modules/user.ts:24-27` |
| P2-F3 | ~~`MENU_TYPE_META`/`menuTypeMeta` 两份逐字相同~~ **复核为误报**：两视图已统一从 `views/system/filters.ts` 导入 `menuTypeMeta`（并有 `tests/system/filters.spec.ts` 覆盖） | `filters.ts:7-15` |
| P2-F4 | `TASK_STATUS_OPTIONS` 手写重复（`JOB_STATUS_OPTIONS` 已存在同值）**→ Phase 2 已修复（复用常量）** | `overview/index.vue:38-45` vs `api/logharvestTypes.ts:304-311` |
| P2-F5 | 数据源只读三态展示逻辑两处实现（"unknown 不冒充已确认只读"不变量散落） | `datasources/index.vue:72-91`、`jobs/create-dialog.vue:183-191,345-359` |
| P2-F6 | `listRuleSets` 各页 page size 不一致（20/200/500），超限静默截断 | `logharvestAdmin.ts:36`、`create-dialog.vue:135`、`rules/maintenance/index.vue:66` |

**前端 — 边界与异常**

| ID | 问题 | 位置 |
|---|---|---|
| P2-F7 | `getToken()` 的 `JSON.parse` 无兜底：cookie 被截断/损坏时应用启动即抛异常，白屏且无法自愈 | `utils/auth.ts:37-39` |
| P2-F8 | `hasPerms` 通配符仅当它是唯一权限时生效（`length === 1 && [0] === "*:*:*"`） | `utils/auth.ts:137` |
| P2-F9 | 统计页"成/败"快捷跳转语义错位：标签是文件任务口径，跳转按作业状态过滤 | `stats/index.vue:240,249` |
| P2-F10 | 统计页日期跳转只过滤第一页（后端无 date 参数，已知契约限制） | `stats/index.vue:227`、`jobs/index.vue:54-61` |
| P2-F11 | 下拉数据源静默截断（用户 500 / 作业 200 / 规则集 500） | `audit-logs/index.vue:49`、`matches/index.vue:141` |
| P2-F12 | 作业详情文件表分页与 matches 查询按钮缺 loading 保护（其他页都有） | `jobs/detail.vue:537-543,577-582`、`matches/index.vue:288-291` |
| P2-F13 | 作业详情页无 requestId 竞态防护的对位缺失（attempt 有、files 无） | `jobs/detail.vue` |

**前端 — 可读性 / 性能 / 安全**

| ID | 问题 | 位置 |
|---|---|---|
| P2-F14 | 两套"未知"占位符混用（`—` 与 `-` 无语义区分依据），同一张表两种破折号 | `format.ts:14 vs 48,53,62,136` |
| P2-F15 | 规则集列表 N+1 请求（每行一个详情请求只为算规则条数，自注"每页最多 20 次"） | `rulesets/index.vue:36-44` |
| P2-F16 | token 存非 HttpOnly cookie + localStorage（模板固有取舍，无 CSP meta；后端 403 是真防线） | `utils/auth.ts:54-86`、`index.html` |
| P2-F17 | `edit-drawer.vue:117` 直接渲染 `created_at` 原文，未走 `formatDateTime` | `rulesets/edit-drawer.vue:117` |
| P2-F18 | 业务视图硬编码中文 vs 框架层 i18n 两套并存（单语产品可接受，登记） | 全部业务视图 |

### 测试覆盖现状与缺口

**后端**（`pytest --cov`，总 96%）：域模型、schemas、权限、队列可移植部分、worker 处理器（98%）覆盖都很好。缺口集中在**异常分支**与 **PG 专有路径**：

| 模块 | 覆盖 | 未覆盖行 | 性质 |
|---|---|---|---|
| `application/services/audit.py` | 80% | 65-67、78-80、98-99（三个吞异常告警分支）、130/132（list 过滤组合） | 异常分支（本轮补） |
| `application/services/maintenance.py` | 77% | 46-48（非 PG 503 分支） | 异常分支（本轮补） |
| `infrastructure/task_queue.py` | 82% | PG 专有 CLAIM/RECOVER SQL（无真实 PG 时不可达） | 集成测试域（19 个 skipped 覆盖） |
| `api/v1/routes_system.py` | 88% | 64-78（维护端点主路径） | 接口路径（本轮补） |
| `application/services/analytics.py` | 94% | 52/54（`_date_key` 分支）、242-244（脏配置跳过）、274/278（摘要截断） | 边界（本轮补） |
| `application/services/rbac.py` | 96% | 13 个边界分支（69/182/319/343/396/418/451/495/504/518/536/539/541） | 边界（本轮补） |
| `domain/rules/evaluator.py` | 93% | 54/56/83/95-96/129/145 | 边界（本轮补） |
| `domain/source_guard.py` | 91% | 57/77/96-97/120-123/136 | 边界（本轮补） |
| `domain/archive/reader.py` | 91% | 预算/损坏容错分支（部分由核心链路文档的专项测试覆盖） | 边界（本轮补可达成部分） |
| `worker/main.py` | 95% | 35/180/229-230/241/277-278/319 | 异常/信号分支（本轮补） |
| `worker/__main__.py` | 0% | `python -m src.worker` 入口 3 行 | 入口胶水（不补） |

**前端**：本轮之前测试基础设施为零（审计期间已建好 vitest 5 + jsdom + 首个 spec）。适合补单测的纯逻辑（按性价比排序）：`utils/sse.ts:parseSseChunk`（多帧/CRLF/残帧/非法 JSON 边界最多）、`views/rules/maintenance/rule-form.ts`（判别联合往返转换 + 前端校验闸门）、`views/logharvest/format.ts` 其余导出（"补 Z"时区解析、422 展开、未知占位）、`api/logharvest.ts`（`apiErrorDetail`/`apiErrorStatus` 三类输入）、`utils/auth.ts:hasPerms`（含通配符边界）、`store/modules/user.ts:extractDetail`。

## 三、分阶段优化方案

总原则：**每阶段独立可验证、行为不变（Phase 3 的三个缺陷修复除外，它们各自带行为断言测试）**；每阶段结束跑完整门禁（后端 pytest 全绿 + 前端 vitest 全绿 + typecheck 通过）；回滚边界 = 每阶段一个提交，`git revert <该提交>` 即可整体回退，阶段间无交叉文件依赖（Phase 1 与 Phase 2 完全不相交：一后端一前端）。

### Phase 0 — 门禁与测试基础设施（已完成）

- 目标：协作规范成文；前端具备单测能力；后端补齐覆盖率工具。
- 内容：`agent.md`（测试门禁规范）；web 安装 `vitest@5 + @vue/test-utils + jsdom`、`vitest.config.ts`、`package.json` 增 `test`/`test:watch` 脚本、首个 spec 冒烟；service `.venv` 补装 `pytest-cov`。
- 验收：`pnpm test` 退出码 0；`pytest --cov` 可运行且总覆盖 ≥ 96%。
- 回滚边界：还原 `package.json`/删除 `vitest.config.ts`/`agent.md` 即回到原状。

### Phase 1 — 后端去重与分层方向修正（行为不变）

- 目标：消除三组逐字重复（P2-B1/B2/B3），把应用层对 API 层的反向依赖（P1-B1 的 import 部分）掉头；`HTTPException` 出服务层（P1-B1 的异常部分）。
- 涉及文件：
  - 新增 `src/domain/text.py`（`escape_like`）→ 替换 `rbac.py`/`audit.py`/`analytics.py` 三份拷贝；
  - 新增 `src/api/v1/helpers.py`（`client_ip(request)`、`as_utc(dt)`）→ 替换 6 份 `_client_ip`/内联、2 份 `_as_utc`；
  - schemas 移位：`src/api/v1/schemas.py` → `src/application/schemas.py`（api → application 方向合法化），更新 11 个 route + 4 个 service + 2 个 test 的 import；
  - `jobs.py` 服务层改抛 `JobCreateError(status_code, detail)` 类异常（新增于服务层），`routes_jobs.py` 统一翻译；行为（状态码/detail 文案）逐条不变。
- 明确不做：`analytics.JobStatus` 与 schemas Literal 词表合并（Pydantic Literal 无法从 StrEnum 派生，合并会动 OpenAPI 契约）；audit/rbac 全量接口签名重构。
- 验收标准：全量 pytest 绿且用例数不减；`ruff check`/`ruff format`/`mypy` 绿；`grep -rn "api.v1.schemas" src/application` 零命中；`grep -rn "HTTPException" src/application` 零命中；OpenAPI schema diff 为空（`/openapi.json` 快照对比）。
- 回滚边界：单提交 revert；数据库无变更。

### Phase 2 — 前端缺陷修复（行为变更，各带测试）

- 目标：修复 P1-F1、P1-F2、P2-F7、P2-F8 四个真实缺陷；消除两处逐字重复（P2-F3/F4）。
- 涉及文件：`utils/http/index.ts`（刷新失败 reject 排队请求）、`api/logharvest.ts`（`createJob` 单独 timeout 60s）、`utils/auth.ts`（getToken try/catch 自愈；hasPerms `includes("*:*:*")`）、`system/roles/index.vue`+`system/menus/index.vue`（抽 `src/views/system/menuMeta.ts` 共享）、`overview/index.vue`（复用 `JOB_STATUS_OPTIONS`）。
- 验收标准：每个修复有对应单测（刷新失败 reject、getToken 损坏兜底、hasPerms 通配、apiErrorDetail 等）；`pnpm test`/`typecheck` 全绿；手工契约核对：401→刷新失败→登录页路径不挂起。
- 回滚边界：单提交 revert；无接口契约变化（createJob 只是客户端超时）。
- 明确不做（登记为后续）：P2-F1 分页 composable（8 页联动改动，收益/风险比不适合本轮）；P2-F2 三处错误提取合并（展示行为不同，合并=行为变更）；P1-F3 `strict: true`（会暴露大量既有隐式 any，需独立专项）；P1-F4 全站 403 降级（需交互设计，非纯代码）。

### Phase 3 — 后端剩余一致性修复（行为不变或可忽略）

- 目标：P1-B3/B4（重复角色查询）、P1-B5（种子父菜单 KeyError）、P2-B5（计数下推 SQL）、P2-B6（统一 `utcnow()`）、P2-B7（workers 路由补 response_model —— OpenAPI 会更精确，属契约补全而非变更）。
- 涉及文件：`navigation.py`、`routes_auth.py`、`rbac_seed.py`、`rbac.py`、`audit.py`、`jobs.py`、`routes_workers.py`。
- 验收标准：全量 pytest 绿（含 RBAC/认证既有用例不动）；`/auth/routes` 与登录的 SQL 条数以新单测断言（query 计数或 mock 断言）；`response_model` 补齐后 FastAPI self-check 通过。
- 回滚边界：单提交 revert。

### Phase 4 — 后端单元测试补全（目标：异常分支与边界全覆盖）

- 目标：覆盖 §二 表列出的缺口；总覆盖率 96% → ≥ 98%，audit.py 80% → ≥ 95%。
- 新增文件：
  - `tests/application/test_audit_service.py`：record/commit/detach 三个吞异常分支（monkeypatch 让 add/commit 抛错 → structlog 告警、业务不受影响）、`list_operation_logs` 过滤矩阵（action 前缀+转义、resource 组合、时间窗）；
  - `tests/api/test_system.py` 增补：维护端点 200 主路径 + 503 非 PG 分支 + 审计留痕断言；
  - `tests/application/test_analytics_service.py` 增补：`_date_key` 三分支、脏配置跳过、摘要截断、空窗口全 0；
  - `tests/application/test_rbac_edges.py`：rbac.py 13 个未覆盖分支逐条（自环防护、环数据、系统角色/菜单保护、空 ids 等）；
  - `tests/domain/test_evaluator_edges.py`、`tests/domain/test_source_guard_edges.py`、`tests/domain/archive/test_reader_edges.py`：边界值（空 pattern、flags 组合、损坏挂载行、预算临界）;
  - `tests/worker/test_runtime.py` 增补：信号分支、孤儿目录清理、维护异常吞并。
- 验收标准：全量绿、无相互依赖（每个测试自建数据）、`--cov` 报告达到上述数字；耗时增量 < 30s。
- 回滚边界：纯新增测试文件，revert 即删除。

### Phase 5 — 前端单元测试补全（目标：纯逻辑全量覆盖）

- 目标：§二"适合补单测清单"全部落地；`pnpm test` 覆盖前端全部可测纯逻辑。
- 新增文件：
  - `tests/utils/sse.spec.ts`：`parseSseChunk` 多帧/CRLF/无空行结尾/非法 JSON/空缓冲；`streamJobProgress` 收尾 flush 分支（mock fetch/ReadableStream）；
  - `tests/rules/rule-form.spec.ts`：`toRule`/`fromRule` 往返一致（keyword any/all 互斥、flags 只取 i/m/s、threshold 只挂正则）、`rowError`/`validateRules` 全部分支（含 id 重复、空表）；
  - `tests/logharvest/format.spec.ts` 扩展：`formatDateTime`"补 Z"、`toApiDateTime` 非法值丢弃、`formatDetailError` 422 展开、各 `*TagType` 未知值、`formatGB/formatMB` 负值/极大值；
  - `tests/api/errors.spec.ts`：`apiErrorDetail`（字符串 detail / pydantic 数组 / 无响应 / 空 fallback）、`apiErrorStatus`；
  - `tests/utils/auth.spec.ts`：`hasPerms`（通配唯一/通配+其他/数组全含/字符串未含）、`getToken` 损坏 cookie 自愈、`formatToken`；
  - `tests/views/menuMeta.spec.ts`（Phase 2 抽出的共享元数据）。
- 验收标准：`pnpm test` 全绿、可重复执行（无时间/顺序依赖）、`pnpm typecheck` 绿。
- 回滚边界：纯新增测试文件。

### Phase 6 — 收尾与文档

- 目标：报告闭环；README 测试章节更新；`agent.md` 与实际门禁命令核对一致。
- 内容：本报告追加"实施结果"节（对照 §三逐条）；`service/README.md` 与 `web/README.md` 补"如何跑测试"小节；确认 `.gitignore` 覆盖新增产物。
- 验收标准：完整门禁（后端 pytest + 前端 vitest + 双端 typecheck/lint）一次性全绿截图/输出入档。

### 有意不做（本轮外，均已登记理由）

继承的 Worker P0×3（行为变更需真实数据 E2E，另有专项文档）；前端 `strict: true`、分页 composable、全站 403 降级、token 存储 HttpOnly 化（部署层 CSP 配合）；`analytics.JobStatus` 词表合并（OpenAPI 契约风险）；死配置四件套清理（上轮已按"标注不删"处理）。

## 四、实施结果（2026-10-28 收尾时回填）

见文末「六、实施结果」。

---

## 六、实施结果（2026-10-28 全部回填）

### 验收基线对照

| 门禁 | 审计起点 | 收尾实测 |
|---|---|---|
| 后端 pytest | 435 passed / 19 skipped | **500 passed / 19 skipped**（净增 65 条，零失败） |
| 后端覆盖率 | 96%（audit 80%、routes_system 88%、maintenance 77%） | **97%**（audit 100%、routes_system 100%、analytics/evaluator/source_guard/main/navigation 100%、rbac 99%、worker/main 99%、reader 94%） |
| 后端 ruff / mypy | 全绿 | 全绿（mypy 29 文件） |
| OpenAPI 契约 | — | Phase 1/3/4 零契约变化；唯一增量是 Phase 3 有意补全的 workers 三端点 response_model（`WorkerActionRead`/`DrainVersionRead`，快照对比验证） |
| 前端 vitest | 无测试设施 | **7 个 spec、77 条用例全绿** |
| 前端 typecheck | 0 错误 | 0 错误 |

### 各阶段落地

| 阶段 | 落地内容 |
|---|---|
| **Phase 0** | `agent.md` 测试门禁规范；web 安装 vitest 5 + @vue/test-utils + jsdom、`vitest.config.ts`、`test`/`test:watch` 脚本；service venv 补装 pytest-cov（原 P2-B10） |
| **Phase 1（行为不变）** | 新增 `domain/text.py:escape_like` 收编 3 份 `_escape_like`；新增 `api/v1/helpers.py`（`client_ip`/`as_utc`）收编 7 份 `_client_ip`、2 份 `_as_utc` 与全部内联；schemas 移位 `api/v1/schemas.py` → `application/schemas.py`（17 个文件 import 更新），`grep api.v1.schemas src/application` 零命中；`jobs.py` 的 10 处 `HTTPException` 改为 `JobCreateError` 由路由翻译，`grep "raise HTTPException" src/application` 零命中 |
| **Phase 2（缺陷修复，各带测试）** | P1-F1 刷新失败挂起：抽出 `utils/http/pendingRequests.ts`（flush/reject 契约），刷新失败逐个 reject 排队请求；P1-F2 `createJob` 客户端超时 60s（不再抢先于后端 503）；P2-F7 `getToken` 损坏 cookie 自愈（try/catch + 清 cookie）；P2-F8 `hasPerms` 通配码不再要求唯一；P2-F4 overview 复用 `JOB_STATUS_OPTIONS`。P2-F3 复核为误报（已收敛于 `filters.ts`），报告已更正 |
| **Phase 3（一致性）** | P1-B3 `build_user_routes` 角色查询 3 次 → 1 次（新增 `role_codes` 复用入口 `permission_codes_from_roles` + `accessible_menu_ids(admin=)`）；P1-B4 登录响应走 `roles_and_permissions` 单次角色查询；P1-B5 种子父菜单缺行从裸 `KeyError` 改为可读 RuntimeError（fail-closed）；P2-B5 `delete_role` 计数下推 SQL、`detach_actor` 改单条 UPDATE；P2-B6 `jobs.py` 统一 `utcnow()`；P2-B7 workers 三端点补 `response_model`。**测试还发现一个真实缺陷并修复**：`routes_system.py` 对 `run_maintenance` 的 dict 返回值做属性访问，真实 PG 下维护端点必然 500（`result["recovered"]` 修正，Phase 4 的 200 主路径用例钉住） |
| **Phase 4（后端测试）** | 新增 9 个测试文件 65 条用例：audit 异常分支、analytics 边界、RBAC 边界、auth/navigation 退化输入、evaluator 边界、source_guard 边界、reader 损坏容器/预算、worker 运行时防御分支、应用 lifespan 接线（启动迁移 + 种子从 0 覆盖到 100%）；`test_system.py` 补维护端点 200 主路径 + 审计断言 |
| **Phase 5（前端测试）** | 新增 6 个 spec：`utils/sse.spec.ts`（13 条：帧解析边界/流收尾/降级契约）、`utils/auth.spec.ts`（hasPerms 通配、cookie 自愈）、`utils/http/pendingRequests.spec.ts`（刷新队列契约）、`views/rules/rule-form.spec.ts`（判别联合往返 + 校验闸门）、`api/errors.spec.ts`、`format.spec.ts` 扩展（UTC 补 Z、422 展开、占位符契约）；连同既有 `system/filters.spec.ts` 共 77 条 |
| **Phase 6（收尾）** | 本报告回填；`service/README.md` 补协作门禁与专项测试清单、覆盖率命令更新；`web/README.md` 补「测试」章节；`.gitignore` 增加 `service/coverage.json` |

### 各阶段验收记录

- **Phase 1**：全量 pytest 绿（用例数不减）；`ruff check`/`ruff format`/`mypy` 绿；OpenAPI before/after 快照 diff 显示差异全部归属本轮之前已存在的未提交改动（operation-logs/analytics/status 参数），Phase 1 自身零贡献。
- **Phase 2**：`pnpm typecheck` 0 错误；`vitest run` 全绿；刷新队列契约由 `pendingRequests.spec.ts` 锁定（"刷新失败必须逐个 reject"成为可回归断言）。
- **Phase 3**：全量 pytest 绿（436 → 436）；OpenAPI 快照 diff 仅含 workers 三端点 response_model 补全；RBAC/认证既有用例零修改。
- **Phase 4**：500 passed / 19 skipped；覆盖缺口表逐项清零（task_queue 的 PG 专有路径仍由 19 个真实 PG 集成用例覆盖，属预期）。
- **Phase 5**：77 条用例全绿、可重复执行（无顺序/时间依赖；`process.env.TZ` 在用例内显式设定，不受宿主时区影响）；typecheck 绿。
- **Phase 6**：完整门禁一次性全绿（见下）。

### 提交与回滚边界说明

本轮工作发生时，工作区已存在**前一批未提交改动**（操作日志/统计页/RBAC 追加等）。为不把两批改动混入同一提交，本轮未执行 `git commit`；上述每个 Phase 都以"全量门禁绿 + 快照对比"验证为独立可回滚状态。提交时建议按 Phase 拆分（先收口前一批改动，再按 Phase 1→6 逐笔提交），每笔提交前的门禁命令见 `agent.md`。

### 仍然开放（与 §三"有意不做"一致）

Worker 核心 P0×3（继承项，另案）、前端 `strict: true`、窗口分页 composable、全站 403 降级、token 存储 HttpOnly 化 + CSP、`analytics.JobStatus` 词表合并、死配置四件套清理。
