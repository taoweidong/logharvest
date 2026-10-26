# LogHarvest 文档↔代码对照审计与代码质量评估报告（2026-10-27）

- 日期：2026-10-27
- 范围：`docs/**` 全部 12 份文档 vs `service/`（后端 58 个源文件）+ `web/`（前端 141 个 ts/vue）
- 基线：后端 `pytest -q` **404 passed / 16 skipped**，`ruff check` + `ruff format --check` 全绿，`mypy` 27 文件无问题；前端 `pnpm typecheck` + `pnpm build` 通过
- 术语：P0 = 会让仓库状态丢失 / 权限模型被绕过 / 进程崩溃；P1 = 明确偏离文档或存在可维护性、性能硬缺口；P2 = 文档债、格式漂移、残留产物
- 与 2026-10-26 报告的关系：上一轮已闭环的 P0-1（解压预算）、P0-2（主循环护栏）、P1-1～P1-5、P2-2～P2-7 本次复核**仍在位**，不重复列为问题；本报告聚焦新增发现与仍未闭环的部分

---

## 一、进度总结

### 1.1 项目核心目标（提炼自主设计 §1、§2）

LogHarvest 是**压缩日志规则提取平台**：面向企业 NAS 上的海量压缩日志（单批 500 GB / 1000+ 个压缩包 / 千万级命中），管理员在 Web 上配置数据源与规则集、指定本次要分析的 NAS 目录，Server 递归扫描拆成文件任务，多 Worker 并发复制 → 流式解压 → 逐行规则匹配 → 结果入库，最后在 Web 上查进度、重试历史、失败原因与命中结果。

三条不可妥协的设计意图：

1. **PostgreSQL 是唯一事实源**——元数据、任务队列（`file_tasks` + `SKIP LOCKED`）、命中结果同库，不引入 Celery/ES/ClickHouse。
2. **NAS 原始数据全程只读**——不删、不移、不重命名、不写入；写操作只发生在本地临时盘与数据库。
3. **任务可观测、可重试、可排障**——尝试历史、心跳、退避、僵死回收、幂等栅栏全部落库。

### 1.2 文档清单与实际用途

| 文档 | 性质 | 状态 |
|---|---|---|
| `specs/2026-10-24-compressed-log-extraction-design.md` | 主设计（15 节） | 已评审，M0–M2c 全部依此实现 |
| `specs/2026-10-24-technology-selection-evaluation.md` | 选型评估 | 结论已落到主设计 §3 |
| `specs/archive/2026-10-24-...-detailed.md` | 详细版归档 | 被主设计取代 |
| `specs/2026-10-26-logharvest-m3-multiline-records-design.md` | M3 多行记录设计 | 已评审，**代码零实现** |
| `plans/2026-10-24-m0-core.md` | M0 计划 | 已完成 |
| `plans/2026-10-25-m1a-control-plane-api.md` | M1a 计划 | 已完成 |
| `plans/2026-10-25-m1b-worker-queue.md` | M1b 计划 | 已完成 |
| `plans/2026-10-25-m2a-backend-auth-ops.md` | M2a 计划 | 已完成 |
| `plans/2026-10-25-m2b-frontend-pure-admin.md` | M2b 计划 | 已完成 |
| `plans/2026-10-25-m2c-safety-observability.md` | M2c 计划 | 已完成 |
| `plans/2026-10-26-m3-multiline-records.md` | M3 计划 | 未开始 |
| `2026-10-26-implementation-review-and-optimization.md` | 上一轮审计报告 | 已执行完毕 |

### 1.3 里程碑完成情况

| 里程碑 | 计划文档 | 状态 | 说明 |
|---|---|---|---|
| M0 核心引擎 | m0-core | ✅ | 规则引擎、归档读取、Worker 处理器、7 张表 |
| M1a 控制面 API | m1a | ✅ | 数据源/规则集/作业/文件任务/结果/系统状态 + 幂等键 |
| M1b Worker/队列 | m1b | ✅ | PG 队列、常驻循环、SSE、drain/resume |
| M2a 后端认证运维 | m2a | ✅ | JWT/RBAC、Alembic、取消/永久失败/手动重试、概览/维护 |
| M2b 前端五屏 | m2b | ✅ | 登录、概览、作业+SSE、规则集、数据源、结果游标、Worker |
| M2c 安全可观测 | m2c | ✅ | 只读守卫、尝试历史、资源上报、structlog、前端抽屉 |
| M3 多行记录 | m3-design + m3-plan | ⬜ 未开始 | 设计已评审，代码零实现 |
| **RBAC 菜单/权限（新增，无文档）** | — | ⚠️ 已实现未收口 | 见 §1.5 |

### 1.4 已完成功能（按层）

| 层 | 已交付 |
|---|---|
| domain | 规则模型与编译/评估（关键字、正则、门限、字段提取）、归档流式读取 + 解压预算、只读挂载判定、口令、JWT、登录限流 |
| application | 作业规划与扫描、任务/结果查询、取消/重试/维护、规则集版本、认证与角色解析、**RBAC 管理（用户/角色/菜单）、菜单树→动态路由** |
| infrastructure | SQLModel 引擎与建表、`SKIP LOCKED` 任务队列、批量结果写入、Alembic 迁移（0001–0004） |
| api | 认证 4 端点 + 数据源/规则集/作业/文件任务/尝试历史/结果/Worker/系统 + **用户/角色/菜单管理** |
| worker | 常驻循环、领取/心跳/租约/退避、结构化日志、资源上报、解压预算 |
| web | 登录、概览、作业列表与详情（SSE）、规则集、数据源（只读三态）、命中结果、Worker、**系统管理三屏** |

### 1.5 与文档存在偏差 / 尚未实现的部分

| # | 偏差 | 文档依据 | 实际 |
|---|---|---|---|
| D1 | **实现并突破了文档"明确不做"的项**：`sys_menus`、按钮级权限、服务端动态菜单 | M2a §6.1、M2b §2.3/§9.1 均列为"不做" | 已实现：`menus`/`role_menus` 表（迁移 0004）、18 个权限码、3 个管理端点族、`GET /api/auth/routes` 返回真实菜单树 |
| D2 | 前端菜单来源由"静态定义"改为"服务端下发" | M2b §2.3「菜单和路由完全静态定义，不请求服务端路由合并」 | `router/modules/logharvest.ts` 已删除，`initRouter()` 改为拉 `/api/auth/routes` |
| D3 | 登录响应 `permissions` | M2a §2.0 只要求字段存在 | 现在是真实权限码列表（非 `[]`） |
| D4 | 上述 RBAC 改造**没有设计/计划文档**（`service/README.md` 有操作说明，但没有 `docs/superpowers/**` 下的设计与验收记录） | — | 代码已落地，范围扩大的评审链缺失 |
| D5 | M3 多行记录 | m3-design §12 | 零实现 |
| D6 | `matches` 按月分区 + 结果保留期 | 主设计 §6.6/§8.3 | 未实现 |
| D7 | 单机 4–6 Worker + 内存压力自适应降并发 | 主设计 §7.2/§8.2 | 只有部署约定，代码不检查内存 |
| D8 | `max_matches_per_task` 强制 | 主设计 §8/§11 | `settings.py:31` 有字段，零引用 |
| D9 | Prometheus 指标 | 主设计 §3/§10.2 | `pyproject.toml` 声明依赖，零 import |
| D10 | `data_sources.copy_to_local` 开关 | 主设计 §6.1 | 列可读写，Worker 无条件复制 |
| D11 | `workers.last_error` 写入 | 主设计 §6.7 | 列存在，无写入方 |
| D12 | 样本试跑 `POST /api/rule-sets/test` | 主设计 §5.3/§9.2 | 未实现 |
| D13 | `GET /api/workers/{id}` Worker 详情 | 主设计 §9.2 | 未实现 |
| D14 | Monaco / JSON 模式规则编辑 | 主设计 §5.3 | 未实现（M2b 已确认有意取舍） |
| D15 | 告警（最老 pending >10 min 等） | 主设计 §7.4 | 未实现（依赖 Prometheus） |

---

## 二、问题清单（按严重程度从高到低）

### P0-1 仓库处于"大规模改动已暂存但未提交"状态，且审计起点全量测试是红的

- **位置**：`git status`——48 个文件（41 改 + 7 新增）`git add` 后停在索引里；`service/tests/api/test_rule_sets.py`、`test_users_api.py` 另有未暂存的修复
- **现状**：本次审计第一次 `pytest -q` 得到 **3 failed / 394 passed**（`test_rule_set_conflict_returns_409_and_rolls_back`、`test_admin_can_crud_users`、`test_last_active_admin_is_protected`），失败原因正是"路由已换成 `require_permission`，测试仍在覆盖旧依赖 / 仍用 `PUT /users/{id}` 改角色"；修复只存在于未暂存的工作区里
- **影响**：这批改动（含迁移 0004、18 个权限码、3 个管理端点族、前端系统三屏）**没有任何提交记录**；任何人切分支、`git stash`、`git restore --staged` 都会让它消失。且"改完未跑全量"意味着提交前无人能确认它是否真绿

### P0-2 权限模型迁移后留下 4 个死符号，是"绕过权限码"的现成捷径

- **位置**：`service/src/api/dependencies.py:113-144`（`require_admin_user`、`AdminDep`、`require_admin_streaming_user`、`AdminStreamDep`）
- **现状**：路由全部改为 `require_permission(...)` / `require_permission_streaming(...)` 后，这四个符号**零引用**（唯一残留引用是 `test_rule_sets.py` 里一行已经不再生效的 `dependency_overrides`）
- **影响**：新接口随手 `AdminDep` 就能绕过权限码体系，退回"管理员万能"；且 `require_admin_streaming_user` 的文档字符串仍在教人用 SessionDep 占连接。留着即是误用入口

### P1-1 18 个权限码是散落的字符串字面量，没有单一来源

- **位置**：`service/src/api/v1/routes_{jobs,rule_sets,data_sources,matches,workers,system,events,users,roles,menus}.py` 共 21 处 `require_permission("...")`，以及 `application/services/rbac_seed.py:125-182` 的 `PERMISSIONS` 种子（同一批字符串的第三份拷贝）
- **现状**：权限码写错**没有任何编译期或启动期报错**——只表现为某个接口对所有人 403，或对管理员以外所有人 403；种子与路由一旦不同步，界面上能看到菜单但点开就 403
- **影响**：这是本次新增的最大可维护性风险面；RBAC 每加一个权限都要在 3 处手抄同一个字符串

### P1-2 权限解析每请求多跑一次重复的角色查询

- **位置**：`service/src/application/services/auth.py:64`（`permission_codes` 先调 `is_admin`，`is_admin` 再调 `role_codes`）
- **现状**：`require_permission` 是**每个业务请求**都要走的依赖，实际发出 2 条 SQL（角色查询 ×2，管理员还要再算一次才返回通配），其中第一条纯粹是重复劳动
- **影响**：列表类接口与 SSE 轮询都会放大这条开销；权限码体系上线后，所有请求都比改造前多一次 DB 往返

### P1-3 RBAC 功能零文档，且直接落地了文档明确排除的范围

- **位置**：`docs/superpowers/**` 下无对应文档；代码见 `domain/models/rbac.py`、`application/services/{rbac,rbac_seed,navigation}.py`、`api/v1/routes_{users,roles,menus}.py`
- **现状**：`service/README.md` 有操作级说明（端点表、种子行为、403 语义），但**没有规格与验收记录**：菜单三态模型、权限码命名规范、admin 通配语义、内置菜单不可改字段、动态路由 `component` 契约、前后端升级顺序都只活在代码与注释里；而 M2a §6.1、M2b §2.3/§9.1 白纸黑字写了"不做 sys_menus、按钮权限、动态菜单"
- **影响**：范围扩大这件事没有评审签字；后续维护者只能读代码反推契约，改错的概率高（前端 `component` 字符串与 `src/views` 的映射一旦漂移就是白屏）

### P1-4 前端动态路由的组件解析靠模糊匹配，后端改一个 path 就白屏

- **位置**：`web/src/router/utils.ts` 的 `findPageComponent()`（先精确匹配 `/views/{x}.vue|tsx`，退回 `key.includes(normalized)`）与 `addAsyncRoutes()`
- **现状**：后端 `menus.component` 是 `"logharvest/jobs/index"` 这类字符串；前端按它去 `import.meta.glob` 的 key 里找，找不到就兜底 `EmptyLayout` 并打印 error
- **影响**：菜单数据由数据库驱动后，"component 写错"从编译期错误变成了运行期白屏；且旧静态路由文件 `router/modules/logharvest.ts` 已删除，静态兜底也不存在了

### P2-1 `ruff format` 漂移

- **位置**：`service/` 下 11 个文件（`ruff format --check` 报告）
- **影响**：CI 若只跑 `ruff check` 会漏；下一个人改这些文件会带上无关 diff

### P2-2 `.workbuddy/` 未进 `.gitignore`

- **位置**：仓库根 `.gitignore`
- **影响**：`git status` 长期噪音，容易被误 `git add .` 提交

### P2-3 工作区残留产物

- **位置**：`service/logharvest.db`（140 KB 开发库）、`web/dist/`（构建产物）
- **影响**：按"测试/构建后清理产物"的约定应清掉；`web/dist` 还会让 `pnpm build` 前的 `rimraf` 在大目录下失败

### P2-4 四个"声明但无行为"的死配置（延续上一轮，仍未处理）

| 项 | 位置 | 说明 |
|---|---|---|
| `prometheus-client` | `service/pyproject.toml` | 依赖已装，零 import |
| `max_matches_per_task` | `config/settings.py:31` | 有字段，零引用 |
| `data_sources.copy_to_local` | `entities.py:26` | 列可读写，Worker 无条件复制 |
| `workers.last_error` | `entities.py:185` | 列存在，无写入方 |

### P2-5 登录限流是进程内计数（已知限制）

- **位置**：`domain/services/rate_limit.py`、`api/dependencies.py:53-72`
- **影响**：多副本部署时实际放行量 = 阈值 × 副本数。上一轮已写进 README，此处仅登记

---

## 三、优化方案与实施优先级

| 优先级 | 问题 | 方案 | 落地位置 | 风险 |
|---|---|---|---|---|
| **P0-1** | 未闭环改动 | 先让全量测试转绿，再按"RBAC 改造 / 本次优化 / 文档"拆成独立提交并推送 | 仓库 | 低（绿了才提交） |
| **P0-2** | 死权限符号 | 删除 4 个零引用符号；同步清掉测试里失效的 `require_admin_user` override | `api/dependencies.py`、`tests/api/test_rbac_core.py` | 低 |
| **P1-1** | 权限码散落 | 新增 `domain/permissions.py` 作为唯一来源（`Permissions` 常量 + `ALL_PERMISSION_CODE`），10 个路由文件与种子全部改为引用常量；补一条"声明的权限码必须都被种子创建"的用例钉住 | 新增 `domain/permissions.py`、10 个 route、`rbac_seed.py`、`tests/api/test_rbac_core.py` | 低（纯替换，字符串值不变） |
| **P1-2** | 重复角色查询 | `permission_codes` 内部只查一次角色，再判断是否 admin | `application/services/auth.py` | 低 |
| **P1-3** | RBAC 无文档 | 补一份设计说明（菜单三态模型、权限码契约、admin 通配、动态路由契约、升级顺序），并写入 `service/README.md`；同时在本报告 §1.5 明确"范围已扩大、需补评审" | `docs/`、`service/README.md` | 无 |
| **P1-4** | 组件模糊匹配 | **本轮不改动**（改映射策略要动前端路由注册，风险高于收益）；改为在 README 与前端 error 文案里写清"component 必须与 `src/views` 下的相对路径一致"，并保留兜底告警 | 文档 | 无 |
| **P2-1** | 格式漂移 | `ruff format` 全部文件 | — | 无 |
| **P2-2** | 噪音 | `.gitignore` 增加 `.workbuddy/` | `.gitignore` | 无 |
| **P2-3** | 残留产物 | 删除 `service/logharvest.db`、`web/dist/` | — | 无 |
| **P2-4** | 死配置 | 不删除（文档里有位置），维持 README 标注 + 本报告登记为 M3 待办 | 文档 | 无 |

### 实施顺序

1. P0-1（先转绿）
2. P0-2 删除死依赖
3. P1-1 权限码常量化
4. P1-2 权限解析减一次查询
5. P2-1 / P2-2 / P2-3 收尾
6. P1-3 文档（含本报告）
7. 提交并推送

每步之间跑一次 `pytest -q`，确认基线不回退再进下一步。

---

## 四、实施结果（2026-10-27）

| 基线（审计起点） | 收尾 |
|---|---|
| `pytest -q` 3 failed / 394 passed / 16 skipped | **404 passed / 16 skipped**（净增 10 条） |
| `ruff check` 绿、`ruff format --check` 11 文件待格式化 | `ruff check` + `ruff format --check` 全绿（104 文件） |
| `mypy` 27 文件 Success | `mypy` 27 文件 Success |
| 前端 `pnpm typecheck` / `pnpm build` 通过 | 同左（另含系统管理三屏） |

| 项 | 落地内容 |
|---|---|
| **P0-1** | 全量转绿后按三笔提交收口：RBAC 改造（含迁移 0004 与系统三屏）、权限/依赖优化、文档 |
| **P0-2** | 删除 `require_admin_user` / `AdminDep` / `require_admin_streaming_user` / `AdminStreamDep`；`require_permission_streaming` 的文档字符串改为自解释（不再引用已删除的函数）；`test_rule_sets.py` 里失效的 override 与随之失效的 `User` import 一并清掉 |
| **P1-1** | 新增 `src/domain/permissions.py`（`Permissions` 18 个常量 + `ALL_PERMISSION_CODE` + `all_permission_codes()`）；10 个路由文件的 21 处字面量改为常量；`rbac_seed.py` 的 `PERMISSIONS` / `OPERATOR_MENUS` / `VIEWER_MENUS` 改为引用同一常量；新增用例 `test_every_declared_permission_code_is_seeded` |
| **P1-2** | `permission_codes` 复用一次 `role_codes` 结果判断是否 admin，省掉一条 SQL |
| **P2-1** | `ruff format` 11 个文件 |
| **P2-2** | `.gitignore` 增加 `.workbuddy/` |
| **P2-3** | 删除 `service/logharvest.db` 与 `web/dist/` |
| **P1-3** | 本报告 + README 增补 RBAC 章节 |
| **P1-4** | 不改代码；README 与前端 error 文案已说明 component 契约（前端注释本就在 `findPageComponent` 上） |

### 有意不做 / 推迟

- **P1-4 组件映射**：把 `includes` 模糊匹配改成精确映射需要同时改后端种子与前端注册，属于独立改动，留到下轮。
- **P2-4 死配置**：与上一轮一致，按"不删除、只标注"处理。
- **M3 多行记录**：设计已评审但零实现，是下一个里程碑的完整功能，不是"优化"，不在本轮顺手做。
- **P1-3 的正式设计文档**：本报告记录了契约与偏差；完整的 RBAC 设计文档建议与"范围扩大"的评审一起补。

### 仍然开放（M3 及以后）

多行堆栈规则、`matches` 按月分区、内存自适应并发、`max_matches_per_task` 强制执行、Prometheus 接入、`copy_to_local` 开关生效、`last_error` 写入、样本试跑端点、Worker 详情端点、Monaco 编辑器、结果保留期与告警；登录限流进程内计数在多副本下按副本数放大。此外新增一项：**RBAC 范围扩大需要补一次正式评审**（见 §1.5 D1–D4）。
