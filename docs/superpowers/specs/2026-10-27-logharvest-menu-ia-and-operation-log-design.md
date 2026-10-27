# LogHarvest 菜单信息架构重构与操作日志设计

- 状态：已评审（2026-10-27，四项决策由用户选定，三项默认由"继续"放行）
- 日期：2026-10-27
- 适用里程碑：**功能优化轮**（M3 多行记录明确推迟，不在本轮）
- 关联：[RBAC 设计与验收记录](2026-10-27-logharvest-rbac-menus-permissions-design.md)、[主设计](2026-10-24-compressed-log-extraction-design.md)、[M3 设计](2026-10-26-logharvest-m3-multiline-records-design.md)（本轮不触碰）
- 决策记录：操作日志=**真做审计闭环**；数据源与 Worker 管理=**都进任务管理**；统计=**新建页面 + 聚合接口**；规则维护=**页面化编辑 + 跨版本检索**；放行默认=seed 重塑、`audit:read` 只授 operator、统计页**不引入图表库**

## 1. 背景与目标

现有菜单只有两个业务目录：`LogHarvest`（7 个页面混在一起）与 `System`（3 个管理页）。作业、命中、规则集、数据源、Worker 全部平铺在"日志分析"一个目录下，用户要找"建任务"和要找"看命中"是同一条路径；系统管理侧则完全没有操作留痕——谁建了作业、谁改了角色授权、谁停用了用户，事后只能从应用日志推断。

本轮目标：

1. 一级菜单按**用户意图**分区：任务管理（做事）、规则管理（定规则）、系统管理（管权限与留痕）。
2. 补齐 **操作日志**：写操作与服务端事件留痕到数据库，可按人/动作/对象/时间检索。
3. 补齐 **任务统计**：从作业视角看批量结果，不再只能逐个点详情。
4. 把 **规则维护** 从"埋在规则集抽屉里的编辑"提升为独立页面，并提供跨版本规则检索。
5. 逐页把交互流程走顺：新建任务成向导、命中结果作用域显式化、数据源能一键起任务、窄屏与权限态一致。

## 2. 范围与非目标

### 2.1 范围内

菜单种子重塑、`operation_logs` 表与迁移 0005、约 14 处写埋点 + 登录事件、`GET /api/operation-logs`、`GET /api/stats/jobs`、`GET /api/rules`、`GET /api/jobs?status=`、3 个新前端页面、5 个既有页面的流程改造。

### 2.2 非目标（明确不做）

- **M3 多行记录**：设计与计划都在，本轮不动 `record_start_regex`、`RecordAssembler` 与相关契约。
- **数据权限**：仍无部门/`data_scope`/行级可见性；操作日志是功能权限下的全局视图，不做"只看自己的作业"。
- **不引入图表库**：`web/package.json` 现在没有 echarts 依赖，统计页用统计卡 + `el-progress` 自绘条形 + 表格；不为一个页面加 ~1 MB 首屏成本。
- **不引入规则实体表**：规则仍活在 `rule_sets.config` 里，版本快照与 Worker 编译路径不变（见 §9）。
- **不做登录限流跨进程化、不做 token 吊销**：延续 RBAC 设计 §13 的既有定性。

## 3. 菜单信息架构

| rank | 一级菜单 | 来源 | 子页面（path 不变者标"原"） |
|---|---|---|---|
| 0 | 首页（静态） | `router/modules/home.ts`，`rank: home = 0` | `/home` 运行概览，隐藏页 |
| 1 | **任务管理** `/logharvest` | **复用现有 `LogHarvest` 目录行**，title 由"日志分析"改"任务管理" | 任务创建 `/logharvest/jobs`（原，title 改）、作业详情 `/logharvest/jobs/:id`（原，隐藏）、任务统计 `/logharvest/stats`（**新**）、日志分析 `/logharvest/matches`（原，title 改）、数据源 `/logharvest/datasources`（原）、Worker 管理 `/logharvest/workers`（原） |
| 2 | **规则管理** `/rules` | **新目录行** `RuleCenter` | 规则维护 `/rules/maintenance`（**新**）、规则集维护 `/logharvest/rulesets`（原页面 re-parent，title 改） |
| 3 | 系统管理 `/system` | 现有 `System` 目录，rank 由 3 保持 | 用户管理、角色管理、菜单管理（原）、操作日志 `/system/audit-logs`（**新**） |

要点：

1. **复用 `LogHarvest` 行而不是新建目录**：内置菜单的 `name`/`path`/`component`/`parent_id` 被服务端保护（RBAC 设计 §10），新建目录就得删一个内置行（同样被保护）。复用只改 `title` 与 `rank`，代价最小。
2. **children 的绝对 path 不需要随父目录变**：`normalizeRoutePath` 对以 `/` 开头的 path 原样返回，规则集页留在 `/logharvest/rulesets` 挂到 `/rules` 目录下不会破坏路由、菜单跳转或已有书签。规则管理目录自身的 path 只作分组用途。
3. 三个新页面的 `component` 按既有契约写视图相对路径：`logharvest/stats/index`、`rules/maintenance/index`、`system/audit-logs/index`；`findPageComponent` 精确后缀命中。
4. 隐藏页（作业详情、运行概览）`show_link=false` 保持；`Overview` 仍挂在任务管理下但不出现在菜单。
5. 权限节点新增 1 个：**`audit:read`**（挂在操作日志页面下）。其余新页面复用已有码：任务统计用 `jobs:read`，规则维护用 `rulesets:read` / `rulesets:write`。权限码总数 18 → **19**。

## 4. 种子重塑机制

`ensure_rbac_seed` 现在的保证是"只补缺失、不覆盖已有"。本轮给它加一条**内置行重塑**规则。规则必须先分清两类字段——`update_menu` 对 `is_system` 行只锁 `name`/`path`/`component`/`parent_id`，而 `title`/`icon`/`rank`/`show_link`/`keep_alive`/`is_active` 是**允许管理员改的**（`test_rbac_seed_preserves_existing_menu_edits` 正是把 `Jobs` 的 title 改成自定义值、rank 改成 99 后断言重塑不冲掉）：

1. **强制字段**（UI 改不了，重置不可能冲掉人工编辑）：`parent_id`、`menu_type`、`path`、`component`。目录重组靠的就是 `parent_id`。
2. **守卫字段**（UI 可改，只能"未被动过"才更新）：`title`、`rank`、`icon`。判定方式是 `LEGACY_DEFAULTS`——只对本次**主动改了默认值**的 `(name, field)` 声明旧默认；现值等于旧默认才写入新值，不相等说明被人改过，保留现值。不在 `LEGACY_DEFAULTS` 里的字段一律不碰。
3. `show_link`、`keep_alive`、`is_active` 完全属于用户可编辑，**永不重置**（新行按声明值创建）。
4. 重塑只针对 `is_system = true` 的行；用户自建的菜单行仍只做"缺失补建"，任何字段都不动。
5. 缺失行照旧补建；`operator`/`viewer` 默认角色仍只在首次种子创建（RBAC 设计 §6 的保证不变）。
6. 新目录 `RuleCenter`、新页面与 `audit:read` 节点在**老库上也会补建**（补建不看 `first_seed`）。
7. 新增页面的授权按角色增量补：`ADDED_MENUS_BY_ROLE` 对已存在的 `operator`（`RuleCenter`、`TaskStats`、`RuleMaintenance`、`AuditLogs`、`audit:read`）与 `viewer`（前三项，**不含** `AuditLogs`/`audit:read`）只补该角色尚未拥有的那几条，不批量覆盖既有授权。首建库直接用完整列表。

本次声明的旧默认：`LogHarvest`（title 日志分析 / icon ep/search / rank 2）、`Jobs`（title 分析作业）、`Matches`（title 命中结果 / rank 5）、`DataSources`（title 数据源 / rank 4）、`RuleSets`（title 规则集 / rank 3）。

幂等性由"跑两次结果相同"的测试锁定；重塑不产生第二套用户体系，也不改变 admin 通配。

## 5. 数据模型：`operation_logs`

```
id            BIGINT 主键（PG 用 BIGSERIAL，SQLite 变体，沿用 matches.id 的既有做法）
actor_id      int NULL（外键 users.id，ON DELETE SET NULL）
actor_name    str  NOT NULL（用户名快照：删号后仍读得懂是谁）
role_hint     str  NOT NULL（记成功登录时的角色码快照，逗号分隔）
action        str  NOT NULL（点分动作，见 §6）
resource_type str  NULL（job/rule_set/data_source/user/role/menu/worker/system）
resource_id   str  NULL（数值或 worker_id 等字符串，统一存文本）
summary       str  NOT NULL（一句话人类可读）
detail        JSON NULL（PG JSONB / SQLite JSON，变更前后或参数摘要）
origin_ip     str  NOT NULL（与登录限流同一来源口径：request.client.host）
created_at    datetime NOT NULL（UTC，与全库时间口径一致）
```

索引：`(created_at)`、`(actor_id)`、`(resource_type, resource_id)`。迁移 `0005_operation_logs`，写法与 0001–0004 同构（显式 `create_table`/`create_index`，可 `downgrade`）。`matches` 按月分区那类规模优化不在本轮。

## 6. 埋点矩阵

显式调用 `record_operation(session, actor=..., action=..., resource_type=..., resource_id=..., summary=..., detail=..., ip=...)`，**不用 HTTP 中间件**：只记写操作，字段语义由调用点负责，避免每请求一行把表写成噪声源。

| action | 触发端点 | detail 要点 |
|---|---|---|
| `auth.login_success` / `auth.login_failed` | `POST /api/login` | 用户名、失败原因（口令错 / 账号停用）；失败时 `actor_id=NULL` |
| `auth.logout` | `POST /api/logout` | — |
| `job.create` | `POST /api/jobs` | 数据源、路径、规则集与版本、文件过滤 |
| `job.cancel` / `job.retry_failed` | `POST /api/jobs/{id}/cancel`、`.../files/retry-failed` | 影响计数 |

作业**没有删除端点**（`routes_jobs.py` 现有 7 个端点：列表/创建/详情/文件/尝试历史/取消/重试），因此不存在 `job.delete` 埋点；等真有删除接口再补，不预先埋空钩子。
| `rule_set.create_version` | `POST /api/rule-sets` | 名称、新版本号、规则条数 |
| `data_source.create` | `POST /api/data-sources` | 路径与开关 |
| `worker.drain` / `worker.resume` / `worker.drain_version` | 对应三个端点 | worker_id、版本、命中数 |
| `system.maintenance` | `POST /api/system/maintenance` | 三个清扫计数 |
| `user.create` / `user.update` / `user.delete` / `user.reset_password` / `user.assign_roles` | users 管理端点 | 变更前后（口令只记"已重置"，**绝不记明文**）；角色 id 集合前后 |
| `role.create` / `role.update` / `role.delete` / `role.assign_menus` | roles 管理端点 | 授权菜单数量与增量 |
| `menu.create` / `menu.update` / `menu.delete` | menus 管理端点 | 变更字段的前后值 |

写事务口径：埋点与业务写**同事务提交**（`record_operation` 只 `session.add`，由被调用方已有的 `commit` 收尾；独立提交路径如登录用单独 commit）。写日志失败不得影响业务结果——记录异常并吞掉（业务可用性优先于审计完整性），但要打进 structlog。

## 7. 查询 API 与权限

`GET /api/operation-logs`（权限 **`audit:read`**）：

- 参数：`limit`（默认 50，≤500）、`offset`、`actor_id`、`action`（前缀匹配，`user.` 即整族）、`resource_type`、`resource_id`、`created_from`/`created_to`（成对给出，否则 422，沿用 `/api/matches` 的时间契约）。
- 返回裸数组按 `created_at DESC, id DESC`，无 `total`（守既有窗口翻页契约）。
- 授权：admin 通配可见；`operator` 本轮授权可见；**`viewer` 不可见**（默认角色不含 `audit:read`）。操作日志含"谁改了什么账号"，给只读值守角色等于泄露权限拓扑。
- 无写接口、无删除接口：日志只追加。保留期清理留 §15。

## 8. 统计接口

`GET /api/stats/jobs`（权限 `jobs:read`）：

```
{
  "days": 14,
  "data_source_id": null,
  "totals": {jobs, succeeded, partial, failed, cancelled, running,
             files_total, bytes_total, matched_rows_total,
             file_succeeded, file_failed},
  "daily": [{date, jobs_created, files_succeeded, files_failed,
             matched_rows, bytes_total}],
  "top_rules": [{rule_id, rule_version, hits, jobs}]
}
```

实现后的两处细化（前端联调时确认，文档按实现为准）：`daily` 的字节字段与 `totals` 同名用 `bytes_total`；`top_rules` 带 `rule_version`——同名规则在不同版本里的命中不能混成一行。

- 参数：`days`（默认 14，1–90）、`data_source_id`（可选，进一步收窄）。
- 口径：按 `jobs.created_at` 归日；`bytes`/`matched_rows` 来自 `file_tasks` 与 `matches` 的既有聚合；失败率 = `files_failed / (succeeded + failed)`，前端做除零显示。
- 用 `func.date()` 归日在 SQLite 与 PG 都可执行，避免方言分叉；时区按 UTC（与全库一致），页面标注"UTC 日界"。
- 不做缓存、不引入物化视图：当前规模下一次聚合查询够用，慢了再谈。

## 9. 规则索引接口与规则维护页

`GET /api/rules`（权限 `rulesets:read`）：把各版本 `config.rules` 展平成只读索引，参数 `keyword`（匹配 `rule.id`、pattern、关键字词）、`type`（`keyword|regex`）、`rule_set_name`、`limit/offset`；返回 `{rule_set_id, rule_set_name, version, rule_id, type, severity, summary}`。规则不落新表，**跨版本检索是查询期解析**：版本数当前量级可接受，代价写进 §15。

规则维护页（`/rules/maintenance`）两态：

1. **编辑态**：选择规则集与版本 → 整页规则表格（增删改、`keyword/regex/threshold` 类型切换、字段提取）→ "另存为新版本"复用 `POST /api/rule-sets`（版本号仍是同名 max+1），后端编译/pathspec 校验为唯一裁判，前端只给互斥项提示（延续 M2b 的取舍）。
2. **检索态**：`GET /api/rules` 的跨版本列表，每行可跳到所属版本继续编辑。

规则集维护页（`/logharvest/rulesets`）保留版本列表与详情，"编辑规则"按钮改为跳转维护页并带 `rule_set_id`/`version` query，不再在抽屉里做重活。

## 10. 作业状态筛选

`GET /api/jobs` 新增可选 `status`（枚举校验，非法值 422），下推到 SQL `WHERE`。修掉 `jobs/index.vue:29` 现在"只过滤当前页 20 条"的假筛选——这是用户能直接感知的错误行为。

## 11. 页面与交互流程

| 页面 | 入口 | 流程与反馈 | 空 / 错 / 窄屏 |
|---|---|---|---|
| 任务创建（作业列表） | 任务管理 → 任务创建 | 顶部筛选（状态、关键字）走服务端；"新建任务"三步向导：① 数据源 + 目录（带 `POST /data-sources/{id}/test` 路径校验）② 规则集与版本（显示规则条数）③ 文件过滤 + 摘要确认 → 提交后跳详情并起 SSE | 无数据源时向导第 ① 步直接给"去建数据源"链接；422 原文显示；390 视口下列表隐藏非关键列 |
| 作业详情 | 列表行 | 保持现有 SSE 与文件任务表；新增"命中结果"入口带 `job_id` | 终态作业不再轮询；错误显示尝试历史 |
| 任务统计（新） | 任务管理 → 任务统计 | 7/14/30 天切换 + 数据源筛选；统计卡（作业数、字节、命中、失败率）+ 每日条形（`el-progress` 自绘）+ Top 规则表；点条形跳当日作业列表（带 `status` 与时间条件） | 全 0 时显示"该时间段没有作业"；接口 403 时整页降级提示 |
| 日志分析（命中结果） | 任务管理 → 日志分析 | 作用域选择器显式二选一（按作业 / 按时间范围），未满足条件时禁用查询并说明后端 422 契约；结果行内"所属作业""所属文件"可跳 | 游标翻页保留；窄屏卡片化 |
| 数据源 | 任务管理 → 数据源 | 卡片列表 + 新建 + 三态只读判定结果；"用它建任务"跳向导并预填 `data_source_id` | 检测失败显示 `writability=unknown` 的诚实文案 |
| Worker 管理 | 任务管理 → Worker 管理 | 按版本分组；drain/resume/按版本排空（已有），操作成功即刷新并提示影响数 | 无 Worker 时提示启动命令 |
| 规则维护（新） | 规则管理 → 规则维护 | §9 两态；保存成功提示"已创建版本 N"并留在编辑态可继续 | 校验失败逐行标红；无写权限时整页只读（复用 `canWrite` 模式） |
| 规则集维护 | 规则管理 → 规则集维护 | 版本列表 + 详情；编辑跳转维护页 | 保留"另存新版本"入口的权限门控 |
| 操作日志（新） | 系统管理 → 操作日志 | 筛选（时间范围、用户、动作族、对象）+ 表格 + 行展开 `detail` JSON 视图；用户名可跳"该用户近 7 天" | 空态提示"尚未产生写操作"；无权限落 `/access-denied` |
| 用户/角色/菜单 | 系统管理 | 沿用已提交的窄屏规范（列宽、`min(px, 92vw)` 弹窗、移动端取消固定列）；新增操作日志后系统目录 4 项 | 同上一轮 |

菜单侧：三个一级目录图标 任务管理 `ep/operation`、规则管理 `ep/set-up`、系统管理 `ep/setting`。任务管理**复用现有目录行**，其路由 `name` 保持 `LogHarvest` 不变（内置菜单的 `name` 被保护）；新目录 `name` 为 `RuleCenter`；系统管理保持 `System`。

## 12. 错误处理与边界

- 审计写失败不阻断业务（§6），但 `operation_logs` 自身的读接口异常按既有 500 语义抛出。
- 所有新接口遵守既有分层：路由 → service → `HTTPException`；请求形状问题 422，跨表规则问题 400/409，认证 401，权限 403。
- `actor_id` 外键 `ON DELETE SET NULL` + `actor_name` 快照，保证删号后日志仍可读；这条与 RBAC 的删除顺序约束（先 flush 关联删除）不冲突，因为是 SET NULL 而非级联删。
- 菜单重塑必须幂等：第二次启动不得再改任何行。
- 新用户无角色仍落 `/access-denied`；`viewer` 现在点操作日志是 403（后端）+ 菜单不可见（前端）。

## 13. 兼容性与升级顺序

1. 老库启动顺序：Alembic `upgrade head`（0005 建表）→ `ensure_rbac_seed` 重塑并补建 → 生效。无需人工脚本。
2. 前端路由 path 全部不变，仅 parent/rank/title 变化 → 旧书签与收藏继续可用。
3. 权限码从 18 增至 19；`test_every_declared_permission_code_is_seeded` 自动覆盖新码。
4. 操作日志表初值为空，不影响既有查询；`matches`/`jobs` 行为不变。
5. Worker 与队列 SQL 不受影响；不改任何 Worker 契约。

## 14. 测试策略与验收标准

单元/接口测试（SQLite）+ 集成（真实 PG，`LOGHARVEST_TEST_PG_URL`）：

- 种子重塑：新目录/新页面/新权限节点补建；parent 与 rank 重置；**守卫式 title 改名**（自定义过的 title 保留）；两次启动幂等；`operator` 获得 `audit:read`、`viewer` 没有。
- 操作日志：登录成功/失败写入；`job.create`、`user.assign_roles`、`menu.update` 等埋点内容与 detail 正确；口令明文**不出现在任何字段**；`audit:read` 负向 403；过滤参数与时间成对 422；删号后 `actor_id` 置空而 `actor_name` 保留。
- 统计：`/api/stats/jobs` 的 totals/daily/top_rules 形状与口径；`days` 边界；`data_source_id` 收窄；SQLite 与 PG 都能跑。
- 规则索引：`/api/rules` 展平与关键字/type/名称过滤；规则数与 config 实际条数一致。
- 作业筛选：`/api/jobs?status=` 下推生效、非法值 422。
- 迁移：0005 在全新库与已有库上幂等，schema 与 metadata 比对一致。

真实环境验收：

1. `cd service && LOGHARVEST_TEST_PG_URL=... uv run pytest -q` 全绿（含新增用例），`ruff check`/`format`、`mypy` 全绿。
2. `cd web && pnpm typecheck && pnpm build` 全绿。
3. Playwright + Chrome 真实视口（1440/1280/390）：三个一级菜单与子项层级正确；admin 走完"建数据源 → 三步向导建任务 → 看统计 → 看命中 → 查操作日志"全流程；operator 与 viewer 的菜单/按钮差异（viewer 看不到操作日志）；无 console error、无意外 4xx/5xx。
4. 文档同步：`service/README.md`（新接口与权限码表、操作日志口径）、`web/README.md`（新 IA 与页面流程）、RBAC 设计文档 §14 追加本轮对内置菜单重塑的例外说明。

## 15. 已知限制与后续

- 审计表只追加、无保留期清理；量大时需要定期归档（与 `matches` 分区同一批处理）。
- 跨版本规则检索是查询期解析 `config` JSON；版本数量级上去后应改为落库或建索引。
- 操作日志按功能权限全量可见，未做"只看自己"（数据权限明确不做）。
- 统计口径按 UTC 日界，与前端本地日期可能差一天，页面已标注。
- 登录事件写库与限流器仍是进程内/单库两套口径，多副本下的限流放大问题不变。

## 16. 实施结果与验收记录（2026-10-27）

**门禁**（真实 wslc PostgreSQL 16.15，宿主 `127.0.0.1:5433`）：

| 项 | 结果 |
|---|---|
| `LOGHARVEST_TEST_PG_URL=… uv run pytest -q` | **454 passed / 1 skipped**（本轮起点 427，净增 27 条；跳过项仍是 Windows FIFO 平台限制） |
| `ruff check` / `ruff format` | 全绿（104 文件） |
| `mypy`（项目 `files` 范围） | Success，28 文件（新增 audit/analytics 两模块进入范围） |
| `pnpm typecheck` / `pnpm build` | 通过，新增 stats/maintenance/rule-table/audit-logs 四个 chunk |
| `pnpm test`（仓库新增的 vitest 门禁，见 `agent.md` §一/§二） | 7 passed（`tests/logharvest/format.spec.ts`） |

**真实浏览器（Playwright + Chrome，1440/1280/390）**：`ia-check.js` **20/20 通过，0 console error，0 意外 4xx/5xx**。侧边栏实测顺序 `概览 → 任务管理(任务创建/任务统计/日志分析/数据源管理/Worker 管理) → 规则管理(规则维护/规则集维护) → 系统管理(用户/角色/菜单/操作日志)`；三步向导、跨版本检索、统计 30 天切换、"建用户→日志出现 `user.create` 行→日志不含明文口令"闭环、viewer 菜单不含操作日志且直连只命中兜底 404（路由未注册）全部通过。

逐页交互流程另跑 `flow-check.js`，**10/10 通过**：命中结果作用域二选一并可按作业查询（3 行）、带 `?job_id=1` 直达自动生效、数据源卡片 + 「用它建任务」跳 `#/logharvest/jobs?data_source_id=1`、Worker 按版本分组、规则集「编辑规则」跳 `#/rules/maintenance?rule_set_id=1&version=1` 且维护页按 query 定位到规则集与版本、作业详情「查看命中结果」跳 `?job_id=1`、作业状态筛选走服务端（选中后列表不含其它状态）。

**真实 PG 上的额外证据**：`func.date` 归日在 PG 返回 `date`、SQLite 返回文本，实现的 `_date_key` 已收敛，两种方言聚合数值逐项一致；老库（`logharvest_e2e`）重启后由种子自动完成 IA 重塑，无需人工脚本。

**本轮实施中发现并修掉的真实缺陷**：

1. `PureTableBar` 头部原本是固定高度 `h-15`，`#buttons` 里的控件一旦换行就会被下面的表格盖住——作业页「新建任务」、日志页「查询/重置」在 1440 下**点不到**。改为 `min-h-15 + items-center`（[bar.tsx](../../../web/src/components/RePureTableBar/src/bar.tsx)）。
2. Element Plus 的 `el-select`/`el-input` 根节点自带 `width:100%`，把 `w-36` 这类工具类盖掉；在 `flex-wrap` 容器里每个控件各占一行。按仓库既有做法把宽度挂到包裹 `div` 上（作业、日志、统计、规则维护四处）。
3. 菜单图标未登记进前端离线图标表（`offlineIcon.ts` / `offlineIconNames.ts`），会退化成运行时去 api.iconify.design 拉图标——内网首屏被拖慢甚至挂起。补齐本轮 IA 用到的 12 个 `ep/*`。
4. 测试种子把 `jobs` 与 `rule_sets` 放在同一次 `add_all` 里：SQLite 全绿、**PG 直接外键违例**（与上一轮删除顺序同一类问题），改为按依赖顺序分批提交。
5. `tests/domain/models/test_entities.py` 的表清单是硬编码的，新增 `operation_logs` 必须同步（迁移↔metadata 的 parity 测试是数据驱动的，但这份清单不是）。

**两条方法论**：审计埋点按显式调用而非中间件，`routes_jobs.py` 的三个作业动作由主线补写——把文件切给不同执行方时，跨切面改动必须显式指定归属，否则会静默漏掉；`elementFromPoint` 判"被遮挡"要先滚动到视口内，否则会把可点的元素误判为缺陷（本轮两次假阳性都源于此）。
