# LogHarvest REST API 契约（e2e 自动化测试参考）

- 调研方式：只读源码通读，所有结论均带 `file:line` 出处（相对 `E:\GitHub\think\`）。
- 源码中所有状态码、detail 文案均逐字摘录；未实现/未找到的内容显式标注「未找到」。

## 0. 全局约定

- 路由前缀：`app.include_router(api_router, prefix="/api")`（`service/src/main.py:28`）。下文路径均为最终对外路径（已含 `/api`）。
- 响应包络（envelope）规则：
  - **仅 auth 系列返回** `{"code":0,"message":"ok","data":...}`（`service/src/api/v1/routes_auth.py:27-29`）：`POST /api/login`、`POST /api/refresh-token`、`GET /api/auth/routes`、`POST /api/logout`。
  - 其余全部业务接口是**纯 REST**：直接返回 DTO（对象）或**裸数组**（列表），无 `code/message/data` 包装。
- 错误体形状：
  - `HTTPException` 为 FastAPI 默认 `{"detail": "<字符串>"}`；本文所有 detail 文案为逐字精确值。
  - Pydantic 请求体校验失败为框架默认 422：`{"detail":[{"loc":...,"msg":...,"type":...}]}`（数组）。
  - 查询参数越界（如 `limit=501`）同样是框架 422 数组体（`ge/le` 声明见各路由行号）。
- 认证方式：`HTTPBearer(auto_error=False)`（`service/src/api/dependencies.py:38`）；请求头 `Authorization: Bearer <accessToken>`，scheme 大小写不敏感（`credentials.scheme.lower() != "bearer"` 判定，`dependencies.py:89`）。
- 401 集合（均带 `WWW-Authenticate: Bearer` 头，`dependencies.py:76-81`）：
  - `"missing bearer token"`（无头或非 Bearer，`dependencies.py:90`）
  - `"invalid or expired token"`（验签失败/过期/token 类型不符，`dependencies.py:93`）
  - `"invalid token payload"`（sub 非数字串，`dependencies.py:96`）
  - `"user is not available"`（用户不存在或被停用，`dependencies.py:107`）
- 403 形状：`{"detail":"permission '<code>' required"}`（`dependencies.py:125-128`；SSE 变体同文案 `dependencies.py:152-156`）。admin 角色走通配短路 `*:*:*`（`dependencies.py:124`；通配常量 `service/src/domain/permissions.py:17`）。
- 列表分页：统一 `limit`（默认 50，多数上限 500，workers 列表上限 200）+ `offset`（默认 0，`ge=0`）；**列表响应无 total 包装**，裸数组即当页数据。
- DTO 定义唯一来源：`service/src/application/schemas.py`（响应模型与请求模型同文件）。

## 1. 端点总表

| 方法 路径 | 权限码 | 成功码 | 实际出现过的状态码集合 |
|---|---|---|---|
| GET `/api/health` | 无（匿名） | 200 | 200（`api/v1/router.py:22-25`） |
| GET `/api/health/ready` | 无（匿名） | 200 | 200 / 503（`router.py:28-44`） |
| POST `/api/login` | 无（匿名） | 200 | 200 / 401 / 403 / 422 / 429（`routes_auth.py:81-151`） |
| POST `/api/refresh-token` | 无（匿名） | 200 | 200 / 401 / 422（`routes_auth.py:154-180`） |
| GET `/api/auth/routes` | 仅需登录 | 200 | 200 / 401（`routes_auth.py:183-186`） |
| POST `/api/logout` | 仅需登录 | 200 | 200 / 401（`routes_auth.py:189-203`） |
| GET `/api/users` | `users:read` | 200 | 200 / 401 / 403 / 422（`routes_users.py:52-60`） |
| POST `/api/users` | `users:write` | 201 | 201 / 400 / 404 / 409 / 422（`routes_users.py:63-98`） |
| PUT `/api/users/{id}` | `users:write` | 200 | 200 / 400 / 404 / 409 / 422（`routes_users.py:101-124`） |
| PUT `/api/users/{id}/roles` | `users:write` | 200 | 200 / 400 / 404 / 409 / 422（`routes_users.py:127-150`） |
| POST `/api/users/{id}/reset-password` | `users:write` | 204 | 204 / 400 / 404 / 422（`routes_users.py:153-176`） |
| DELETE `/api/users/{id}` | `users:write` | 204 | 204 / 400 / 404 / 409（`routes_users.py:179-204`） |
| GET `/api/roles` | `roles:read` | 200 | 200 / 401 / 403 / 422（`routes_roles.py:51-58`） |
| POST `/api/roles` | `roles:write` | 201 | 201 / 404 / 409 / 422（`routes_roles.py:61-95`） |
| PUT `/api/roles/{id}` | `roles:write` | 200 | 200 / 400 / 404 / 422（`routes_roles.py:98-121`） |
| DELETE `/api/roles/{id}` | `roles:write` | 204 | 204 / 404 / 409（`routes_roles.py:124-147`） |
| GET `/api/roles/{id}/menus` | `roles:read` | 200 | 200 / 404（`routes_roles.py:150-155`） |
| PUT `/api/roles/{id}/menus` | `roles:write` | 204 | 204 / 400 / 404 / 409 / 422（`routes_roles.py:158-189`） |
| GET `/api/menus` | `menus:read` | 200 | 200 / 401 / 403 / 422（`routes_menus.py:55-62`） |
| GET `/api/menus/tree` | `menus:read` | 200 | 200（`routes_menus.py:65-67`） |
| POST `/api/menus` | `menus:write` | 201 | 201 / 400 / 409 / 422（`routes_menus.py:70-98`） |
| PUT `/api/menus/{id}` | `menus:write` | 200 | 200 / 400 / 404 / 409 / 422（`routes_menus.py:101-126`） |
| DELETE `/api/menus/{id}` | `menus:write` | 204 | 204 / 404 / 409（`routes_menus.py:129-147`） |
| GET `/api/data-sources` | `datasources:read` | 200 | 200 / 401 / 403 / 422（`routes_data_sources.py:22-29`） |
| POST `/api/data-sources` | `datasources:write` | 201 | 201 / 409 / 422（`routes_data_sources.py:32-62`） |
| POST `/api/data-sources/{id}/test` | `datasources:read` | 200 | 200 / 404（`routes_data_sources.py:65-74`） |
| GET `/api/rule-sets` | `rulesets:read` | 200 | 200 / 401 / 403 / 422（`routes_rule_sets.py:18-25`） |
| GET `/api/rule-sets/{id}` | `rulesets:read` | 200 | 200 / 404（`routes_rule_sets.py:28-37`） |
| POST `/api/rule-sets` | `rulesets:write` | 201 | 201 / 409 / 422（`routes_rule_sets.py:40-71`） |
| GET `/api/jobs` | `jobs:read` | 200 | 200 / 401 / 403 / 422（`routes_jobs.py:40-48`） |
| POST `/api/jobs` | `jobs:write` | 201（重放 200） | 200 / 201 / 400 / 404 / 409 / 422 / 503（`routes_jobs.py:51-108`） |
| GET `/api/jobs/{id}` | `jobs:read` | 200 | 200 / 404（`routes_jobs.py:111-117`） |
| GET `/api/jobs/{id}/files` | `jobs:read` | 200 | 200 / 404 / 422（`routes_jobs.py:120-130`） |
| GET `/api/jobs/{id}/files/{task_id}/attempts` | `jobs:read` | 200 | 200 / 404（`routes_jobs.py:133-148`） |
| POST `/api/jobs/{id}/cancel` | `jobs:operate` | 200 | 200 / 404（`routes_jobs.py:151-170`） |
| POST `/api/jobs/{id}/files/retry-failed` | `jobs:operate` | 200 | 200 / 404 / 409（`routes_jobs.py:173-199`） |
| GET `/api/jobs/{job_id}/events` (SSE) | `jobs:read`（流式变体） | 200 | 200 / 401 / 403 / 404（`routes_events.py:89-103`） |
| GET `/api/matches` | `matches:read` | 200 | 200 / 422（`routes_matches.py:16-59`） |
| GET `/api/workers` | `workers:read` | 200 | 200 / 401 / 403 / 422（`routes_workers.py:25-33`） |
| POST `/api/workers/{id}/drain` | `workers:operate` | 200 | 200 / 404（`routes_workers.py:36-66`） |
| POST `/api/workers/{id}/resume` | `workers:operate` | 200 | 200 / 404 / 409（`routes_workers.py:69-97`） |
| POST `/api/workers/drain-version` | `workers:operate` | 200 | 200 / 422（`routes_workers.py:100-130`） |
| GET `/api/system/status` | `system:read` | 200 | 200（`routes_system.py:21-26`） |
| GET `/api/system/overview` | `system:read` | 200 | 200（`routes_system.py:29-34`） |
| POST `/api/system/maintenance` | `system:operate` | 200 | 200 / 503（`routes_system.py:37-81`） |
| GET `/api/operation-logs` | `audit:read` | 200 | 200 / 422（`routes_operation_logs.py:23-61`） |
| GET `/api/stats/jobs` | `jobs:read` | 200 | 200 / 422（`routes_analytics.py:23-35`） |
| GET `/api/rules` | `rulesets:read` | 200 | 200 / 422（`routes_analytics.py:38-56`） |

路由挂载顺序：`router.py:47-59`。RBAC 错误类映射：`RbacError`=400、`RbacNotFoundError`=404、`RbacConflictError`=409（`application/services/rbac.py:41-52`；路由翻译 `routes_users.py:35-36`、`routes_roles.py:30-31`）。

## 2. 认证契约

### 2.1 POST /api/login
- 请求体：`{"username": str(1-150), "password": str(1-256)}`（`routes_auth.py:59-61`）。
- 响应（envelope）：`data = {"avatar": null, "username", "nickname", "roles": [role_code...], "permissions": [perm_code...], "accessToken", "refreshToken", "expires"}`（`routes_auth.py:140-151`）。
  - `expires` 是 **int 毫秒 epoch**（`int(expires_at.timestamp() * 1000)`，`routes_auth.py:149`）。
  - admin 的 `permissions` 即 `["*:*:*"]`（`application/services/auth.py:71-73`）；其他用户 = 激活角色授权激活的 `menu_type==2` 节点名集合（`auth.py:89-103`）。`roles` 为激活角色码去重排序（`auth.py:34-43`）。
- 失败状态码（逐字 detail）：
  - 429 `"too many login attempts; try again later"` + 响应头 `Retry-After: <秒>`（`routes_auth.py:97-101`）。
  - 401 `"invalid credentials"`（用户不存在或口令不符；未知用户名也跑一次 bcrypt 防时序泄露，`routes_auth.py:78,103-110`）。
  - 403 `"user is disabled"`（口令正确但账号停用；**不计入限流失败**，`routes_auth.py:111-115`）。
  - 422：框架请求体校验。
- 限流语义（`domain/services/rate_limit.py`）：滑动窗口、**只统计失败尝试**；key = `username.strip().lower() + "|" + client_ip`（`rate_limit.py:32-39`，取不到 IP 时用 `-` 从严）；默认 `login_max_attempts=10` / `login_window_seconds=300`（`config/settings.py:48-49`）；成功登录即清零（`routes_auth.py:117`；`rate_limit.py:79-81`）；限流器是**进程内单例**，阈值只在首次使用时从 settings 读一次（`dependencies.py:54-70`）；窗口判定 `check` 只读、`record_failure` 记账（`rate_limit.py:65-77`）；`retry_after = max(1, ceil(最早失败时刻 + window - now))`（`rate_limit.py:83-88`）。多副本部署各自计数（`rate_limit.py:16-17` 注释）。

### 2.2 POST /api/refresh-token
- 请求体字段是 **camelCase `refreshToken`**（`routes_auth.py:64-65`），且不能全空白（校验器抛 `"refreshToken must not be blank"`，框架包成 422，`routes_auth.py:67-72`）。
- 成功 `data = {"accessToken", "refreshToken", "expires"}`（`routes_auth.py:174-180`）；refresh token 会轮换（新签发）。
- 401 `"invalid refresh token"`：解码失败、期望类型不是 refresh、sub 非法、用户不存在/停用/用户名不匹配（`routes_auth.py:160-168`）。
- 无服务端会话，旧 refresh token 不吊销、自然过期（`routes_auth.py:191` 注释）。

### 2.3 GET /api/auth/routes 与 POST /api/logout
- routes：envelope 包 `build_user_routes` 结果（`routes_auth.py:183-186`）；节点形状 `{path, name, component, meta{title,icon,rank,showLink,keepAlive[,auths]}, children?}`，page 节点带 `auths`，无可见子页面的目录被丢弃（`application/services/navigation.py:29-107`）。
- logout：需合法 access token；返回 `{"code":0,"message":"ok","data":null}`（`routes_auth.py:189-203`）。

### 2.4 token 服务与过期
- access/refresh 由 `TokenService(secret_key, access_minutes, refresh_days)` 签发（`dependencies.py:41-51`）；默认 access 30 分钟、refresh 7 天（`settings.py:35-36`）。decode 校验 `expected_type`（access/refresh 互不通用，`dependencies.py:91`；`routes_auth.py:160`）。

### 2.5 admin 账号怎么来
- 启动只种 RBAC（菜单+角色，`main.py:16-22`），**不自动建 admin 用户**；admin 用户靠脚本 `service/scripts/seed_admin.py`：口令取 `LOGHARVEST_ADMIN_PASSWORD`（缺省交互提示），最短 8 位（`seed_admin.py:18-25`），`ensure_admin` 幂等建用户并挂 admin 角色（`application/services/auth.py:10-31`）。admin 用户名默认 `"admin"`（`settings.py:37`）。
- admin 角色 `is_system=True`、**不写 role_menus 行**，靠通配（`rbac_seed.py:429-444`）。

## 3. RBAC 管理接口与权限码

### 3.1 19 个权限码（`domain/permissions.py:23-50`）+ 通配
`jobs:read` `jobs:write` `jobs:operate` `rulesets:read` `rulesets:write` `datasources:read` `datasources:write` `matches:read` `workers:read` `workers:operate` `system:read` `system:operate` `users:read` `users:write` `roles:read` `roles:write` `menus:read` `menus:write` `audit:read`；通配码 `*:*:*`（`permissions.py:17`，仅 admin 生效）。

### 3.2 用户（`api/v1/routes_users.py`）
- GET `/api/users?limit&offset&keyword` → 裸 `list[UserRead]`；`UserRead={id,username,nickname,is_active,created_at,roles:[{id,code,name}]}`（`schemas.py:250-264`）。keyword 模糊匹配（`rbac.py:118`）。
- POST `/api/users` 201，body `UserCreate={username, password(8-256), nickname="", is_active=true, role_ids=[]}`；username 模式 `^[A-Za-z0-9][A-Za-z0-9_.@-]{1,49}$`（`schemas.py:267-274`）。重复名 409 `"username already exists"`（服务层 `rbac.py:132` 或路由 IntegrityError `routes_users.py:75-77`）；未知角色 400 `"unknown role ids: {missing}"`（`rbac.py:104`）。
- PUT `/api/users/{id}` 200，body `UserUpdate={nickname?, is_active?}`（`schemas.py:277-279`）。404 `"user not found"`（`rbac.py:153`）；400 `"cannot disable yourself"`（`rbac.py:158`）；409 `"cannot disable the last active admin"`（`rbac.py:160`）。
- PUT `/api/users/{id}/roles` 200，body **`{"role_ids": [int]}`**（`schemas.py:282-283`）。404 同上；400 `"unknown role ids: ..."`；409 `"cannot remove the last active admin"`（`rbac.py:217`）。
- POST `/api/users/{id}/reset-password` → **204 空体**，body `{"password": 8-256}`（`routes_users.py:153-176`、`schemas.py:286-287`）。
- DELETE `/api/users/{id}` → **204 空体**（`routes_users.py:179-204`）。400 `"cannot delete yourself"`（`rbac.py:245`）；409 `"cannot delete the last active admin"`（`rbac.py:247`）。删除前把审计日志 `actor_id` 置空（`routes_users.py:198`；`application/services/audit.py:79-94`）。

### 3.3 角色（`api/v1/routes_roles.py`）
- GET `/api/roles` → `list[RoleRead]`；`RoleRead={id,code,name,description,is_active,is_system}`（`schemas.py:290-298`）。
- POST `/api/roles` 201，body `RoleCreate={code, name, description="", is_active=true}`；code 模式 `^[a-z][a-z0-9_-]{1,49}$`（`schemas.py:300-304`）。409 `"role code already exists"`（`rbac.py:270` / `routes_roles.py:75`）。
- PUT `/api/roles/{id}` 200，body `RoleUpdate={name?,description?,is_active?}`；显式传空白 name → 422（校验器 `"name must not be blank"`，`schemas.py:307-318`）。400 `"system role cannot be disabled"`（`rbac.py:285`）；404 `"role not found"`（`rbac.py:281`）。
- DELETE `/api/roles/{id}` 204。409 `"system role cannot be deleted"`（`rbac.py:306`）；409 `"role is assigned to {n} user(s)"`（`rbac.py:315`）。
- GET `/api/roles/{id}/menus` → **裸 `list[int]`（菜单 id 数组）**（`routes_roles.py:150-155`）。**特例：admin 角色返回全部菜单 id**，而不是 role_menus 行（`rbac.py:330-333`）。400 `"unknown menu ids: {missing}"` 只出现在 PUT（`rbac.py:348`）。
- PUT `/api/roles/{id}/menus` → **204 空体**，body **`{"menu_ids": [int]}`**（`schemas.py:321-322`）。409 `"system role menus cannot be changed"`（`rbac.py:357`）。

### 3.4 菜单（`api/v1/routes_menus.py`）
- GET `/api/menus`（平铺）与 GET `/api/menus/tree`（`children` 嵌套）→ `list[MenuRead]`；字段含 `parent_id,name,menu_type(0目录/1页面/2权限节点),path,component,title,icon,rank,show_link,keep_alive,is_active,is_system,created_at,children`（`schemas.py:325-341`；树逻辑 `routes_menus.py:65-67`）。
- POST 201，body `MenuCreate`：`menu_type` 必须 ∈ `Literal[0,1,2]`（`schemas.py:344-355`）。
- PUT `/api/menus/{id}` 200，body `MenuUpdate`：`exclude_unset` 语义——**显式 `parent_id: null` = 移到根，缺省 = 不改**（`schemas.py:358-383`）。
- DELETE 204。
- 服务层错误（全 400 除注明；`rbac.py:427-454,504-568`）：`"parent menu not found"`、`"root menu must be a directory"`、`"permission menu cannot have children"`、`"directory children must be directories or pages"`、`"page children must be permission nodes"`、`"directory menu cannot have a component"`、`"page menu requires path and component"`、`"permission menu cannot have a component"`、`"built-in menu cannot change {field}"`、`"built-in menu cannot change parent_id"`、`"cannot move menu under itself or its descendant"`；409 `"menu name already exists"`（`rbac.py:459,518`）、404 `"menu not found"`（`rbac.py:504,562`）。
- **内置资源删除语义**：409 `"built-in menu cannot be deleted"`（`is_system=True` 菜单，`rbac.py:564`）；409 `"menu still has children"`（`rbac.py:568`）。

### 3.5 种子：角色 ↔ 菜单映射（`application/services/rbac_seed.py`）
- 权限码节点名即权限码（`menu_type=2` 的 `name`；`permissions.py:3-5` 注释）。
- `operator`（首种才建，`rbac_seed.py:446-455`；名单 `OPERATOR_MENUS:248-272`）：页面 LogHarvest/Jobs/JobDetail/TaskStats/Matches/DataSources/Workers/Overview/RuleCenter/RuleMaintenance/RuleSets/AuditLogs + 权限 `jobs:read, jobs:write, jobs:operate, rulesets:read, datasources:read, matches:read, workers:read, workers:operate, system:read, system:operate, audit:read`。**没有** rulesets:write、datasources:write、users/roles/menus 任何码。
- `viewer`（`VIEWER_MENUS:274-292`）：`jobs:read, rulesets:read, datasources:read, matches:read, workers:read, system:read` 六个只读码；**无 audit:read**（设计注释 `rbac_seed.py:295`）。
- operator/viewer **只在 menus 表为空的首次种子时创建**；管理员删掉后重启不会复活（`rbac_seed.py:399-403,446-464`）；已存在的默认角色用 `ADDED_MENUS_BY_ROLE` 增量补授权（`rbac_seed.py:296-297,465-470`）。

## 4. 数据源（`api/v1/routes_data_sources.py`）

- **只有 GET 列表 / POST 创建 / POST {id}/test 三个端点；无更新、无删除接口（未找到）。** 注释明示 create-only（`routes_data_sources.py:45`）。
- POST body `DataSourceCreate`（`schemas.py:18-23`）：`{"name": str(min_length=1), "root_path": str(min_length=1), "read_only": true, "copy_to_local": true, "description": ""}`。**`read_only` 是 `Literal[True]`：传 `false` 直接 422**；缺省即 true。响应 201 `DataSourceRead={id,name,type,root_path,read_only,copy_to_local,enabled,description,created_at}`（`schemas.py:26-36`；表默认 `type="mount", enabled=True`，`domain/models/entities.py:18-29`）。重名 409 `"data source name already exists"`（`routes_data_sources.py:41-43`）。
- POST `/api/data-sources/{id}/test`（权限 `datasources:read`）→ `DataSourceTestResult={exists, is_dir, readable, writability, read_only_confirmed}`（`schemas.py:39-57`；构建 `application/services/data_sources.py:23-33`，readable=is_dir 且 st_mode 带任一读位）。404 `"data source not found"`（`routes_data_sources.py:72-73`）。
- `writability` 三态（`domain/source_guard.py:24-26`）：挂载表最长前缀命中且带 `ro` → `"read_only"`；显式 `rw` → `"writable"`；其余 → `"unknown"`（`source_guard.py:114-124`）。非 POSIX 或读不到挂载表时退到 `st_mode` 弱判定：**无任何写位才算 read_only，否则 unknown**（`source_guard.py:101-111`）。`read_only_confirmed` 只有在 `writability=="read_only"` 时才允许为 true，否则模型校验直接报错（`schemas.py:53-57`）。**unknown 永远不等于只读**。
- 可写挂载的后果在**作业创建/Worker 执行**处生效，不在 test 接口：`nas_read_only_policy` 默认 `enforce`，可写 → 创建作业 409（见 §6）。

## 5. 规则集（`api/v1/routes_rule_sets.py`）

- GET `/api/rule-sets` → `list[RuleSetRead]={id,name,version,created_at}`（按 name,version 排序，`application/services/rule_sets.py:52-54`）。
- GET `/api/rule-sets/{id}` → `RuleSetDetailRead`（加 `config: dict` 与 `created_by`，`schemas.py:73-75`）。404 `"rule set not found"`（`routes_rule_sets.py:36`）。
- POST `/api/rule-sets` 201，body `{"name": str(min_length=1), "config": RuleSetConfig}`（`schemas.py:60-62`）。**不接受 version 字段**：服务端按同名 max+1 自增（`application/services/rule_sets.py:38-39`；唯一约束 `uq_rule_sets_name_version`，`entities.py:34`）。并发撞唯一键 → 409 `"rule set version conflict"`（`routes_rule_sets.py:51-53`）。
- config 非法 → **422，detail 为字符串 `str(exc)`**（`routes_rule_sets.py:49-50`）。触发路径：
  - Pydantic 结构错误（body 解析阶段）→ 框架 422 数组体（`{"detail":[...]}`）。
  - 编译期 ValueError（服务层主动跑 `compile_ruleset`，`application/services/rule_sets.py:19`）：正则非法、flags 越界（只允许 i/m/s 子集，`domain/rules/evaluator.py:123-136`）、threshold 字段不是命名组（`evaluator.py:263-267`）等 → 422 字符串体。
  - `file_glob` 不是合法 gitwildmatch → 422 `"invalid file_glob {…}: …"`（`rule_sets.py:20-23`）。
  - 未知编码 → 422 `"unknown encoding 'xxx'"` / `"unknown fallback_encoding 'xxx'"`（LookupError，`rule_sets.py:24-28`）。
- `RuleSetConfig` 结构（`domain/rules/models.py:55-67`）：
  - `{"name": str, "file_glob": "**/*"(默认), "encoding": "utf-8"(默认), "fallback_encoding": "gb18030"(默认), "rules": [Rule...]}`；`rules` 内 `id` 必须唯一，否则 ValueError `"rule ids must be unique"`（`models.py:62-67`）。
  - `Rule={id, name="", enabled=true, match, threshold|null, severity="info", tags=[]}`（`models.py:36-43`）。
  - `match` 判别式二选一（`type` 字段）：
    - `{"type":"keyword","any":[...] 或 "all":[...],"case_sensitive":false}`：**any/all 恰好给一个**（同时给或同时不给 → ValueError `"exactly one of any/all must be provided"`，`models.py:13-17`）。
    - `{"type":"regex","pattern":str,"flags":"", "timeout_ms":50}`（`models.py:20-24`）；regex 超时仅告警并跳过该规则（`evaluator.py:430-438`）。
  - `threshold={"field":命名组名,"op":"gt|gte|lt|lte|eq","value":Decimal}`（`models.py:30-33`）；**threshold 只能配 regex**，keyword+threshold → ValueError `"threshold requires a regex match with named groups; keyword matches produce no fields"`（`models.py:45-52`）。
- **提取语义：未找到名为 `extract`/`fields` 的 config 键**。字段提取只来自 regex 命名组：命中行 `fields = found.groupdict()`（`domain/rules/evaluator.py:441`），`numeric_value` 取 threshold.field 的数值；keyword 规则的 `fields` 恒空（`models.py:48-51` 注释语义）。
- `created_by` 存 access token 对应用户名（`routes_rule_sets.py:48` 传 `user.username`）。审计 action `rule_set.create_version`（`routes_rule_sets.py:59`）。

## 6. 作业（`api/v1/routes_jobs.py`）

### 6.1 POST /api/jobs 请求与幂等
- body `JobCreate`（`schemas.py:85-90`）：
  ```json
  {"name": "…", "data_source_id": 1, "source_path": "…", "rule_set_id": 1,
   "file_filter": {"recursive": true, "max_depth": 5, "suffixes": null, "max_files": 20000}}
  ```
  - `file_filter` 各字段默认值与边界：`recursive=true`、`max_depth` ∈ [1,20] 默认 5、`suffixes: set[str]|null`、`max_files` ∈ [1,20000] 默认 20000（`schemas.py:78-82`）。可整体缺省（default_factory，`schemas.py:90`）。
  - `source_path` 解析后必须在数据源根内且是目录（`application/services/jobs.py:113-118`）。
  - 扫描只认压缩后缀集合 `ARCHIVE_SUFFIXES = .zip + .tar/.tar.gz/.tar.bz2/.tar.xz/.tgz/.tbz2/.txz + .gz/.bz2/.xz`（`domain/archive/reader.py:21-24,176`）；`suffixes` 显式给出时与 ARCHIVE_SUFFIXES 求交为空 → 400 `"file_filter.suffixes must contain at least one compressed archive suffix (…)"`（`jobs.py:75-88`）。
- 请求头 `Idempotency-Key`（`routes_jobs.py:76`）。语义（`application/services/jobs.py:91-204`）：
  - key 仅做 `strip()`，按 `(created_by, key)` 唯一查找（`jobs.py:46-56,97-104`；表唯一约束 `uq_jobs_creator_idempotency`，`entities.py:50`）。
  - 同 key + **请求体完全一致**（name/data_source_id/resolve 后 source_path/rule_set_id/file_filter 相等，`jobs.py:59-67`）→ 返回既有 Job，HTTP **200**（路由显式改写 `response.status_code = 200`，且不写第二条审计，`routes_jobs.py:86-89`）。
  - 同 key + 体不一致 → 409 `"Idempotency-Key was already used with a different request body"`（`jobs.py:68-72`）。
  - 空白 key（只有空格）→ 400 `"Idempotency-Key must not be blank"`（`jobs.py:97-99`）。
  - 并发撞唯一键后回查同样走重放/409 分支（`jobs.py:193-201`）。
  - **不带 key** 每次新建（key 列可空，NULL 不参与唯一冲突）→ **201**。
- 完整状态码集合（路由声明与代码路径一致，`routes_jobs.py:55-68`）：201 / 200（重放）/ 400 / 404 / 409 / 422 / 503。逐字 detail：
  - 404 `"data source not found or disabled"`、404 `"rule set not found"`（`jobs.py:106-111`）。
  - 400 `"source_path is outside data source root"`、400 `"source_path is not a directory"`（`jobs.py:115-118`）。
  - 409 可写挂载：`SourceNotReadOnly` → 409，detail = `"source path {path} is mounted writable; refusing to process"`（`routes_jobs.py:80-82`；消息 `domain/source_guard.py:143-144`；守卫在扫描之前，`jobs.py:120-122`）。
  - 503 `"source_path scan timed out; narrow the directory or filters"`（扫描 10 秒截止 `SCAN_DEADLINE_SECONDS=10.0`，`jobs.py:39,143-148`）。
  - 400 `"too many files in source directory"`（超 `max_files`，`jobs.py:149`）；400 `"cannot scan source_path: {exc}"`（OSError，`jobs.py:150-151`）。
- 成功响应 `JobRead`（`schemas.py:93-108`）：`{id,name,data_source_id,source_path,rule_set_id,status,total_files,total_bytes,cancel_requested,created_by,created_at,started_at,finished_at,error}`。
  - **0 个文件时作业创建即终态**：`status="succeeded"`、`finished_at=now`（`jobs.py:164-166`）。有文件则 `status="running"`。
  - `file_tasks` 批量入队，初始 `status="pending", attempts=0, max_attempts=5`（`jobs.py:174-186`；列默认 `entities.py:106-109`）。

### 6.2 作业状态机
- Job 状态词表（`FileTaskStatus`/`JobStatusFilter` 共用同一套词，`schemas.py:111-130`）：`pending, running, succeeded, partial, failed, cancelled`；作业行默认 `"pending"`（`entities.py:62`）但创建路径立刻写 running/succeeded（§6.1）。
- 终态集合：`TERMINAL_JOB_STATUSES = {"succeeded","partial","failed","cancelled"}`（`application/services/jobs.py:19`）。
- GET `/api/jobs?status=<六词之一>&limit(1-500)&offset` → 裸数组，按 `created_at desc, id desc`（`routes_jobs.py:40-48`；排序 `application/services/queries.py:35`）；非法 status 由 FastAPI 422（Literal 校验，`schemas.py:120` 注释）。
- GET `/api/jobs/{id}` → `JobRead`；404 `"job not found"`（`routes_jobs.py:33-37`）。

### 6.3 文件任务
- GET `/api/jobs/{id}/files?status=&limit&offset` → 裸 `list[FileTaskRead]`（`routes_jobs.py:120-130`）；status 是 `FileTaskStatus` Literal（六词），非法 → 422。FileTaskRead 字段：`{id,job_id,source_path,file_name,size_bytes,status,attempts,max_attempts,worker_id,scheduled_at,heartbeat_at,processed_lines,matched_rows,error}`（`schemas.py:133-148`）。作业不存在 → 404 `"job not found"`（先查再列，`routes_jobs.py:129`）。排序按 `FileTask.id`（`queries.py:49`）。
- GET `/api/jobs/{job_id}/files/{task_id}/attempts` → 裸 `list[FileTaskAttemptRead]={file_task_id,attempt_no,worker_id,worker_version,status,started_at,finished_at,processed_lines,matched_rows,error}`（`schemas.py:151-162`）。**只有任务不存在或不属于该 job 才 404 `"file task not found"`；存在但无历史 → 200 `[]`**（`routes_jobs.py:133-148`；`queries.py:97-108`）。按 `attempt_no` 升序。

### 6.4 取消与重试
- POST `/api/jobs/{id}/cancel`（`jobs:operate`）→ 200 `JobRead`（`routes_jobs.py:151-170`）。语义（`application/services/jobs.py:207-230`）：终态作业直接原样返回；置 `cancel_requested=true`；把所有 `pending` 任务翻成 `cancelled`；**若仍有 `running` 任务，作业保持 `running`**（由心跳/收尾推进）；无 running 则立即 `cancelled` + `finished_at`。
- POST `/api/jobs/{id}/files/retry-failed`（`jobs:operate`）→ 200，**响应是普通 dict（无 response_model）**：`{"retried": int, "remaining_failed": int}`（`routes_jobs.py:173-199`；`jobs.py:274`）。
  - 409 `"cancelled jobs cannot be retried"`（`cancel_requested` 或已 `cancelled`，`jobs.py:243-244`）。
  - 只捞 `failed` 终态任务、按 id 升序、**单批上限 200**（`jobs.py:233,254-260`）；`attempts` **不重置**（docstring `jobs.py:236-237`）；重入队字段：`status=pending, error=null, finished_at=null, worker_id=null, scheduled_at=now`（`jobs.py:262-267`）；有任务被重入且作业在终态 → 作业回 `running` 并清 `finished_at`（`jobs.py:269-272`）。`remaining_failed = failed 总数 - 本批 retried`。
  - 审计 action `job.retry_failed`，detail `{"counts": {...}}`（`routes_jobs.py:188-197`）。

### 6.5 SSE GET /api/jobs/{job_id}/events
- 帧格式**逐字节**：`"event: progress\ndata: " + json.dumps(payload, default=str) + "\n\n"`（`api/v1/routes_events.py:56-57`）。
- payload 字段：`{"job_id": int, "status": job.status, "total": int, "by_status": {状态: 计数}, "processed_lines": int, "matched_rows": int}`（`routes_events.py:39-46`；by_status 是对 FileTask 按 status 分组计数，`routes_events.py:28-38`）。
- 轮询间隔 1.0 秒（`SSE_POLL_SECONDS`，`routes_events.py:20,86`）；查询参数 `max_events` 默认 0（=不限）、`ge=0, le=100`（`routes_events.py:94`）。
- 终止条件：发出终态 status 后结束（`routes_events.py:84-85`；终态集合来自 `jobs.py:19`）；流中途作业消失 → 静默结束流（`routes_events.py:76-79`）。
- 响应头：`Content-Type: text/event-stream`、`Cache-Control: no-cache`、`X-Accel-Buffering: no`（`routes_events.py:99-103`）。
- **在返回流之前先校验作业存在**：未知 job → 普通 404 `"job not found"`（`routes_events.py:96-98` 调 `_snapshot`）。
- 权限走 `require_permission_streaming(jobs:read)`（无 request 会话，短会话），401/403 同普通接口文案（`routes_events.py:93`；`dependencies.py:134-159`）。
- **心跳/ping 注释行：未找到**（流里只有 `event: progress` 帧，两次事件之间最长 1 秒静默，无 `:ping` 之类行）。

## 7. 命中查询 GET /api/matches（`routes_matches.py`）

- 参数（全部 query）：`job_id, rule_id(str), file_name, severity, created_from, created_to, created_before, id_before(ge=1), limit(默认50, 1-500)`（`routes_matches.py:20-28`）。
- **scope 强制**：`job_id` 与 `created_from+created_to` 二者至少其一，否则 422 `"either job_id or both created_from and created_to are required"`（`routes_matches.py:30-34`）。
- 422 `"created_from must not be after created_to"`（`routes_matches.py:38-42`）。
- 游标翻页：`created_before` 与 `id_before` **必须成对**，否则 422 `"created_before and id_before must be provided together"`（`routes_matches.py:43-47`）。游标谓词：`created_at < cb OR (created_at = cb AND id < ib)`，排序 `created_at DESC, id DESC`（`queries.py:78-88`）——即"上一页最后一行"的两个字段原样回传。
- `file_name` 是**精确等值**不是模糊（`queries.py:71`）。created 边界为闭区间（>=, <=，`queries.py:74-77`）。
- 响应：裸 `list[MatchRead]`；字段 `{id, job_id, file_task_id, rule_id, rule_version, source_path, member_path, file_name, line_no, raw_line, raw_truncated, fields(dict), numeric_value(Decimal|null→JSON 字符串/number), severity, tags, created_at}`（`schemas.py:165-182`）。注意 `MatchRead` **不含 rule_set_id**（表里有列，`entities.py:155`，DTO 未暴露）。
- 命中行写入语义（供断言）：`raw_line` 超 4096 UTF-8 字节会截断且 `raw_truncated=true`（`application/worker/processor.py:29,81-86`）；`fields` 来自 regex 命名组（`processor.py:327`）。severity 默认 `"info"`（`rules/models.py:42`；列默认 `entities.py:171`）。

## 8. Workers（`routes_workers.py` + `domain/models/entities.py:179-200`）

### 8.1 表列（WorkerRead 与列一一对应）
`workers(id str PK, hostname, version, status 默认"online", drain_requested 默认false, current_task_id int|null, max_concurrent_copies 默认4, cpu_percent float|null, memory_bytes bigint|null, temp_disk_free_bytes bigint|null, last_heartbeat, started_at, last_error str|null)`（`entities.py:179-200`；DTO `schemas.py:185-199`）。status 实际取值集合：`online / busy / draining / offline`（REGISTER 写 online，`infrastructure/task_queue.py:198-208`；run_once 报 draining/online/busy，`worker/main.py:124-134`；退出报 offline，`worker/main.py:242-243`；清扫降级 offline，`task_queue.py:224-230`）。

### 8.2 指标采集与上报
- 采集：`psutil.Process().cpu_percent(interval=None)`（两次调用间的均值，首次为 0.0）、`memory_info().rss`、`shutil.disk_usage(work_dir).free`；**任何采集异常降级为 None**（`worker/metrics.py:11-26`）。
- 上报 SQL：`cpu_percent = COALESCE(:cpu_percent, cpu_percent)`——**None 不覆盖旧值**（`task_queue.py:210-219`）。
- 上报周期：每轮 `run_once` 的 claim 前后各一次（idle 时≈`worker_poll_seconds` 默认 1.0s 一次，`worker/main.py:130-134` + `settings.py:29`）；任务执行中由 `on_lease_refresh` 回调在行心跳（20000 行/5s）与批量写入栅栏 touch 时刷新（`worker/main.py:168`；`processor.py:141-151,338-353`）。

### 8.3 管理端点
- GET `/api/workers?limit(1-200,默认50)&offset` → 裸数组按 `id` 升序（`routes_workers.py:25-33`；排序 `queries.py:92-94`）。**注意上限 200 与其他列表的 500 不同**。
- POST `/api/workers/{worker_id}/drain`（`workers:operate`）→ 200 `WorkerActionRead={"id","drain_requested","status"}`（`routes_workers.py:36-66`）。语义：**offline 行保持 status 不变**，只置 `drain_requested=true`（回来后再排，`routes_workers.py:44-48`）；在线行 → `status="draining"`。worker_id 未知 → 404 `"worker not found"`（`routes_workers.py:18-22`）。
- POST `/api/workers/{id}/resume` → 200 同形状；**offline 行 409 `"worker is offline"`**；否则 `drain_requested=false, status="online"`（`routes_workers.py:69-97`）。
- POST `/api/workers/drain-version?version=<str,min_length=1>` → 200 `{"drained": int, "flagged": int}`：`drained`=在线被翻成 draining 的数量，`flagged`=离线行只打标记的数量（`routes_workers.py:100-130`；`schemas.py:210-217`）。version 缺失/空 → 框架 422。匹配 `Worker.version == version` 全部行。
- **Worker 侧消费 drain_requested**：每轮 `run_once` 开头 `is_draining()`（SELECT workers.drain_requested，`task_queue.py:232`）→ 为真则上报 `"draining"` 状态并**不认领新任务**；已认领任务照常跑完（`worker/main.py:124-128`）。drain 不中断在跑任务。
- 审计 action：`worker.drain` / `worker.resume` / `worker.drain_version`（detail 各含 worker_id/version 与 status/计数，`routes_workers.py:51-60,82-91,118-128`）。

## 9. 系统状态与健康检查

- GET `/api/health`（匿名）→ 恒 200 `{"status":"ok","service":"logharvest"}`（`router.py:22-25`，纯存活探针，不碰库）。
- GET `/api/health/ready`（匿名）→ 执行 `SELECT 1`；成功 200 `{"status":"ready","service":"logharvest"}`；数据库异常 → 503 `{"detail":"database unavailable"}`（`router.py:28-44`）。
- GET `/api/system/status`（`system:read`）→ `{"file_tasks": {状态:计数}, "pending_oldest_seconds": float|null, "workers_online": int}`（`routes_system.py:21-26`；构建 `queries.py:111-130`；online 口径 = status ∈ `("online","busy","draining")`，`queries.py:11`）。
- GET `/api/system/overview`（`system:read`）→ `{"jobs":{状态:计数}, "file_tasks":{状态:计数}, "matches_24h": int, "workers": {"online": int, "by_version": {版本:计数}}, "oldest_pending_seconds": int|null}`（`routes_system.py:29-34`；构建 `queries.py:151-182`；`MATCHES_WINDOW_HOURS=24`，`queries.py:12`；`by_version` 含 offline 行，`schemas.py:226-231`）。
- POST `/api/system/maintenance`（`system:operate`）→ 200 `{"recovered": int, "workers_demoted": int, "jobs_finalized": int}`（`routes_system.py:37-81`；`schemas.py:241-244`）。非 PostgreSQL 连接 → 503（`MaintenanceRequiresPostgres`，`routes_system.py:57-62`；判定 `application/services/maintenance.py:42-45`）。幂等可重复跑，无积压时三个计数为 0（docstring `routes_system.py:50-52`）。僵死阈值默认 120s（`maintenance.py:5` `DEFAULT_STALE_SECONDS=120`）。审计 action `system.maintenance`（`routes_system.py:70`）。

## 10. 统计、规则索引、操作日志

- GET `/api/stats/jobs?days(默认14, 1-90)&data_source_id(可选, ge=1)`（权限 `jobs:read`，`routes_analytics.py:23-35`）→ `JobStatsRead={days, data_source_id, totals, daily[], top_rules[]}`（`schemas.py:413-450`）：
  - `totals={jobs,succeeded,partial,failed,cancelled,running,files_total,bytes_total,matched_rows_total,file_succeeded,file_failed}`（`schemas.py:413-424`）。
  - `daily=[{date(YYYY-MM-DD,UTC 日界),jobs_created,files_succeeded,files_failed,matched_rows,bytes_total}]`（`schemas.py:427-433`）。
  - `top_rules=[{rule_id,rule_version,hits,jobs}]`，命中数前 10（`schemas.py:436-440`；`application/services/analytics.py:67-208`）。窗口锚定 `jobs.created_at`。
- GET `/api/rules?keyword&type(keyword|regex, **查询键名是 type**, alias 实现，`routes_analytics.py:43`)&rule_set_name&limit(1-500)&offset`（权限 `rulesets:read`，`routes_analytics.py:38-56`）→ 裸数组 `RuleIndexRead={rule_set_id,rule_set_name,version,rule_id,type,severity,summary}`（`schemas.py:453-462`）。Python 侧展开 config、跳过 ValidationError 的历史脏配置、按 (name asc, version desc, rule_id) 排序后内存分页（`analytics.py:211-260`）。无 total。
- GET `/api/operation-logs?limit(1-500)&offset&actor_id&action&resource_type&resource_id&created_from&created_to`（权限 `audit:read`，`routes_operation_logs.py:23-61`）→ 裸数组 `OperationLogRead={id,actor_id,actor_name,role_hint,action,resource_type,resource_id(str),summary,detail,origin_ip,created_at}`（`schemas.py:391-410`）。
  - 422 `"created_from and created_to must be provided together"`（不成对，`routes_operation_logs.py:38-42`）；422 `"created_from must not be after created_to"`（`routes_operation_logs.py:43-50`）。
  - `action` 过滤是 **ILIKE 前缀匹配**（`application/services/audit.py:114-131`）：传 `user` 能匹配 `user.create` 等。
  - 表只追加：无写/删接口（`routes_operation_logs.py:1-4` docstring）。删除用户时 `actor_id` 置 NULL 但 `actor_name/role_hint` 是快照仍可读（`domain/models/audit.py:36-43`；`audit.py:79-94`）。
- **审计 action 全集（源码逐字，共 23 个）**：
  `auth.login_failed` `auth.login_success` `auth.logout`（`routes_auth.py:49,131,195`）；
  `user.create` `user.update` `user.assign_roles` `user.reset_password` `user.delete`（`routes_users.py:83,113,139,164,190`）；
  `role.create` `role.update` `role.delete` `role.assign_menus`（`routes_roles.py:81,110,135,171`）；
  `menu.create` `menu.update` `menu.delete`（`routes_menus.py:90,115,140`）；
  `data_source.create`（`routes_data_sources.py:49`）；`rule_set.create_version`（`routes_rule_sets.py:59`）；
  `job.create` `job.cancel` `job.retry_failed`（`routes_jobs.py:93,162,191`）；
  `worker.drain` `worker.resume` `worker.drain_version`（`routes_workers.py:54,85,121`）；
  `system.maintenance`（`routes_system.py:70`）。
  登录成功额外写 `role_hint`=当时角色码逗号串（`routes_auth.py:138`）。审计写入失败只吞异常不影响业务事务（`audit.py:25-76`）。

## 11. 环境变量全集（前缀 `LOGHARVEST_`）

Settings：`env_prefix="LOGHARVEST_"`、`env_file=".env"`、`extra="ignore"`（`config/settings.py:19-23`）。默认值逐行（`settings.py:25-52`）：

| 变量（LOGHARVEST_ 前缀省略后） | 默认 | 用途 / 约束 |
|---|---|---|
| `APP_NAME` | `LogHarvest` | 展示名（:25） |
| `ENVIRONMENT` | `dev` | 非 dev/test 时强制 admin_password（:26,65-69；`PROTECTED_ENVIRONMENTS={"dev","test"}` :14） |
| `DATABASE_URL` | `sqlite:///./logharvest.db` | 队列 SQL 需 PostgreSQL，否则 maintenance 503（:27；`maintenance.py:42-45`） |
| `AUTO_CREATE_TABLES` | `true` | 启动时建表+RBAC 种子（:28；`main.py:16-22`） |
| `WORKER_POLL_SECONDS` | `1.0` | Worker 空闲退避 / SSE 无关（:29；`worker/main.py:316`） |
| `WORKER_VERSION` | `0.1.0` | **Worker 注册版本**，heartbeat 与 drain-version 依据（:30；`worker/main.py:314`；register SQL `task_queue.py:198-208`） |
| `MAX_CONCURRENT_COPIES` | `4` | 注册进 workers 表列（:31；`worker/main.py:311`；SQL :199-201） |
| `MATCHES_BATCH_SIZE` | `1000` | 命中批量落库行数（:32；`worker/main.py:317`；缓冲逻辑 `processor.py:333-336`） |
| `MAX_MATCHES_PER_TASK` | `200000`（ge=1） | 单任务命中上限，超限永久失败并清理本任务命中（:33；`processor.py:307-311,365-373`） |
| `JWT_SECRET_KEY` | `""` **必填** | ≥32 字符否则启动失败（:34,54-63；`MIN_SECRET_LENGTH=32` :15）；无 dev 回退常量 |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | （:35） |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `7` | （:36） |
| `ADMIN_USERNAME` | `admin` | 仅 seed_admin 脚本用（:37；`scripts/seed_admin.py:30`） |
| `ADMIN_PASSWORD` | `""` | 非 dev/test 必填（:38,65-69）；脚本口令源（`seed_admin.py:22`） |
| `NAS_READ_ONLY_POLICY` | `enforce` | 词表 enforce/warn/ignore，拼错直接启动失败（:39,72-81；`source_guard.py:28-31`） |
| `ARCHIVE_MAX_MEMBER_BYTES` | `1<<30` (1073741824) | 解压预算-单成员（:42-44；`archive/reader.py:35`） |
| `ARCHIVE_MAX_TOTAL_BYTES` | `4<<30` (4294967296) | 预算-整包（`reader.py:36`） |
| `ARCHIVE_MAX_MEMBERS` | `10000` | 预算-成员数（`reader.py:37`） |
| `ARCHIVE_MAX_RATIO` | `500.0` | 预算-膨胀比；解压量 <64MB 不比压缩比（`reader.py:38,41`） |
| `LOGIN_MAX_ATTEMPTS` | `10`（ge=0） | 登录滑动窗口失败上限（:48；`routes_auth.py:91-101`） |
| `LOGIN_WINDOW_SECONDS` | `300`（gt=0） | 窗口长度（:49） |
| `TRUSTED_PROXY_CIDRS` | `""` | 逗号分隔 CIDR；仅当 TCP 对端命中才解析 X-Forwarded-For/X-Real-IP（:50-52,83-102；`api/v1/helpers.py:63-78`）。非法 CIDR → 启动 ValueError |

**非 Settings 的纯环境变量（Worker 进程 os.getenv 直读）**：
- `LOGHARVEST_WORKER_ID`：缺省用 `socket.gethostname()`（`worker/main.py:307`）。
- `LOGHARVEST_WORK_DIR`：缺省 `tempfile.gettempdir()`；实际工作目录 = `<base>/logharvest/<worker_id>`（`worker/main.py:308-309`）。
- `LOGHARVEST_ADMIN_PASSWORD`：仅 seed_admin 读取（`scripts/seed_admin.py:22`）。

启动强校验汇总（`settings.py:54-102`）：jwt 密钥必填且 ≥32；非 dev/test 必须有 admin_password；policy 词表；CIDR 可解析。`extra="ignore"` 意味着拼错变量名**静默无效**。

## 12. 队列 / 租约 / 失败语义（PostgreSQL 专有 SQL，`infrastructure/task_queue.py`）

- **认领**（CLAIM_SQL :19-43）：`status='pending' AND scheduled_at<=now() AND job.cancel_requested=false`，`ORDER BY scheduled_at,id LIMIT 1 FOR UPDATE OF t SKIP LOCKED`；同事务翻 `running`、`attempts = attempts + 1`、`started_at=COALESCE(旧值,now())`、写 `heartbeat_at`。领取一并返回 root_path/rule_set_id/version（免二次查询）。认领成功插入 attempt 历史行（START_ATTEMPT_SQL :52-57，`attempt_no=attempts`）。
- **租约/心跳**：`heartbeat_at` 由 (id, attempts, status='running') 三重栅栏刷新（HEARTBEAT_SQL :61-69；拷贝期只 touch :73-79）。栅栏失配 = 租约被回收 → Worker 抛 `LeaseLost` 停手（`processor.py:141-151`；`worker/main.py:186-188`）。行级心跳节奏：每 20000 行或每 5 秒取先到（`processor.py:105-107,338-353`），取消检查搭在同一节奏里（`processor.py:347-350`）；拷贝每 5 秒 touch 一次（`processor.py:107,456-469`）。
- **完成与重试分类**（COMPLETE_SQL :95-142）：`will_retry = (:status='failed' AND :retryable AND attempts < LEAST(max_attempts, :max_attempts) AND NOT cancel_requested)`；retry → `pending` + `scheduled_at = now() + make_interval(secs => attempts*5)`（**线性退避 5s×attempts**，:117-121）+ `finished_at=NULL`；已请求取消的作业失败 → 写 `cancelled` 不回队（:112-114,注释 :86-88）；attempt 历史行同语句收尾。
  - retryable=True 的通道：瞬时 errno `EIO/ETIMEDOUT/EAGAIN/EBUSY/EINTR/ENOMEM` +（存在时）`ESTALE/ENETRESET/ENETUNREACH/EHOSTUNREACH` → `RetryableTaskFailure`（`processor.py:38-52,75-78,393-401`）；其他任何未分类异常也按 retryable 处理（:406-414）。
  - retryable=False（永久）：符号链接逃逸根目录（`"source escapes data source root: …"`，:169-172）、size/mtime 与规划不符（:185-197）、`SourceNotReadOnly`（:383-392）、`MatchLimitExceeded`（:365-373）、成员级 partial/failed（内容失败，:416-431）。
  - **命中上限清理**：超限 → 永久失败前按同一租约栅栏 `delete_for_task` 丢弃本任务已写命中行，上报 matched_rows 归 0；清理失败不掩盖真实失败（`processor.py:212-228,240-251`）。重试开始时（attempts>1）也先清旧行（:279-280）。
  - 取消：`JobCancelled` → complete 写 `cancelled`（:362-364）。
- **僵死回收**（RECOVER_STALE_SQL :156-193）：`running` 且 `heartbeat_at IS NULL OR < now()-:seconds`；`FOR UPDATE SKIP LOCKED` 使并发清扫互斥。去向：作业取消中 → `cancelled`；`attempts < LEAST(max_attempts,:max_attempts)` → `pending`（立即，scheduled_at=now()，**attempts 保留不回退**）；否则 `failed` + `worker_id=NULL` + `finished_at=now()`；attempt 历史行标 `'timeout'`（:184-190）。阈值默认 120s（`recover_stale(stale_seconds=120)`，:517-523；`worker/main.py:31`）。
- **Worker 生命周期**：register 幂等 upsert 且把 status 强拉回 online（:198-208）；心跳 SQL :210-219；死worker 降级 `mark_dead_workers(stale_seconds=120)`：`status<>'offline' AND last_heartbeat<cutoff` → offline + 清 current_task_id（:224-230,552-562）。Worker 启动时先回收+清扫再进循环（`worker/main.py:214-219`），此后每 `DEFAULT_RECOVER_INTERVAL_SECONDS=300` 秒周期清扫（`worker/main.py:32,229-234`）；孤儿临时目录（>3600s）启动时清理（`worker/main.py:35,284-303`）。
- **作业终态汇总**（:244-341）：任务完结后 `finalize_job` 取快照（仅当 job 仍 running），`_derive_job_status` 优先级：有任何 open 任务 → 不收尾；`cancel_requested` → `cancelled`；否则 `partial>0 或 (failed 且 succeeded)` → `partial`；`failed` → failed；`succeeded` → succeeded；全 cancelled → cancelled（:310-341）。"取消中的作业 pending 不算 open"（CLAIM 不会再领，:255-259）。FINISH 语句带行级栅栏 `status='running'` 保证并发单写者（:282-294）。补偿扫描 `CANDIDATE_JOBS_SQL` 覆盖经回收/取消终结而无回调的作业（:296-307,543-550）。
- 规则 config 缓存：repo 端 (rule_set_id,version) → config，256 条 FIFO（:359,438-440）；Worker 端编译缓存 64 条 FIFO（`worker/main.py:105-114`）。版本不可变所以永不失效。

## 13. 「未找到」清单

1. 数据源**无更新/删除端点**；规则集**无删除/改版本端点**（只有 create-version 流）——e2e 清理只能直接动库。
2. 规则 config **无 `extract` / `fields` 键**；字段只来自 regex 命名组（§5）。
3. SSE **无心跳/ping 注释帧**、无自定义事件名（只有 `event: progress`）。
4. 无 token 吊销/登出黑名单；无服务端会话（`routes_auth.py:191`）。
5. 列表接口**均无 total/分页元数据**，纯 limit/offset（含 /api/rules 的内存分页）。
6. `MatchRead` 未暴露 `rule_set_id`（表有列，DTO 无，`schemas.py:165-182`）。
7. `GET /api/roles/{id}/menus` 对 admin 返回全部菜单 id 属特例语义（§3.3）。
8. 登录限流为进程内状态，重启即清零（`dependencies.py:54-70`）。
9. `PUT /api/users/{id}`、`PUT /api/roles/{id}` 等更新接口对"请求体字段全部缺省"无保护——按 Pydantic 缺省即不改动处理（`exclude_unset` 快照，`routes_users.py:117`）。

## 14. e2e 编写提示（与契约直接相关）

- 登录响应是 envelope，需先取 `["data"]["accessToken"]`；其余接口直接 JSON。
- 断言错误时读 `detail`：业务错误是**字符串**；框架校验错误是**数组**——两种 422 形状不同。
- 测试数据源 root_path 必须真实存在且（在 enforce 策略下）判定为非 writable，否则创建作业 409；CI 容器里可用只读 bind mount 或 `LOGHARVEST_NAS_READ_ONLY_POLICY=warn|ignore` 绕开（§11）。
- 作业要产生命中需在 root 下放入 `.zip/.tar.gz/…` 归档（§6.1 后缀集合），归档内文本按 config `encoding/fallback_encoding` 解码。
- 幂等重放的等体判定用 resolve 后的 `source_path`（相对路径/尾斜杠差异会被规范化，`jobs.py:64`）。

## 15. 典型报文示例（字段名与默认值均取自上文已核实处）

以下仅演示形状；`…` 表示由测试数据决定。字段集合与 §2–§10 逐一对应。

### 15.1 登录 / 刷新
```jsonc
// POST /api/login  请求（routes_auth.py:59-61）
{"username": "admin", "password": "..."}

// 200 响应：envelope 仅此系列出现（routes_auth.py:27-29,140-151）
{"code": 0, "message": "ok", "data": {
  "avatar": null, "username": "admin", "nickname": "...",
  "roles": ["admin"], "permissions": ["*:*:*"],
  "accessToken": "…jwt…", "refreshToken": "…jwt…",
  "expires": 1730000000000            // int 毫秒 epoch（:149）
}}

// POST /api/refresh-token  请求（camelCase！routes_auth.py:64-65）
{"refreshToken": "…jwt…"}
// 200 data = {"accessToken", "refreshToken", "expires"}（:174-180，双 token 均新发）

// 失败样例：
// 401 {"detail":"invalid credentials"}；403 {"detail":"user is disabled"}；
// 429 {"detail":"too many login attempts; try again later"} + 头 Retry-After: 42
```

### 15.2 数据源
```jsonc
// POST /api/data-sources → 201（read_only 传 false 会 422，Literal[True]，schemas.py:18-23）
{"name": "nas-a", "root_path": "/mnt/logs", "read_only": true,
 "copy_to_local": true, "description": ""}

// 201 响应 DataSourceRead（schemas.py:26-36）
{"id": 1, "name": "nas-a", "type": "mount", "root_path": "/mnt/logs",
 "read_only": true, "copy_to_local": true, "enabled": true,
 "description": "", "created_at": "…iso…"}

// POST /api/data-sources/1/test → 200（schemas.py:39-57）
{"exists": true, "is_dir": true, "readable": true,
 "writability": "read_only",          // read_only|writable|unknown（三态，§4）
 "read_only_confirmed": true}         // 仅 writability=="read_only" 时可为 true
```

### 15.3 规则集（keyword + regex 命名组 + threshold）
```jsonc
// POST /api/rule-sets → 201；不传 version（服务端 max+1，rule_sets.py:38-39）
{"name": "demo", "config": {
  "name": "demo",                      // RuleSetConfig（models.py:55-67）
  "file_glob": "**/*",
  "encoding": "utf-8", "fallback_encoding": "gb18030",
  "rules": [
    {"id": "oom", "enabled": true, "severity": "info", "tags": ["mem"],
     "match": {"type": "keyword", "any": ["OutOfMemoryError", "oom-kill"],
               "case_sensitive": false}},          // any/all 二选一（models.py:13-17）
    {"id": "slow", "severity": "info",
     "match": {"type": "regex", "pattern": "cost=(?<ms>\\d+)ms",
               "flags": "i", "timeout_ms": 50},    // flags 仅 i/m/s 子集（evaluator.py:123-136）
     "threshold": {"field": "ms", "op": "gt", "value": 1000}}  // field 必须是命名组
  ]                                                  // keyword 规则配 threshold → 422
}}
// GET /api/rule-sets/1 → RuleSetDetailRead 加 "config":{…} 与 "created_by":"admin"
```

### 15.4 作业创建与幂等
```jsonc
// POST /api/jobs   头：Authorization: Bearer …；Idempotency-Key: case-001
{"name": "may-scan", "data_source_id": 1,
 "source_path": "/mnt/logs/2026/05",   // 存储值=str(resolve(请求值))，jobs.py:114,157
 "rule_set_id": 1,
 "file_filter": {"recursive": true, "max_depth": 5,
                 "suffixes": null, "max_files": 20000}}   // 可整体缺省

// 首次 201 / 同 key 同体重放 200（routes_jobs.py:86-89）——JobRead：
{"id": 1, "name": "may-scan", "data_source_id": 1,
 "source_path": "/mnt/logs/2026/05", "rule_set_id": 1,
 "status": "running",                  // 命中 0 文件则创建即 "succeeded"（jobs.py:164-166）
 "total_files": 3, "total_bytes": 10240,
 "cancel_requested": false, "created_by": "admin",
 "created_at": "…", "started_at": "…", "finished_at": null, "error": null}

// 同 key 异体 → 409 {"detail":"Idempotency-Key was already used with a different request body"}
// 空白 key → 400 {"detail":"Idempotency-Key must not be blank"}
// 可写挂载(enforce) → 409 {"detail":"source path … is mounted writable; refusing to process"}
```

### 15.5 SSE 首帧（GET /api/jobs/1/events）
```
event: progress
data: {"job_id": 1, "status": "running", "total": 3, "by_status": {"pending": 2, "running": 1}, "processed_lines": 1000, "matched_rows": 5}

```
（帧尾为 `\n\n`；无 ping/心跳行；终态帧发出后流自然结束，§6.5）

### 15.6 命中与错误形状
```jsonc
// GET /api/matches?job_id=1&limit=50 → 裸数组（MatchRead，schemas.py:165-182）
[{"id": 10, "job_id": 1, "file_task_id": 3, "rule_id": "slow", "rule_version": 1,
  "source_path": "/mnt/logs/2026/05/a.zip", "member_path": "app.log",
  "file_name": "a.zip", "line_no": 42,
  "raw_line": "…(≤4096 UTF-8 字节，超出则截断)", "raw_truncated": false,
  "fields": {"ms": "1500"},             // 仅 regex 命名组；keyword 恒 {}
  "numeric_value": 1500, "severity": "info", "tags": [], "created_at": "…"}]

// 缺 scope → 422 {"detail":"either job_id or both created_from and created_to are required"}（字符串体）
// 框架校验 → 422 {"detail":[{"loc":["body","password"],"msg":"…","type":"…"}]}（数组体）
// 典型 404 均为此形状：{"detail":"job not found"} / "rule set not found" /
//   "data source not found" / "worker not found" / "file task not found" / "user not found"
```

### 15.7 用户创建（RBAC 冒烟用）
```jsonc
// POST /api/users → 201（username 模式见 schemas.py:267-274；password 8-256）
{"username": "op1", "password": "***", "nickname": "",
 "is_active": true, "role_ids": [2]}
// UserRead: {"id":2,"username":"op1","nickname":"","is_active":true,
//   "created_at":"…","roles":[{"id":2,"code":"operator","name":"…"}]}
// PUT /api/users/2/roles 体是 {"role_ids":[3]}；PUT /api/roles/3/menus 体是 {"menu_ids":[…]}，均 204 空体
// POST /api/users/2/reset-password 体 {"password":"…"} → 204 空体
```
