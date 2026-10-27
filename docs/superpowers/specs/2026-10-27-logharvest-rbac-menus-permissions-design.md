# LogHarvest RBAC 菜单权限与服务端动态路由设计

- 状态：已实现，文档补记（回溯设计 + 验收记录；对应审计报告 D1–D4 的收口）
- 日期：2026-10-27
- 适用里程碑：M2a/M2b 的**计划外范围扩大**（新增能力，非 M3 前置）
- 实现提交：`b74d0fd`（RBAC 全栈）、`08b0b8b`（只读规则集抽屉）、`3e4445d`（PostgreSQL 删除外键顺序）、`71451fb`（入口校验与系统页窄屏布局）
- 关联文档：[主设计](2026-10-24-compressed-log-extraction-design.md) §1.3、[M2a 后端计划](../plans/2026-10-25-logharvest-m2a-backend-auth-ops.md) `Global Constraints`、[M2b 前端计划](../plans/2026-10-25-logharvest-m2b-frontend-pure-admin.md) `Architecture`/`Plan Self-Review`、[2026-10-27 审计报告](../../2026-10-27-implementation-audit-and-optimization.md)（D1–D4 由本文收口）
- 分工：**怎么用**（配置项、接口清单、启动顺序）在 `service/README.md` 与 `web/README.md`；本文只记录**为什么这样设计、边界在哪里、验收证据是什么**。

## 1. 背景与目标

M2a/M2b 交付的是"单一管理员工具"：一个 `admin` 账号 + 前端静态菜单 + 角色只做存在性校验。实际共用平台的是三类人——值守只看结果、运维要建作业和动 Worker、平台管理员改数据源/规则集/账号——三类人共用一个全权账号既无法审计也无法放权。

设计目标：

1. **服务端是唯一强制点**。前端隐藏按钮只是体验，任何接口在 Server 侧独立可拒。
2. **权限码单一来源**。权限码写错不会报错，只会让某个接口对所有人 403，排查成本远高于一个常量的代价。
3. **菜单即权限**。授权对象只有一种（菜单节点），避免"权限表 + 菜单表"两套数据各自漂移。
4. **按模块细粒度**。`resource:action` 两级，够用即可，不引入 ACL/ABAC 表达式。
5. **零数据权限**。不做部门、`data_scope`、行级可见性——这是文档明确排除项，本文也不改变它。

## 2. 范围扩大评审（D1–D4 的正式记录）

### 2.1 与既有文档的冲突

| 既有陈述 | 出处 | 现状 |
|---|---|---|
| "不做多租户、**复杂 RBAC**、作业暂停/恢复" | 主设计 §1.3 v1 不做的事（第 51 行） | **边界不变**：本次只交付功能权限（菜单/按钮/接口），无多租户、无数据权限，仍在"不复杂"的那一侧 |
| "不引入 Redis 缓存、token 黑名单、`sys_menus`/按钮权限、部门岗位、动态菜单表" | M2a 计划 `Global Constraints`（第 24 行） | **被本文取代**：新增 `menus`/`role_menus`、按钮权限、动态菜单；Redis 与 token 黑名单仍然不做 |
| "`/auth/routes` 返回空数组（菜单在前端代码里静态定义，这是有意的最简实现）" | M2a 计划 Task 2（第 693 行） | **被本文取代**：返回按角色过滤的真实路由树 |
| "菜单与路由改为前端静态定义（不做服务端动态菜单）"、保留 `src/router/modules/logharvest.ts` | M2b 计划 `Architecture`（第 7 行）与 `Global Constraints`（第 23 行） | **被本文取代**：`logharvest.ts` 已删除，菜单完全来自后端 |
| "不覆盖并显式排除：服务端动态菜单与按钮权限" | M2b 计划 `Plan Self-Review` §1 Spec coverage（第 700 行） | **被本文取代** |
| 信封只覆盖 `login`/`refresh-token`/`auth-routes`/`logout`；业务与管理接口纯 REST、列表裸数组无 `total` | M2a/M2b 跨轨契约 | **保持不变**，新管理接口按纯 REST 落地 |

### 2.2 决策与依据

2026-10-26 会话内以三项选择题确认（用户答复原文口径）：

1. **权限模型**：完整对齐参考基座 `Hello-FastApi`（后端 `menus` 表 + 动态路由 + 菜单管理页），而非只做接口级角色校验。
2. **粒度**：按模块细分（`jobs:*`、`rulesets:*`、`datasources:*`、`matches:read`、`workers:*`、`system:*`、`users:*`、`roles:*`、`menus:*`），而非全平台单码。
3. **数据权限**：**不做**（无部门表、无 `data_scope`）。

因此这是一次**有记录、有边界**的范围扩大，而不是实现顺手多做。D4 指出的"评审链缺失"由本文补齐。

### 2.3 明确仍然不做

数据权限/行级可见性、部门与岗位、多租户、SSO、token 黑名单与服务端会话吊销、Redis 依赖、RBAC 变更审计流水（见 §13/§14）。

## 3. 术语

| 术语 | 含义 |
|---|---|
| 菜单节点 | `menus` 表一行；`menu_type` 0=目录、1=页面、2=权限 |
| 权限码 | `menu_type == 2` 节点的 `name`，形如 `jobs:read`；全库共 18 个 |
| 通配码 | `*:*:*`，只由 `admin` 角色在解析时合成，不入库 |
| 有效角色 | 经 `user_roles` 关联且 `roles.is_active` 为真 |
| 动态路由 | `GET /api/auth/routes` 返回目录/页面树，前端注册进 `router` |
| 内置对象 | `is_system = true` 的角色或菜单，由种子维护、受保护 |

## 4. 数据模型

### 4.1 表

- `menus`：`id`、`parent_id`（自引用外键，`NULL` 为根）、`name`（唯一索引 `ix_menus_name`）、`menu_type`、`path`、`component`、`title`、`icon`、`rank`、`show_link`、`keep_alive`、`is_active`、`is_system`、`created_at`；`ix_menus_parent_id` 支撑建树查询。
- `role_menus`：`(role_id, menu_id)` 复合主键 + 两个外键。无额外列——授权关系不需要属性。
- `roles` 扩展：`description`、`is_active`、`is_system`。

### 4.2 迁移 0004

`alembic/versions/0004_rbac_menus.py` 建两表并给 `roles` 补三列。补列走 `batch_alter_table` + `server_default`：SQLite 不支持 `ALTER` 之外的列变更，batch 模式在 SQLite 上重建表、在 PostgreSQL 上退化为普通 `ALTER`；`server_default` 让**已有角色行**在两种方言上都能安全拿到默认值。全新库幂等 `upgrade head` 已在真实 PG 上验证，13 张表与 `SQLModel.metadata` 一致（唯一约束按 `get_unique_constraints` 比对；`get_indexes` 不报 PG 唯一约束是检查器语义差异，非缺陷）。

### 4.3 删除顺序是硬约束

PostgreSQL 立即检查外键，SQLite 默认不检查。三处删除必须"先删关联行 → `session.flush()` → 再删主实体"，否则 SQLAlchemy 的单位工作可能先发 `DELETE users`，在 PG 上撞 `fk_user_roles_user_id_users` 返回 500：

- `delete_user`：清 `user_roles`
- `delete_role`：清 `role_menus`
- `delete_menu`：清 `role_menus`

这正是 SQLite 全绿、真实 PG 炸出的缺陷（§12.6），已由 `tests/integration/test_postgres_rbac.py` 钉住。

## 5. 权限模型

`src/domain/permissions.py` 是权限码的**唯一定义处**：`ALL_PERMISSION_CODE`、`Permissions` 的 18 个常量、`all_permission_codes()`。三处消费方必须共用它——路由的 `require_permission(code)`、种子的权限节点与默认角色授权、前端的按钮码与 `meta.auths`。

解析路径（`application/services/auth.py`）：

- `role_codes`：只取 `is_active` 角色，去重排序。
- `permission_codes`：先复用上一步结果判断是否 `admin`；是则返回 `[ALL_PERMISSION_CODE]`（**故意不枚举**，新增权限无需重新授权），否则 `user_roles → role_menus → menus` 一次 JOIN，条件为角色有效 + 菜单有效 + `menu_type == 2`。
- `accessible_menu_ids`：从授权节点沿 `parent_id` 上溯补全祖先；遇到停用祖先即停止，保证分支不会脱离可见父节点浮出。`admin` 直接返回全部有效菜单。
- 授权表**不存** `admin` 的通配：`role_menu_ids(admin)` 在读取时返回全部菜单 id，只为让前端权限树呈现"全选"。

## 6. 种子与升级路径

`ensure_rbac_seed`（启动 `lifespan` 与 `ensure_admin` 前调用，幂等）：

- 写入 **30 个菜单**：2 目录（`LogHarvest`、`System`）+ 10 页面 + 18 权限节点；权限节点的 `path` 继承父页面，`show_link=false`。
- 内置角色：`admin`（`is_system=true`，通配）+ `operator`（18 个授权，含写/操作）+ `viewer`（14 个授权，全只读），共 32 条 `role_menus`。
- **`operator`/`viewer` 只在首次种子（`menus` 为空）创建**。管理员删掉默认角色后，重启不得悄悄把它建回来——这条由 `test_deleted_default_role_is_not_recreated_on_restart` 锁定。
- 只补缺失，不覆盖已有行的 `title`/`rank`/授权，管理端的修改不会被启动流程冲掉；唯一例外是把旧库 `admin.is_system` 强制置真。
- `admin` 账号本身由 `scripts/seed_admin.py`（读 `LOGHARVEST_ADMIN_USERNAME/PASSWORD`）创建；登录路径不建账号。`ensure_admin` 不再自带"兜底建角色"分支——种子已保证角色存在，重复一份只会造出缺 `is_system` 的第二定义。

## 7. 服务端动态路由契约

`GET /api/auth/routes`（信封 `{code,message,data}`，`CurrentUser` 保护）返回路由树：

- 目录：`component=""` + `children`，只渲染有可见子页面的目录（空目录剔除，也不注册路由）。
- 页面：`path`/`name`/`component`（如 `logharvest/jobs/index`）+ `meta.{title,icon,rank,showLink,keepAlive,auths}`。
- `meta.auths` 是该用户**有效权限码全集**（`admin` 为全部声明码）；权限节点不进树，只喂给 `auths`。
- 排序 `(rank, id)`，目录与页面同规则。
- 没有任何可见菜单（有账号无角色）→ `data: []`，前端落 `/access-denied`，而不是停在一个持续 403 的首页。

## 8. 后端强制

`api/dependencies.py` 提供两个工厂，全项目 **28 处 `Depends(require_permission...)`** 声明：

- `require_permission(code)`：请求级 `SessionDep`。缺/坏 token 与停用用户保持 **401**；已认证但无码 → **403** `permission '<code>' required`。通配短路。
- `require_permission_streaming(code)`：SSE 等长响应专用，按 `EngineDep` 自开自闭一条短会话，连接在响应开始前归还——不能用请求级 Session 钉住整条流。
- 权限映射：作业读/写/操作、规则集读/写、数据源读/写（探测接口无副作用，归 `datasources:read`）、命中读、Worker 读/操作、系统读/维护、用户与角色与菜单的读/写。旧的 `AdminDep` 等 4 个死符号已删除，杜绝"绕过权限码"的捷径。
- 业务作用域校验（如 `/api/matches` 必须带作用域）与 RBAC 是两层，各自独立返回 422/403。

## 9. 前端强制

- `initRouter()` 在登录与刷新时拉取 `/api/auth/routes`；`addAsyncRoutes` 把 `component === "Layout"` 映射为完整 Layout，把"空 component + children"的目录映射为 `layout/empty-layout.vue`（只有 `<router-view/>`，复用完整 Layout 会套出第二层侧边栏），页面用 `findPageComponent` 精确后缀优先匹配 `src/views`，匹配不到时 `console.error` + `EmptyLayout` 兜底而非静默置空。
- `filterNoPermissionTree` 同时按 `meta.roles` 与 `meta.auths` 过滤，`*:*:*` 放行。
- 按钮级：`v-perms="'code'"`（9 个页面、24 处）与脚本内 `hasPerms(code)`（规则集抽屉用它把整个表单切成只读）。
- 静态首页概览声明 `meta.auths: ["system:read"]`；`/home` 默认标签只在菜单可见时保留，避免默认落地页 403。
- 守卫：`meta.roles`/`meta.auths` 不满足 → `/access-denied`；登出走 `resetRouter()`（`clearRoutes()` + 还原静态路由 + 清缓存），不给下一个用户留下上一个用户的动态路由。
- **权限快照的时效**：登录响应里的 `roles/permissions` 存在 localStorage，管理端改授权后需重新登录才刷新；后端接口权限实时生效。这是有意取舍（§13）。

## 10. 管理 API 与边界保护

**17 个管理端点**（`users` 6 / `roles` 6 / `menus` 5，纯 REST，写操作权限码，删除返回 204）：

| 族 | 端点 |
|---|---|
| users | `GET /users`（`keyword`/`limit`/`offset`，行带 `roles`）、`POST`、`PUT /{id}`、`PUT /{id}/roles`、`POST /{id}/reset-password`、`DELETE /{id}` |
| roles | `GET /roles`、`POST`、`PUT /{id}`、`DELETE /{id}`、`GET /{id}/menus`、`PUT /{id}/menus` |
| menus | `GET /menus`、`GET /menus/tree`、`POST`、`PUT /{id}`、`DELETE /{id}` |

保护矩阵（服务层 `application/services/rbac.py` 抛 `RbacError` 子类，路由映射 400/404/409；路由再兜底 `IntegrityError` → 409）：

| 规则 | 结果 |
|---|---|
| 停用/删除自己、删除自己 | 400（`cannot disable/delete yourself`） |
| 停用/删除/摘除**最后一名 active admin** 的 admin 角色 | 409 |
| 停用或删除系统角色（`is_system`）、修改系统角色权限 | 409；停用系统角色 400 |
| 角色仍被用户引用时删除 | 409（带引用数） |
| 删除/改名/改 `path`/改 `component`/改父节点的内置菜单 | 400/409 |
| 菜单父子类型：目录→目录/页面、页面→权限、权限无子节点、根必须是目录 | 400 |
| 菜单/角色/用户名唯一性；未知 `role_ids`/`menu_ids` | 409 / 400 |
| 移动菜单到自身或后代（含数据成环） | 400 |
| 请求体形状：用户名与角色码字符集、名称/标题不接受显式空白 | 422 |

## 11. 方案取舍

| 取舍 | 选了什么 | 为什么 |
|---|---|---|
| 授权数据 | 复用 `users/roles/user_roles`，只加 `menus/role_menus` | 项目已有这三张表；引入 classy-fastapi 那套多用户表会形成第二套用户体系 |
| 权限载体 | 菜单即权限（`menu_type=2` 的 `name`） | 一份数据同时驱动侧边栏、路由与按钮；代价是"权限"必须挂在某个页面上 |
| 权限解析 | 通配码 + 常量单一来源，不接 Casbin/表达式引擎 | 18 个码的规则用一句 `in` 就判完，引规则引擎是纯负债 |
| 角色权限树 | `check-strictly`（勾选父节点不联动子节点） | 联动树会让"只给读、不给操作"这种常见诉求无法表达；页面已把这条规则写在提示里 |
| 分页 | 管理列表沿用裸数组 + `limit/offset`，不返回 `total` | 守住既有契约；前端用窗口式翻页并显式写明"后端不返回总数" |

## 12. 测试与验收记录

### 12.1 用例分布（RBAC 相关 38 条）

| 文件 | 条数 | 覆盖 |
|---|---|---|
| `tests/api/test_rbac_core.py` | 8 | 种子幂等/30 菜单/默认角色/首建不复活/权限码全量落地/通配 |
| `tests/api/test_users_api.py` | 7 | CRUD、分配角色、重置口令、最后 admin、自我保护、用户名校验 |
| `tests/api/test_roles_api.py` | 6 | CRUD、唯一码、授权读写、系统角色保护、空白名 422 |
| `tests/api/test_menus_api.py` | 8 | 树、类型层级、内置保护、成环拒绝、删除级联、空白名/标题 422 |
| `tests/api/test_business_permissions.py` | 6 | viewer/operator 对业务接口的正向 2xx 与负向 403（含作用域 422） |
| `tests/integration/test_postgres_rbac.py` | 3 | PG 上三处删除的外键顺序回归 |

### 12.2 真实 PostgreSQL（不是 SQLite 代跑）

`wslc` 容器 `lh-postgres`（PG 16.15，宿主 `127.0.0.1:5433`）+ `LOGHARVEST_TEST_PG_URL`：全量 **426 passed / 1 skipped**（427 收集；跳过项是 Windows FIFO 平台限制），其中 18 条 PG 集成（15 队列 + 3 RBAC）。

### 12.3 真实浏览器（Playwright + Chrome，非内置隐藏页）

- `rbac-functional.js`：用户/角色/菜单三屏 18 步完整 CRUD（建→搜→改→授权→停用/启用→删），**18/18 通过，0 console error，0 意外 4xx/5xx**。
- `review-check.js`：菜单上级选项与后端类型规则一致（目录可挂根、页面不给根、权限只列页面）、用户名非法时字段错误、合法时错误消失——**5/5 通过**。
- 布局审计在 1440/1280/390 真实视口截图量 DOM：1440 表格 1166 = 容器 1166、1280 1006 = 1006（无横向溢出），390 下弹窗 359/367 px、移动端取消固定操作列。

### 12.4 质量门禁

`ruff check` 全绿、`ruff format` 97 文件一致、`mypy`（项目 `files` 范围）27 文件 Success、`pnpm typecheck` + `pnpm build` 通过。

### 12.5 验收清单

| 验收项 | 证据 | 结论 |
|---|---|---|
| admin 通配且无需授权即可用全部新接口 | `test_rbac_core`、E2E 全绿 | ✅ |
| viewer 对所有写接口 403 | `test_business_permissions` | ✅ |
| 无角色用户不停留在 403 首页 | 守卫 + `/access-denied`，E2E 负向验证 | ✅ |
| 目录不嵌套第二层侧边栏 | `EmptyLayout` + 1440/1280 截图 | ✅ |
| 权限变更后端实时生效 | 依赖每次请求解析 | ✅ |
| 默认角色不被重启复活 | `test_deleted_default_role_is_not_recreated_on_restart` | ✅ |
| PG 与 SQLite 迁移结果一致 | Alembic 双跑 + schema 比对 | ✅ |
| 删除链路在真实外键下可用 | `test_postgres_rbac` | ✅ |

### 12.6 交付后由真实环境暴露并修复的缺陷

1. **PG 删除 500**（FK violation）→ 三处 `session.flush()` + 回归用例（`3e4445d`）。
2. **只读用户能点"另存为新版本"**（后端 403，UI 不当）→ 抽屉改纯查看态（`08b0b8b`）。
3. **入口校验缺口**：用户名可带空白/非 ASCII、角色名与菜单名/标题可显式传空白 → 422 约束 + 前端同规则（`71451fb`）。
4. **菜单弹窗给出必然被后端拒绝的"根目录"选项**、`ensure_admin` 兜底建角色的死分支、seed 测试依赖环境 `.env` 不具隔离性 → 同笔修复。

## 13. 已知限制与接受风险

| # | 限制 | 定性 |
|---|---|---|
| L1 | 最后一名 active admin 的判定是"检查后写"，PG 多副本并发下两个 admin 请求可互相越过保护（SQLite 串行化写，不受影响）。彻底修法是在 admin 行上加 `FOR UPDATE`（方言条件）。 | 接受风险，已定性 |
| L2 | 前端 `permissions` 是登录快照，改授权需重新登录；后端不受影响。 | 有意取舍 |
| L3 | 登录限流是进程内计数，多副本按副本数放大（延续 M2a 已知限制）。 | 已知限制 |
| L4 | `/auth/routes` 对非 admin 会重复解析角色 3 次（`is_admin` → `accessible_menu_ids` → `permission_codes`）；30 行表上的小 JOIN，每登录/刷新一次。 | 可忽略，未收敛 |
| L5 | `GET /api/menus?limit&offset` 是平铺分页，页边界可能切断子树；前端统一用 `/menus/tree`。 | 使用约定 |
| L6 | 无 RBAC 变更审计流水、无服务端会话吊销；口令经 TLS 传输，不在前端哈希。 | 属 §14 |
| L7 | `open-code-review` 对这条线 45 文件产出 36 条意见，逐条复核后仅 1 条半成立——外部评审结论必须回读源码，不代表代码本身无缺陷（§12.6 的四条都是人工/真实环境发现的）。 | 方法论 |

## 14. 后续扩展

1. **RBAC 变更审计**：谁给谁授了什么码、改了哪个菜单——目前只能从应用日志推断。
2. **服务端会话/吊销**：口令重置与账号停用后让旧 token 立即失效（依赖 Redis 或 `jti` 黑名单，属独立里程碑）。
3. **按页面收窄 `meta.auths`**：现为"用户权限全集"；若要做"这页只暴露这几个码"，需后端按父页面聚合。
4. **组件精确映射**：`findPageComponent` 的 `includes` 兜底改为显式注册表（上一轮有意推迟，涉及后端种子与前端注册同改）。
5. **权限码新增流程**：加一个码需要同步 `Permissions` 常量、`rbac_seed` 节点、路由依赖、前端 `v-perms` 四处；`test_every_declared_permission_code_is_seeded` 会在漏种时失败，这是当前的护栏。
6. **数据权限**：仍然不做。若未来要做，需求应先落到独立设计，而不是在本模型上叠 `data_scope` 字段。
