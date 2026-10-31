# LogHarvest Service

M0 核心引擎 + M1a 控制面 API + M1b Worker/队列 + M2a 认证/运维 + M2c 安全/可观测性：规则编译/匹配、压缩包发现与流式读取、Worker 任务处理器（成员级 `partial`）、七张核心表 SQLModel 模型；数据源、规则集版本、作业规划、文件任务、结果与系统状态 REST API；PostgreSQL `SKIP LOCKED` 任务队列、Worker 常驻领取循环、SSE 作业进度与 Worker drain/resume 管理；JWT 登录与管理员种子、Alembic 迁移与启动接线、取消/永久失败/手动重试语义、概览统计与服务端维护入口。

Worker 与队列需要 PostgreSQL；控制面 API 测试仍使用内存 SQLite，无需外部服务。前端（M2b）见 [`../web/`](../web/)。

## 环境要求

- Python 3.12（仓库内 `service/.python-version` 已固定为 `3.12`，uv 会优先使用该版本）
- [uv](https://docs.astral.sh/uv/)

## 开发

```bash
cd service
uv sync --python 3.12
uv run --python 3.12 pytest
uv run --python 3.12 ruff check src tests scripts
uv run --python 3.12 uvicorn src.main:app --reload --port 8000
```

`pyproject.toml` 的 `addopts` 已包含 `-q`；再追加 `-q` 会连汇总行一起隐藏，故命令中不重复传入。

健康检查（无需认证）：

```bash
curl http://127.0.0.1:8000/api/health
# {"status":"ok","service":"logharvest"}
```

依赖锁定：`service/uv.lock` 已提交，`uv sync --python 3.12` 会按锁文件安装可复现的环境。该锁文件使用阿里云 PyPI 镜像（`https://mirrors.aliyun.com/pypi/simple/`）生成；使用默认 PyPI 的贡献者如需切换索引，可重新执行 `uv lock` 并提交更新后的锁文件。

## 首次启动

应用启动时（`LOGHARVEST_AUTO_CREATE_TABLES=true`，默认值）执行 `alembic upgrade head`，因此本地起服务前不需要手工建表：

```bash
cd service
cp ../.env.example .env                       # 按需修改 LOGHARVEST_* 变量
uv run --python 3.12 alembic upgrade head     # 可选：先手工把库升到 head
uv run --python 3.12 -m scripts.seed_admin    # 创建/补齐管理员账号（幂等）
uv run --python 3.12 uvicorn src.main:app --reload --port 8000
```

`scripts/seed_admin` 从 `LOGHARVEST_ADMIN_PASSWORD` 取口令（未设置则交互式询问），少于 8 位直接拒绝并返回 1；已存在同名用户时只补角色关联，不覆盖已有口令。

登录并调用受保护接口：

```bash
curl -s -X POST http://127.0.0.1:8000/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<seed_admin 里的口令>"}'
# {"code":0,"message":"ok","data":{"accessToken":"...","refreshToken":"...","expires":1792...,"roles":["admin"],...}}

curl -s http://127.0.0.1:8000/api/jobs -H "Authorization: Bearer <accessToken>"
```

## 配置

变量按 `LOGHARVEST_` 前缀从环境变量与 `service/.env` 读取（复制方式见上面"首次启动"）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LOGHARVEST_ENVIRONMENT` | `dev` | 运行环境标识；`dev`/`test` 之外的环境会强制下面的 JWT 与管理员口令 |
| `LOGHARVEST_JWT_SECRET_KEY` | `""` | HS256 签名密钥，**最短 32 字符，任何环境都必填**（含 `dev`）。留空或过短都会在配置校验阶段直接报错启动失败；代码里不再有任何内置开发密钥。生成示例：`python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `LOGHARVEST_ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | `accessToken` 有效期（分钟） |
| `LOGHARVEST_REFRESH_TOKEN_EXPIRE_DAYS` | `7` | `refreshToken` 有效期（天） |
| `LOGHARVEST_ADMIN_USERNAME` | `admin` | `scripts/seed_admin` 创建的管理员用户名；登录本身只校验库中已有的用户 |
| `LOGHARVEST_ADMIN_PASSWORD` | `""` | 种子脚本读取的口令来源；非 `dev`/`test` 环境必须显式配置（留空在配置校验阶段报错），少于 8 位时种子脚本拒绝执行 |
| `LOGHARVEST_DATABASE_URL` | `sqlite:///./logharvest.db` | **数据库分层**：本地开发/控制面单测默认 SQLite（相对路径基于启动目录；API 测试用内存 SQLite）；生产与 e2e 一律指向 **PostgreSQL 18** 连接串。**启动 Worker 必须改为 PostgreSQL 连接串**——队列 SQL（SKIP LOCKED / ON CONFLICT / advisory lock）是 PG 专有语法，且引擎层只对 PG 附加连接池加固（pre_ping/容量/回收） |
| `LOGHARVEST_AUTO_CREATE_TABLES` | `true` | 现在表示**应用启动时执行 `alembic upgrade head`**（`src/main.py` 的 lifespan），不再是"未接入的开关"；置为 `false` 时启动不动表结构。测试夹具通过 autouse fixture 强制关掉它，改用内存 SQLite 的 `create_all`，因此跑测试不会碰到开发库 |
| `LOGHARVEST_WORKER_ID` | 主机名 | Worker 标识；同机启动多个 Worker 进程时必须逐个设为唯一值，否则会共用 `workers` 表的同一行 |
| `LOGHARVEST_WORKER_VERSION` | `0.1.0` | Worker 版本号，注册与任务尝试记录都会写入 |
| `LOGHARVEST_WORK_DIR` | 系统临时目录 | 本地临时盘根目录，压缩包复制到 `<work_dir>/logharvest/<worker_id>/<task_id>/` |
| `LOGHARVEST_WORKER_POLL_SECONDS` | `1.0` | 队列为空时的轮询间隔 |
| `LOGHARVEST_MATCHES_BATCH_SIZE` | `1000` | 命中结果批量写入条数，已接线到 `TaskProcessor.batch_size` |
| `LOGHARVEST_MAX_CONCURRENT_COPIES` | `4` | 单机并发预算，仅作运维声明与展示；M1b 的并发由启动的 Worker 进程数实现（一个进程一次处理一个任务），进程内并发池未实现 |
| `LOGHARVEST_MAX_MATCHES_PER_TASK` | `200_000` | 单任务命中上限；达到上限后停止扫描，任务落 `failed` 且不自动重试，并删除本次尝试已写入的 `matches`，避免半截结果可见 |
| `LOGHARVEST_NAS_READ_ONLY_POLICY` | `enforce` | NAS 只读守卫策略：`enforce`（判定为可写就拒绝处理）/ `warn`（只告警）/ `ignore`（完全沉默）。**Windows 上挂载表读不到，判定恒为 `unknown`**，此时 `enforce` 不会拦截（详见"PostgreSQL 依赖与验证边界"） |
| `LOGHARVEST_ARCHIVE_MAX_MEMBER_BYTES` | `1073741824`（1 GiB） | 解压预算之一：单个成员的解压字节上限；超限只丢弃该成员，其余成员照常产出，任务落 `partial` |
| `LOGHARVEST_ARCHIVE_MAX_TOTAL_BYTES` | `4294967296`（4 GiB） | 整包累计解压字节上限；超限停止扫描（`failed` 且不重试） |
| `LOGHARVEST_ARCHIVE_MAX_MEMBERS` | `10000` | 单个归档的成员个数上限；超限停止扫描 |
| `LOGHARVEST_ARCHIVE_MAX_RATIO` | `500.0` | 膨胀比上限（累计解压量 ÷ 源文件字节）。为避免误伤正常小文件，累计解压量低于 64 MiB 时不参与判定 |
| `LOGHARVEST_LOGIN_MAX_ATTEMPTS` | `10` | 同一"用户名 + 来源 IP"在一个窗口内允许的**失败**登录次数；超过返回 `429` 与 `Retry-After`。成功登录会清空该来源的计数 |
| `LOGHARVEST_LOGIN_WINDOW_SECONDS` | `300` | 上述失败计数的滑动窗口（秒）。计数在进程内存中，**多副本部署时实际放行量是阈值 × 副本数** |
| `LOGHARVEST_TRUSTED_PROXY_CIDRS` | `""` | 可信反向代理地址/CIDR，逗号分隔。只有 TCP 对端落在此列表内时才解析 `X-Forwarded-For`/`X-Real-IP`；默认空 = 不信任任何转发头。经 nginx 部署时必须填入 nginx 容器地址或网段，否则登录限流仍按代理 IP 分桶 |

## 数据库迁移

DDL 的所有权已经从 `create_all` 交给 Alembic（`alembic.ini` + `service/alembic/`）。`alembic/env.py` 用 `settings.database_url` 建同步引擎（SQLite 走 `connect_args_for`），目标 metadata 是 `SQLModel.metadata`。

| 版本 | 职责 |
|---|---|
| `0001_baseline` | 一次建出 M1 的七张业务表（`data_sources`、`rule_sets`、`jobs`、`file_tasks`、`file_task_attempts`、`matches`、`workers`）。生产库尚不存在，这里有意使用 `SQLModel.metadata.create_all(bind, tables=BUSINESS_TABLES)` 捷径 |
| `0002_auth_tables` | 新增 `users`/`roles`/`user_roles`，写成显式 `op.create_table`/`op.create_index`，不复用捷径，保证可 `downgrade` |
| `0003_matches_rule_index` | 给 `matches` 补 `(rule_id, created_at)` 索引（规格要求的三条索引之一）。建索引前先 `inspect` 是否已存在：0001 的 `create_all` 捷径已经让新库在基线就带上它，只有 0002 之前升上来的库才真的缺 |
| `0004_rbac_menus` / `0005_operation_logs` | RBAC 菜单表与操作日志表；索引分别随表创建 |
| `0006_file_task_queue_indexes` | 给 `file_tasks` 补领取 `(scheduled_at, id) WHERE status='pending'` 与僵死回收 `(heartbeat_at) WHERE status='running'` 两条部分索引。0001 的新库基线已带索引，迁移只补旧库；`downgrade` 可删除 |

```bash
cd service
uv run --python 3.12 alembic upgrade head   # 幂等：第二次不重复建表
uv run --python 3.12 alembic current        # 0006 (head)
```

启动接线：`src/infrastructure/migrations.py` 的 `run_migrations()` 由 `src/main.py` 的 lifespan 在 `LOGHARVEST_AUTO_CREATE_TABLES=true`（默认）时调用，置 `false` 则启动不动 schema。

**测试例外**：`tests/conftest.py` 有一个 autouse fixture 把 `auto_create_tables` 强制设为 `False`，每个用例改用 `src/infrastructure/database.py:create_db_and_tables()` 在内存 SQLite 上建表。原因是不让测试碰开发库（也避免每个用例跑一遍迁移）。副作用是 `create_all` 会跟着模型自动同步，新增表时忘了写迁移不会被测试发现；为此 `tests/infrastructure/test_migrations.py` 会把 `alembic upgrade head` 的结果逐表、逐列、逐索引与 `SQLModel.metadata` 比对，漏写迁移直接失败。

`alembic upgrade head` → `alembic current`（`0006 (head)`）→ `alembic downgrade base`（回到只剩 `alembic_version`）已在临时 SQLite 文件上真实执行确认；**未在 PostgreSQL 上验证过**（见"PostgreSQL 依赖与验证边界"）。0006 的两条部分索引已在 `tests/infrastructure/test_migrations.py` 中按名称断言。

## 认证

M2a 起改为 JWT 登录。静态 `LOGHARVEST_ADMIN_TOKEN` 已从认证路径与配置字段中一并移除：不存在任何「静态 token 也能过」的后门，`Settings` 里也没有这个字段。

流程：

1. `POST /api/login`（`{"username": "...", "password": "..."}`）取 `accessToken`/`refreshToken`。登录**只校验、不创建用户**：库里没有任何用户时返回 `401`（不是 `500`），管理员由 `scripts/seed_admin` 建立。
2. 受保护接口带 `Authorization: Bearer <accessToken>`；只有 `GET /api/health`、`GET /api/health/ready`、`POST /api/login` 与 `POST /api/refresh-token` 不需要凭证（`/api/auth/routes` 与 `/api/logout` 需要）。
3. `POST /api/refresh-token`（`{"refreshToken": "..."}`，字段名是 camelCase 以对齐前端基座）换一对新的 access/refresh。两类令牌带不同的 `type` 声明，不能互换使用。
4. `POST /api/logout` 只回一个空数据信封：服务端没有会话、没有 token 黑名单，令牌靠 `exp` 自然过期（access 默认 30 分钟）。

令牌与口令：

- HS256，声明为 `sub`（用户 id 的字符串形式）、`username`、`type`（`access` / `refresh`）、`exp`。
- 签名密钥只取自 `LOGHARVEST_JWT_SECRET_KEY`（最短 32 字符），**任何环境留空都拒绝启动**。此前 dev 有一个内置回退常量，而 `environment` 默认就是 `dev`——忘记配置环境的部署会接受用该公开常量伪造的管理员 token，所以常量连同回退逻辑一起删除。本地开发必须显式给一把密钥（生成命令见 `.env.example`）。
- 口令存 bcrypt；超过 72 字节的口令先做 SHA-256 再哈希，避免 bcrypt 静默截断尾部差异。

| 状态码 | 场景 | `detail` |
|---|---|---|
| `401` | 缺少 Bearer 头，或 scheme 不是 `bearer` | `missing bearer token` |
| `401` | 签名不符、已过期或 `type` 不匹配 | `invalid or expired token` |
| `401` | `sub` 不是数字用户 id | `invalid token payload` |
| `401` | 用户不存在或 `is_active=false` | `user is not available` |
| `401` | 登录时用户名或口令错误 | `invalid credentials` |
| `403` | 口令正确但账号被停用 | `user is disabled` |
| `403` | 已认证但缺少该接口要求的权限码 | `permission '<code>' required` |

受保护接口返回的 `401`（认证依赖发出）带 `WWW-Authenticate: Bearer` 头；`POST /api/login` 的 `401` 是普通 `HTTPException`，只有 `{"detail": "invalid credentials"}`，不带该头。

**响应信封**：只有 `POST /api/login`、`POST /api/refresh-token`、`GET /api/auth/routes`、`POST /api/logout` 返回 `{"code": 0, "message": "ok", "data": ...}`（前端基座按 `data.code === 0` 判定）；其余业务接口保持纯 REST 对象与 `{"detail": ...}` 错误体，不包信封。

**RBAC（菜单即权限）**：`menus` 表同时承载目录、页面与权限节点（`menu_type` 0/1/2），权限码是 `menu_type=2` 的 `name`（如 `jobs:read`）。`roles` 增加 `description`/`is_active`/`is_system`，`role_menus`、`user_roles` 建立角色-菜单与用户-角色多对多。启动时幂等种子写入 **3 个目录（任务管理 `/logharvest`、规则管理 `/rules`、系统管理 `/system`）、13 个页面、19 个权限节点**和 admin/operator/viewer 三个默认角色；已存在的数据不会被覆盖。内置菜单行按 IA 声明**重塑**：`parent_id`/`menu_type`/`path`/`component` 归位（管理端本来就改不了这些），`title`/`rank`/`icon` 只在现值仍等于旧默认时才更新，因此管理员的定制不会被冲掉；用户自建菜单只做补缺失。admin 是内置保护角色（`is_system`），operator/viewer 是可编辑的默认角色（仅在首次种子创建，删除后重启不会重建）。admin 不依赖授权表，权限解析返回通配 `*:*:*`；其他用户只拿到 active 角色关联的 active 权限节点。登录返回 `roles`/`permissions`；`GET /api/auth/routes` 按当前用户生成动态菜单/路由（没有可见子页面的目录会被剔除），页面 `meta.auths` 携带有效权限码供前端按钮级控制。业务接口按细粒度权限码校验（如 `jobs:read`/`jobs:write`/`jobs:operate`），SSE 使用短会话的流式权限依赖，不在连接期间占用请求级 Session。内置 admin 角色不可删除、不可停用、菜单授权只读；用户/角色/菜单管理接口见下表。设计与验收记录（范围扩大评审、边界保护矩阵、真实 PG 与真实浏览器证据、已知限制）见 [`docs/superpowers/specs/2026-10-27-logharvest-rbac-menus-permissions-design.md`](../docs/superpowers/specs/2026-10-27-logharvest-rbac-menus-permissions-design.md)，本轮 IA 重构与操作日志见 [`docs/superpowers/specs/2026-10-27-logharvest-menu-ia-and-operation-log-design.md`](../docs/superpowers/specs/2026-10-27-logharvest-menu-ia-and-operation-log-design.md)。

### 用户 / 角色 / 菜单管理

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/users` | 用户列表（裸数组），支持 `keyword`（用户名/昵称）与 `limit`/`offset`；每行带 `roles` |
| `POST` | `/api/users` | 创建用户（用户名唯一且限 `[A-Za-z0-9][A-Za-z0-9_.@-]{1,49}`，口令至少 8 位，可带 `role_ids`） |
| `PUT` | `/api/users/{id}` | 修改昵称/启用状态；禁止停用自己 |
| `PUT` | `/api/users/{id}/roles` | 覆盖用户角色集合；移除最后一名 active admin 的 admin 角色返回 `409` |
| `POST` | `/api/users/{id}/reset-password` | 重置口令（204） |
| `DELETE` | `/api/users/{id}` | 删除用户（204）；禁止删除自己或最后一名 active admin |
| `GET` | `/api/roles` | 角色列表（裸数组） |
| `POST` | `/api/roles` | 创建角色；`code` 唯一且不可修改 |
| `PUT` | `/api/roles/{id}` | 修改名称/描述/启用状态；名称传空白返回 `422`；系统角色（admin）不可停用 |
| `DELETE` | `/api/roles/{id}` | 删除角色（204）；系统角色或仍被用户引用时 `409` |
| `GET` | `/api/roles/{id}/menus` | 角色已授权菜单 id；admin 返回全部菜单 id |
| `PUT` | `/api/roles/{id}/menus` | 覆盖角色菜单授权（204）；系统角色 `409` |
| `GET` | `/api/menus` | 菜单平铺列表（裸数组） |
| `GET` | `/api/menus/tree` | 菜单树（目录/页面/权限节点） |
| `POST` | `/api/menus` | 新建自定义菜单；校验目录→页面→权限的父子关系 |
| `PUT` | `/api/menus/{id}` | 更新菜单；名称/标题传空白返回 `422`；内置菜单只允许改标题/图标/排序/显示/启用 |
| `DELETE` | `/api/menus/{id}` | 删除自定义叶子菜单（204）；内置菜单或有子节点时 `409` |
| `GET` | `/api/operation-logs` | 操作日志（需 `audit:read`，operator 有、viewer 没有）：`actor_id`/`action` 前缀/`resource_type`/`resource_id`/`created_from`+`created_to`（成对，否则 `422`）+ `limit`/`offset`，按时间倒序返回裸数组。表只追加，没有写接口也没有删除接口 |

管理接口全部要求对应的 `users:*` / `roles:*` / `menus:*` 权限；未授权返回 `403`。权限变更在重新登录后反映到前端菜单与 `permissions` 快照，后端接口权限实时生效。

**权限码的唯一来源**是 `src/domain/permissions.py`（`Permissions` 常量与通配码 `*:*:*`）：路由声明、启动种子、前端按钮权限三处共用这一份定义。加权限时先在那里加常量，再补种子条目——`tests/api/test_rbac_core.py::test_every_declared_permission_code_is_seeded` 会在权限码声明了却没有种子节点时立刻失败。写错权限码没有任何编译期报错，只会让接口对所有人返回 403。

**动态路由的 component 契约**：后端菜单的 `component` 必须是 `src/views` 下的相对路径（页面写 `logharvest/jobs/index`，目录留空）。前端按它在 `import.meta.glob` 里找组件，找不到会兜底成空布局并在控制台报错，**不会**在构建期失败——改菜单数据前先确认路径存在。

## API（M1a 控制面 + M1b 队列/进度 + M2a 认证/运维）

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/health` | 存活检查（liveness），无需认证；只证明进程在运行 |
| `GET` | `/api/health/ready` | 就绪检查（readiness），无需认证；执行 `SELECT 1`，数据库不可用时返回 `503` |
| `POST` | `/api/login` | 用户名口令换 `accessToken`/`refreshToken`；返回信封，`roles`/`permissions` 在 `data` 里 |
| `POST` | `/api/refresh-token` | 用 `refreshToken` 换新令牌对；返回信封 |
| `GET` | `/api/auth/routes` | 需认证；按当前用户角色返回动态菜单/路由树（目录无可见页面时剔除），页面 `meta.auths` 带有效权限码；返回信封 |
| `POST` | `/api/logout` | 需认证；只回 `data: null`，服务端无会话；返回信封 |
| `GET` | `/api/data-sources` | 数据源列表，`limit`/`offset` 分页 |
| `POST` | `/api/data-sources` | 创建数据源；名称重复返回 `409`；`read_only` 只接受 `true` |
| `POST` | `/api/data-sources/{id}/test` | 检查 `root_path` 的 `exists` / `is_dir` / `readable` |
| `GET` | `/api/rule-sets` | 规则集轻量列表（不含 `config`/`created_by`），按名称、版本排序，`limit`/`offset` 分页 |
| `GET` | `/api/rule-sets/{id}` | 规则集详情，返回完整 `config` 与 `created_by`；不存在返回 `404` |
| `POST` | `/api/rule-sets` | 创建规则集新版本（同名 `max(version)+1`），不覆盖旧版本；配置校验失败返回 `422` |
| `GET` | `/api/jobs` | 作业列表，`limit`/`offset` 分页；`status` 下推到 SQL（非法状态值 `422`） |
| `POST` | `/api/jobs` | 创建作业并同步扫描规划；可选 `Idempotency-Key` |
| `GET` | `/api/jobs/{id}` | 作业详情 |
| `GET` | `/api/jobs/{id}/files` | 作业的文件任务，`limit`/`offset` 分页；可选 `status` 精确筛选（`pending`/`running`/`succeeded`/`partial`/`failed`/`cancelled`），值不在枚举内返回 `422`；条目含 `status`、`attempts`、`max_attempts`、`worker_id`、`heartbeat_at`、`error` |
| `POST` | `/api/jobs/{id}/cancel` | 请求取消作业 |
| `POST` | `/api/jobs/{id}/files/retry-failed` | 把该作业处于 `failed` 的文件任务放回队列（置 `pending`、清 `error`/`worker_id`/`finished_at`、`scheduled_at=now()`），`attempts` 历史保留、不重置；返回 `{retried: n, remaining_failed: m}`（单次最多 200 条，`remaining_failed` 是本次未取走的剩余数）；确有任务入队且作业处于终态时，作业被拉回 `running` 并清 `finished_at`，否则终态汇总看不到它；作业不存在 `404`，**已取消**（`cancel_requested` 或 `cancelled`）返回 `409` —— `CLAIM_SQL` 永不再领取取消作业的任务，重新入队会让它永久滞留 |
| `GET` | `/api/matches` | 命中结果；必须提供 `job_id` 或 `created_from` + `created_to`，支持 `rule_id`、`file_name`、`severity`、`(created_at,id)` 游标与 `limit` |
| `GET` | `/api/stats/jobs` | 任务统计（需 `jobs:read`）：`days` 1–90、可选 `data_source_id`；返回 `totals` / `daily`（UTC 日界）/ `top_rules`（带 `rule_version`），单对象非数组 |
| `GET` | `/api/rules` | 跨版本规则索引（需 `rulesets:read`）：查询期展开 `rule_sets.config`，支持 `keyword`（字面量匹配，`%`/`_` 不当通配）、`type=keyword|regex`、`rule_set_name`、`limit`/`offset`，返回裸数组 |
| `GET` | `/api/workers` | Worker 列表，`limit`/`offset` 分页（不包含心跳/管理写接口） |
| `POST` | `/api/workers/{id}/drain` | 请求排空；返回 `{id, drain_requested, status}`，离线 Worker 只记录标记、状态保持 `offline` |
| `POST` | `/api/workers/{id}/resume` | 取消排空并置为 `online`；对 `offline` Worker 返回 `409` |
| `POST` | `/api/workers/drain-version?version=` | 按版本批量排空；返回 `{drained, flagged}`，`drained` 为在线转 `draining` 的数量，`flagged` 为仅记录标记的离线数量 |
| `GET` | `/api/jobs/{id}/events` | SSE 作业进度，`text/event-stream`；见下文 |
| `GET` | `/api/system/status` | 文件任务状态计数、最老 pending 等待秒数、在线 Worker 数 |
| `GET` | `/api/system/overview` | 概览统计：`{jobs: {status: 数量}, file_tasks: {status: 数量}, matches_24h, workers: {online, by_version: {version: 数量}}, oldest_pending_seconds}`；`workers.online` 统计 `online`/`busy`/`draining`，`by_version` 包含 `offline` 行（升级时用来确认旧版本清零）；`oldest_pending_seconds` 是最老 `pending` 文件任务的等待秒数（整数），没有 pending 时为 `null` |
| `POST` | `/api/system/maintenance` | 立即执行一次服务端补偿：`{recovered, workers_demoted, jobs_finalized}`（回收僵死任务 / 把心跳超时的 Worker 置 `offline` / 收尾全部文件任务已终态的作业）；可重复执行，无待处理项时三项均为 `0`。**队列不是 PostgreSQL 时返回 `503`** 并说明原因（回收语句是 PostgreSQL 专有语法），而不是抛未处理异常；见"服务端维护入口" |

所有列表接口都分页：`limit` 默认 `50`、`offset` 默认 `0`；数据源、规则集、作业、文件任务与命中结果的 `limit` 最大 `500`，Worker 最大 `200`；`limit` 超出上限或 `offset` 为负数返回 `422`。`GET /api/matches` 使用 `(created_at, id)` 游标而不是 `offset`：查询参数为 `created_before` + `id_before`（必须成对提供，取上一页最后一条的 `created_at` 与 `id`），按 `created_at DESC, id DESC` 排序，`limit` 默认 `50`、最大 `500`。

列表接口的响应是**纯 JSON 数组**（业务接口不包 `{code,message,data}` 信封，也没有 `{items, total}` 包装，总数不在响应里，需要总数时用 `GET /api/system/overview` 的分组计数）。`GET /api/jobs/{id}/files` 的 `status` 筛选与 `limit`/`offset` 叠加，语义是先按状态过滤再分页。

规则集创建时会调用运行时编译器与 Worker 输入校验：正则语法、flags（仅 `i`/`m`/`s`）、threshold 命名分组、`file_glob`（gitwildmatch）、`encoding`/`fallback_encoding` 编码名；任一非法返回 `422`，不会写入数据库。

### 创建作业流程

1. `POST /api/data-sources` 登记 NAS 挂载点 `root_path`（默认 `read_only=true`、`copy_to_local=true`）。
2. `POST /api/rule-sets` 提交规则配置（关键字/正则、`file_glob`、编码与回退编码），得到 `rule_set_id` 与 `version`；同名再次提交会新增版本。
3. `POST /api/jobs` 提交 `name`、`data_source_id`、`source_path`、`rule_set_id`，可选 `file_filter`（默认 `recursive=true`、`max_depth=5`、`max_files=20000`）。`suffixes` 省略或为 `null` 时使用压缩包默认后缀集合；显式传入时先与压缩包后缀集合求交，只保留 zip/tar/gz/bz2/xz 等可解压格式，交集为空（例如只传 `.log`/`.txt`）返回 `400`，原始文本文件不会进入作业。可选请求头 `Idempotency-Key`。
4. 服务解析并校验路径：`source_path` 必须位于数据源 `root_path` 之内（路径逃逸 `400`），且必须是已存在目录（`400`）。
5. 在请求内同步递归扫描该目录，识别 zip、tar 系列、单文件 gz/bz2/xz；跳过符号链接和点目录。`max_depth` 是静默截断：相对深度达到 `max_depth` 的目录不再下探（该层文件仍会纳入），不会报错；扫描在发现第 `max_files + 1` 个压缩文件时立即中止并返回 `400`，不再遍历剩余目录；扫描超过 10 秒返回 `503`。扫描前会结束当前数据库读事务，避免长时间占用连接。`max_depth` 超出 1–20 或 `max_files` 超出 1–20000 属于 schema 校验失败，返回 `422` 且不开始扫描。
6. 创建一条 `Job`，并为每个压缩文件创建一条 `FileTask`（`pending`，`job_id + source_path` 唯一），累计 `total_files` / `total_bytes`。目录为空或未发现匹配的压缩文件时直接 `succeeded`；发现压缩文件时初始为 `running`，由 Worker 领取循环消费并在全部文件任务进入终态后汇总作业终态。
7. 相同管理员使用相同 `Idempotency-Key` 再次提交返回已有作业（`200`）；键会去除首尾空白后参与比较与入库，空白字符串返回 `400`。相同键但 `name`/`data_source_id`/`source_path`/`rule_set_id`/`file_filter` 归一化后不一致返回 `409`。并发插入冲突时会回查并返回已创建作业。
8. `POST /api/jobs/{id}/cancel` 将 `pending` 文件任务置为 `cancelled` 并设置 `cancel_requested=true`；若没有 `running` 任务则作业立即 `cancelled`，已处于终态的作业不做变更。仍有 `running` 任务时，作业由该任务完成后的终态汇总收尾（`cancel_requested` 为真时汇总为 `cancelled`）。

## Worker 运行时

```bash
cd service
# 队列只在 PostgreSQL 上运行；Worker 必须指向 PostgreSQL 连接串
LOGHARVEST_DATABASE_URL=postgresql://logharvest:logharvest@127.0.0.1:5432/logharvest \
  uv run --python 3.12 python -m src.worker
```

`SIGTERM`/`SIGINT` 触发优雅退出：完成当前任务后把 `workers.status` 置为 `offline`。

领取循环（`src/worker/main.py` → `src/application/worker/processor.py`）：

1. 注册 Worker（`online`），启动即执行一次补偿：僵死回收（`heartbeat_at` 落后 120 秒的 `running` 任务）、把心跳超时的 Worker 置 `offline`、作业终态补偿扫描。
2. 每轮先读 `workers.drain_requested`：为真时只发送 `draining` 心跳、不领取新任务，当前任务跑完即停。
3. `CLAIM_SQL` 用 `FOR UPDATE SKIP LOCKED` 原子领取一条 `scheduled_at <= now()` 的 `pending` 任务（已请求取消的作业不再领取），`attempts + 1` 并写入 `file_task_attempts`；领取同时把规划时记录的 `size_bytes`/`mtime` 带回给处理器。
4. **源文件校验（复制之前）**：只读 `stat` 一次，源文件不存在、`size_bytes` 与规划不符、`mtime` 与规划不符（按秒比较，naive 时间列按 UTC 解释）任意一条成立即抛 `PermanentTaskFailure`，走永久失败路径，不进入复制。
5. 复制到本地临时盘：1 MiB 分块，至少每 5 秒 `touch` 续租（`id + attempts + status='running'` 栅栏）。续租返回假说明租约已被回收或接管，立即抛 `LeaseLost` 中止，不写任务终态、也不决定作业终态。
6. 流式解压与逐行匹配：每 20000 行或每 5 秒 `heartbeat` 续租并回写进度计数，因此被 `file_glob` 过滤掉的行同样会续租；命中按 `LOGHARVEST_MATCHES_BATCH_SIZE` 批量写入 `matches`。
7. **取消检查**：`cancel_check` 跟随心跳预算执行（每 20000 行或每 5 秒一次），不是逐行查询；为真即抛 `JobCancelled`、把该 `file_task` 写成 `cancelled`（并关闭当次尝试记录）后停止，不再评估剩余行。
8. 重试幂等：`attempts > 1` 时先 `delete_for_task` 清理上一次写入的命中，再重新写入。任务进入 `failed` 终态时也会在写终态前删除本次尝试的命中（`partial` 保留），所以失败任务不会留下半截结果。
9. 单任务命中上限：每写入一条命中前检查 `LOGHARVEST_MAX_MATCHES_PER_TASK`；达到上限即停止扫描，删除本次尝试命中并以 `failed + retryable=False` 收尾，错误文案包含 `max_matches_per_task`。`partial` 是成员级内容失败，保留已成功成员的结果。
10. 终态写回 `complete` 使用同一租约栅栏，迟到的完成不会覆盖已被回收的任务；`failed` 且未达 `max_attempts`（5）时退回 `pending`，`scheduled_at` 线性退避 `attempts * 5` 秒。`COMPLETE_SQL` 的自动重排还要求 `:retryable` 为真：`PermanentTaskFailure`/`SourceNotReadOnly`/命中上限以 `retryable=False` 落 `failed` 终态，**不自动重试**，等人工确认后用 `POST /api/jobs/{id}/files/retry-failed` 手动放回队列；`ESTALE`/`EIO`/`ETIMEDOUT` 等瞬时源错误以 `retryable=True` 重新入队。`failed` 且作业已请求取消时写成 `cancelled`，避免作业被永远悬在 `running`。
10. 任务处理成功后尝试汇总作业终态；队列为空时等待 `LOGHARVEST_WORKER_POLL_SECONDS` 再继续领取。Worker 不绑定作业，不会因为某个作业结束而退出。
11. 每 300 秒重复执行僵死回收 + 作业终态补偿，覆盖没有完成回调的路径。

成员级容错：zip 遇到损坏成员时记录错误并继续读取剩余成员；流式 tar 无法越过损坏成员，会在该处停止（`scan_incomplete`）。tarfile 会把后续坏 header 或块边界截断静默当作 EOF，因此 tar 扫描结束时还会额外校验零结束块，缺少结束标记同样标记 `scan_incomplete`，避免漏读成员却报告成功。处理器按 `file_glob` 汇总成员结果：至少一个匹配成员完整读完 → `partial`；所有匹配成员失败 → `failed`；两者均以 `retryable=False` 写终态，不自动重试，错误摘要写入 `file_tasks.error`。被 `file_glob` 排除且扫描仍能继续的 zip 成员损坏不影响任务状态；但流式 tar 的停止位置之后可能还有匹配成员，因此该错误始终计入。

租约栅栏（M1b 写栅栏，M2a 未放宽）：任何 `matches` 写库前都必须先确认 `(file_task_id, attempts, status='running')` 仍然成立（`_verify_lease` → `touch`），丢租约立即 `LeaseLost` 中止且不写终态；`delete_for_task` 之前同样先过栅栏。

手动重试的边界：`retry_failed_tasks` 只搬 `failed` 行、保留 `attempts`，所以一次手动重试仍受 `max_attempts` 约束（回收与 `COMPLETE_SQL` 的比较都是 `attempts < LEAST(max_attempts, 队列上限)`）；`cancelled` 的任务不会被放回。

并发模型：

- 一个 Worker 进程一次只处理一个任务；单机并行通过启动多个进程实现（默认 4、最多 6，内存压力时按 4→3→2 减少进程数）。
- `LOGHARVEST_MAX_CONCURRENT_COPIES` 目前是运维声明的单机并发预算（会展示在 Worker 记录中），进程内并发池尚未实现。
- 同一主机启动多个 Worker 进程时，必须为每个进程设置唯一的 `LOGHARVEST_WORKER_ID`；默认取主机名，多进程共用主机名会互相覆盖 `workers` 表中的同一行状态，并且启动时的本地残留清理可能删掉同 id 对端正在使用的复制目录（该任务会失败重试，NAS 数据不受影响）。
- NAS 只读：源文件仅以 `rb` 打开，不写入、不移动、不改属性；所有写入只发生在 `LOGHARVEST_WORK_DIR` 下按任务隔离的临时目录，任务结束（含失败）后删除。

## 作业终态汇总

| 条件 | `jobs.status` |
|---|---|
| 全部文件任务 `succeeded` | `succeeded` |
| 存在 `partial`（无论是否有 `succeeded`/`failed`） | `partial` |
| 无 `partial`，存在 `succeeded` 与 `failed` | `partial` |
| 无 `succeeded`/`partial`，存在 `failed` | `failed` |
| `cancel_requested` 为真 | `cancelled` |
| 文件任务全部被取消 | `cancelled` |

仅在作业仍为 `running` 且不存在 `pending`/`running` 文件任务时写入，同时设置 `finished_at`。并发汇总以 `jobs.status = 'running'` 行级栅栏保证单一写入者；触发点是完成任务的 Worker，兜底是 Worker 启动与每 300 秒的补偿扫描，以及下节的 `POST /api/system/maintenance`。文件任务级 `partial` 已实现：压缩包内部分成员损坏、其余匹配成员成功时写 `partial`，作业汇总保留 `partial`，即使全部任务都是 `partial` 也不会被吞成 `failed`。

## 服务端维护入口

设计方案 §7.2/§7.4 要求"Server 定时补偿兜底"：回收僵死任务、降级静默 Worker、收尾作业。Worker 循环里确实做了这三件事，但它们都跑在 Worker 进程内 —— **一个存活 Worker 都没有时（全部退出、还在滚动升级、队列被清空后进程被回收），没有人执行它们**，作业会一直悬在 `running`、任务一直占着租约。

`service/src/application/services/maintenance.py` 的 `run_maintenance(engine, stale_seconds=120)` 把同一段补偿搬到服务端，`POST /api/system/maintenance`（仅 `admin` 角色）立即执行一次并返回计数：

```json
{"recovered": 0, "workers_demoted": 2, "jobs_finalized": 1}
```

- `recovered`：`recover_stale` 回收的僵死 `running` 任务数（PostgreSQL 专有 SQL）。
- `workers_demoted`：`mark_dead_workers` 降级为 `offline` 并清空 `current_task_id` 的静默 Worker 数。
- `jobs_finalized`：`finalize_finished_jobs` 收尾的作业数。

三步都是幂等 SQL，可重复执行，因此**调度交给外部**（cron、systemd timer 或 compose job，例如每分钟一次）：本服务不内置定时器，也不引入 Redis/后台线程。接口没有请求体，`stale_seconds` 目前是服务层默认参数 120 秒，尚未暴露为查询参数。

验证边界：`mark_dead_workers` 与 `finalize_finished_jobs` 是可移植写法，已在内存 SQLite 上真实执行验证；`recover_stale` 是 PostgreSQL 专有语句（SQLite 上直接语法错误，已实测），已由 `tests/integration/test_postgres_queue.py` 在真实 PostgreSQL 上执行验证。

## SSE 作业进度

`GET /api/jobs/{id}/events`，`text/event-stream`：

- 未知作业在返回流之前给出 `404`（`application/json`），不会先发响应头再报错。
- 每秒轮询一次快照：

```text
event: progress
data: {"job_id":1,"status":"running","total":12,"by_status":{"pending":3,"running":1,"succeeded":8},"processed_lines":81234,"matched_rows":977}
```

- `max_events` 默认 `0`（持续到作业终态），`1..100` 为调试/测试用的有限流；非法值返回 `422`。
- 作业进入终态后发送最后一个快照再结束；作业在流中被删除时安静结束。
- 每次轮询使用独立的短生命周期 Session，长连接不会占用数据库连接。
- 该端点需要 `jobs:read` 权限（SSE 使用短会话的流式权限依赖）：浏览器的 `EventSource` **不能**设置 `Authorization` 头，所以前端必须用 `fetch` + 流式读取（`response.body.getReader()`）自己解析 `event:`/`data:` 帧，而不是 `EventSource`。本里程碑没有为 SSE 提供查询参数口令等替代鉴权方式。

## PostgreSQL 依赖与验证边界

- 队列 SQL（`FOR UPDATE SKIP LOCKED`、`make_interval`、`ON CONFLICT`、CTE `UPDATE ... RETURNING`）只能在 PostgreSQL 上执行。真实执行与 `SKIP LOCKED` 并发行为由 `tests/integration/test_postgres_queue.py` 覆盖（设置 `LOGHARVEST_TEST_PG_URL`，每个用例重建 schema）；SQLite 套件只验证可移植语句。
- PostgreSQL 连接会强制 `timezone=UTC`（`connect_args_for`）：时间戳列是 `without time zone`，SQL `now()` 写入的是会话时区的墙上时间，而 Python 侧写入 UTC。若会话不是 UTC，`heartbeat_at`、僵死回收阈值与"静默 Worker 降级"的比较会整体偏移，降级可能永远不触发。
- 作业终态汇总 SQL 使用可移植写法，已在真实建表的 SQLite 上验证聚合与栅栏语义。
- 表结构 DDL 已交给 Alembic（`0001_baseline`、`0002_auth_tables`、`0003_matches_rule_index`、`0004_rbac_menus`、`0005_operation_logs`、`0006_file_task_queue_indexes`），并在启动时按 `LOGHARVEST_AUTO_CREATE_TABLES` 执行 `upgrade head`；`create_db_and_tables()` 仍然存在，用途收缩为测试夹具建表。迁移只在临时 SQLite 文件上真实跑过 `upgrade head` / `current` / `downgrade base`，**没有在 PostgreSQL 上执行过**：`0001` 的 `create_all` 捷径与 `0002` 的显式 op 生成的语句在 PG 方言下的行为（`JSONB`、`BIGSERIAL`、`BIGINT` 列类型）仍待补验。
- **NAS 只读判定在非 POSIX 平台上恒为 `unknown`**（见 `src/domain/source_guard.py`）：无副作用的证据只有两处——POSIX 挂载表（`/proc/self/mounts`、`/etc/mtab`）与 `st_mode` 的写权限位。Windows / SMB 上读不到挂载表，而 SMB 挂载通常带回可写位，于是判定落到 `unknown`。后果是 `LOGHARVEST_NAS_READ_ONLY_POLICY=enforce` 在 Windows 开发机上**实际不会拦截任何数据源**（只有真的判为 `writable` 才拒绝处理），`warn` 则只打一条 `nas_read_only_not_confirmed` 日志。规格 §7.2 的"拒绝在可写挂载上处理"只能在 Linux + 只读挂载的环境里真正生效，**不要拿 Windows 开发机当作这条保护已生效的证据**；前端同源处理（`DataSourceTestResult.writability`）也把 `unknown` 显示成"只读未确认"而不是绿勾。

## 测试

### PostgreSQL 集成门禁（CI）

`tests/integration/` 默认跳过；CI 使用 `scripts/run_pg_integration.sh` 启动一次性 `postgres:18` 容器，
先对空库执行 `alembic upgrade head` 并断言 `file_tasks` 的领取/回收索引存在，再运行全部集成用例，
退出时删除容器。Jenkins 的「PostgreSQL 集成测试」阶段已接入该脚本。

```bash
cd service
bash scripts/run_pg_integration.sh   # 需要 Docker；不会接触 deploy/ 下的数据库
```

- API 测试（`tests/api/`、`tests/test_health.py`）使用内存 SQLite（`StaticPool`）与 FastAPI `dependency_overrides`，不需要 PostgreSQL；每个 API 用例通过 `create_db_and_tables()` 建表。`tests/conftest.py` 的 autouse fixture 会把 `settings.auto_create_tables` 置为 `False`，因此测试进程从不执行 Alembic，也不会碰开发库；`tests/conftest.py` 在任何 `src` 导入之前直接赋值注入 `LOGHARVEST_ENVIRONMENT=test` 与测试签名密钥（`Settings` 在模块导入时即完成校验），`auth_headers` 夹具再用 `settings.jwt_secret_key` 签发真实 access token。`tests/domain/` 与 `tests/application/` 为领域/应用单元测试，不经过 API 与依赖覆盖（模型测试自行使用内存 SQLite 验证建表）。
- `tests/api/` 覆盖 JWT 登录/刷新/登出/停用账号、数据源、规则集、作业规划/幂等/路径校验/取消/失败重试、结果查询、Worker 管理、SSE、系统状态与概览/维护接口；`tests/domain/services/` 覆盖 bcrypt 与 `TokenService`（含过期、错密钥、access/refresh 不可互换）；`tests/scripts/test_seed_admin.py` 覆盖管理员种子的幂等与弱口令拒绝；`tests/application/` 覆盖 Worker 处理器的复制续租、时间心跳、租约丢失、取消检查与永久失败；`tests/worker/` 用 fake 仓库覆盖领取循环、drain、注册、僵死回收、周期补偿、取消检查接线与信号退出；`tests/infrastructure/` 覆盖队列 SQL 形状、批量写入、作业终态汇总（后两者在真实建表的 SQLite 上执行）以及迁移结果与 `SQLModel.metadata` 的逐表逐列比对；`tests/integration/` 在真实 PostgreSQL 上覆盖 `SKIP LOCKED` 并发领取、租约栅栏、重试/取消/永久失败、成员级 `partial`、僵死回收、作业汇总与批量写入，以及用户/角色/菜单删除时外键关联的 flush 顺序（`test_postgres_rbac.py`）。
- 作业唯一约束、幂等并发回退、作业终态栅栏等并发语义在 SQLite 上验证；`SKIP LOCKED` 领取、`make_interval` 退避、`ON CONFLICT` 注册、`RECOVER_STALE_SQL` 回收等 PostgreSQL 行为由 `tests/integration/test_postgres_queue.py` 在真实 PostgreSQL 上验证。维护入口在测试里分两层：`tests/application/test_maintenance.py` 直接真实执行两步可移植清扫（降级静默 Worker、收尾作业）并断言 `run_maintenance` 在非 PostgreSQL 上拒绝；`tests/api/test_system.py` 只断言该场景返回 `503`。

## 质量门禁

```bash
uv run ruff check .
uv run mypy
uv run pytest -q
LOGHARVEST_TEST_PG_URL="postgresql+psycopg://logharvest:logharvest@127.0.0.1:5433/logharvest" \
  uv run pytest -q tests/integration
uv run --with coverage coverage run -m pytest -q
uv run --with coverage coverage report --include="src/*"
```

`mypy` 当前只覆盖 `src/domain`、`src/worker`、`src/application/worker`、`src/config`（见 `pyproject.toml` 的 `[tool.mypy].files`）：`src/api` 与 `src/application/services` 大量使用 SQLModel，mypy 无法正确推断主键 `int | None` 与列表达式，全量检查会产生成片假阳性。前端门禁为 `pnpm test`（vitest 单测）、`pnpm typecheck` 与 `pnpm build`，见 [`../web/README.md`](../web/README.md)。

### 协作测试门禁（2026-10-28 起）

每一次 AI 交互在允许提交代码前，必须执行前后端全部单元测试并全部通过；测试未通过不得提交，先定位修复再重跑验证。完整规范见仓库根目录 [`agent.md`](../agent.md)：

```bash
cd service && .venv/Scripts/python.exe -m pytest tests -q   # 后端全量（Windows/Git Bash）
cd web && pnpm test && pnpm typecheck                        # 前端单测 + 类型检查
```

覆盖率工具已入 venv（pytest-cov）：`pytest tests -q --cov=src --cov-report=term` 直接可用（2026-10-28 起，总覆盖 97%）。

### 边界/异常分支专项测试（2026-10-28 补全）

`tests/application/test_audit_service.py`（审计吞异常三分支 + 过滤矩阵）、`tests/application/test_analytics_edges.py`（日期归一/脏配置/摘要截断）、`tests/application/test_rbac_edges.py`（RBAC 保护规则/环防护/显式 None）、`tests/application/test_auth_navigation_edges.py`（无 id User 的退化输入、动态路由空目录）、`tests/domain/test_evaluator_edges.py`（regex 标志/阈值退化/防御分支）、`tests/domain/test_source_guard_edges.py`（挂载表退化/三态映射/非法策略）、`tests/domain/archive/test_reader_edges.py`（损坏容器/预算成员错误/源大小退化）、`tests/worker/test_runtime_edges.py`（失败结果日志/维护吞错/孤儿目录 stat 失败）、`tests/test_app_lifespan.py`（启动迁移 + 种子接线）。

## 范围

### M0（已交付）

- 规则编译/匹配：关键字 `any`/`all`、正则命名分组、数值门限、禁用规则、超时恢复。
- 压缩包发现与流式读取：递归发现、zip、tar.gz、单文件 gz/bz2/xz、原始文本、GBK 回退、超长行截断。
- Worker 处理器：本地复制、规则集成员 glob 与编码回退解析、逐行匹配、批量结果缓冲、重试清场、成功/部分成功/失败终态。
- 七张核心表的 SQLModel 模型：`data_sources`、`rule_sets`、`jobs`、`file_tasks`、`file_task_attempts`、`matches`、`workers`；PostgreSQL 方言下字节/内存计数为 `BIGINT`、`Match.id` 为 `BIGSERIAL`、JSON 列为 `JSONB`。

### M1a（已交付）

- 管理员 Bearer 认证（`secrets.compare_digest`、拒绝空 token、非 dev/test 拒绝默认 token）、Session 依赖与测试夹具；`Job` 增加 `idempotency_key` 及 `(created_by, idempotency_key)` 唯一约束。**该静态 token 认证已在 M2a 被 JWT 登录替换，不再接受。**
- 数据源创建/列表（分页）与路径检测；规则集版本化创建/列表（分页）、运行时配置校验与完整详情查询。
- 作业创建时的 NAS 目录递归扫描（`max_files` 提前中止、10 秒 deadline、扫描前释放读事务）、路径逃逸校验、幂等重放/请求体一致性校验、空目录处理、每个压缩文件一条 `file_task`；作业列表/详情/文件任务/取消。
- 命中结果 `(created_at,id)` 游标查询（强制 `job_id` 或时间范围、支持 severity）、Worker 列表、系统状态汇总。
- API 层测试全部基于 SQLite，无外部服务依赖。

### M1b（已交付）

- PostgreSQL 队列适配：`FOR UPDATE SKIP LOCKED` 原子领取、`attempts` 租约栅栏的 `heartbeat`/`touch`/`complete`、`file_task_attempts` 历史、失败线性退避、`recover_stale` 僵死回收、Worker 注册与心跳、`matches` 批量写入与按任务清理。
- Worker 常驻循环 `python -m src.worker`：注册、启动与周期（300 秒）僵死回收和作业汇总补偿、drain 检查、领取-处理-续租-终态、复制期 5 秒 `touch`、行处理每 5 秒或每 20000 行心跳、`LeaseLost` 中止、信号优雅退出与 `offline` 收尾。
- 作业终态汇总：`succeeded`/`partial`/`failed`/`cancelled` 与 `finished_at`，由最后完成任务的 Worker 写入、周期扫描兜底，`status='running'` 行级栅栏保证单一写入者。
- SSE 作业进度 `GET /api/jobs/{id}/events`：终态或作业删除即结束，每次轮询独立短会话，不占用请求级连接。
- Worker 管理接口：`drain`、`resume`、`drain-version`；离线 Worker 不被改写成 `draining`/`online`，但会记录 `drain_requested` 以便重启后生效。

### M2a（已交付）

- 认证领域核心：`users`/`roles`/`user_roles` 三张表、bcrypt 口令哈希（超 72 字节先 SHA-256，拒绝空口令）、HS256 `TokenService`（`access`/`refresh` 带 `type` 声明、密钥最短 32 字符、过期与错签名一律拒绝）。
- JWT 登录链路：`POST /api/login`、`POST /api/refresh-token`、`GET /api/auth/routes`、`POST /api/logout`（四个端点返回 `{"code","message","data"}` 信封），认证依赖解码 access token；后续 RBAC 把业务路由从单一 `AdminDep` 升级为细粒度 `require_permission(...)`，`scripts/seed_admin.py` 幂等创建管理员，启动时补齐 RBAC 种子。
- Alembic：`0001_baseline`（七张业务表）+ `0002_auth_tables`（显式 op）+ `0004_rbac_menus`（menus/role_menus 与 roles 扩列）+ `0006_file_task_queue_indexes`（领取/回收部分索引），`src/infrastructure/migrations.py:run_migrations()` 由 lifespan 按 `LOGHARVEST_AUTO_CREATE_TABLES` 执行 `upgrade head`；测试关掉迁移改用 `create_all`，并用逐表逐列断言防止漏写迁移。
- 队列/Worker 遗留语义：领取语句带回 `size_bytes`/`mtime`，复制前源校验，源缺失或变更走 `PermanentTaskFailure` 并以 `retryable=False` 落 `failed`（不自动重试），取消检查按心跳预算执行（每 `heartbeat_every_lines` 行或每 `heartbeat_every_seconds` 秒一次，`is_job_cancelled` → `cancelled`；规格 §7.3 的"每 2 万行"，不是逐行 SELECT），手动重试 `POST /api/jobs/{id}/files/retry-failed`。
- 概览与运维：`GET /api/system/overview`（作业/文件任务分组计数、24 小时命中量、在线 Worker 与版本分布、最老 pending 等待秒数）、`POST /api/system/maintenance`（`run_maintenance`：回收僵死任务、降级静默 Worker、收尾作业；**队列不是 PostgreSQL 时返回 503**，因为回收语句用了 PostgreSQL 专有语法）、`GET /api/jobs/{id}/files?status=` 状态筛选。

### 明确不包含（后续里程碑）

- 前端本身在仓库的 [`../web/`](../web/)（见其 README）；后端提供动态菜单/路由，菜单数据由 `menus` 表维护。
- 多副本并发启动时的 advisory lock 压测：单实例 `alembic upgrade head` 已在真实 PostgreSQL 上执行验证，但尚未专门压测多个 API 副本同时启动的锁竞争。
- Docker/PostgreSQL 部署编排；镜像替换由 Jenkins + Docker Compose 执行，Web 不承载部署。
- 进程内复制并发池与 `LOGHARVEST_MAX_MATCHES_PER_TASK` 强制执行。
- `data_sources.copy_to_local` 与 `workers.last_error` 的行为：两列都随表建出、API 也返回，但当前没有读取或写入方——Worker 无条件把压缩包复制到本地临时盘（开关不生效），`last_error` 没有任何写入方（恒为 `null`）。列是**有意保留**的，避免 M3 再做一次迁移；在接线前不要把它们当成已生效的能力。
- 多行堆栈规则；v1 保持单行优先。
- 数据权限（部门/本人范围）：当前 RBAC 只控制功能权限，所有有权限的用户看到同一批作业与结果；`roles.data_scope` 与部门表未引入。
- Token 黑名单与服务端会话、Redis 缓存、进程内后台定时器：`POST /api/system/maintenance` 只执行一次补偿，周期由外部 cron / compose job 负责。
- 口令策略（长度、复杂度）与**账号级**失败锁定：口令强度只在种子脚本里检查最短 8 位。登录限流已实现（`LOGHARVEST_LOGIN_MAX_ATTEMPTS` / `LOGHARVEST_LOGIN_WINDOW_SECONDS`，按"用户名 + 来源 IP"的失败次数，见配置表），但它只挡住来源维度，不是账号级锁定。

## 目录

```text
service/
├── alembic/
│   ├── env.py                      # 用 settings.database_url 建同步引擎
│   └── versions/                   # 0001_baseline … 0006_file_task_queue_indexes
├── alembic.ini
├── scripts/seed_admin.py           # 幂等创建/补齐管理员账号
├── src/
│   ├── api/
│   │   ├── dependencies.py         # Session / Engine / JWT、admin 角色与登录限流器依赖
│   │   └── v1/
│   │       ├── router.py           # /api 路由聚合与健康检查
│   │       ├── routes_auth.py      # 登录/刷新/登出/菜单占位（响应信封）
│   │       ├── routes_data_sources.py
│   │       ├── routes_events.py    # SSE 作业进度
│   │       ├── routes_jobs.py
│   │       ├── routes_matches.py
│   │       ├── routes_rule_sets.py
│   │       ├── routes_system.py    # status / overview / maintenance
│   │       ├── routes_workers.py   # 列表 / drain / resume / drain-version
│   │       └── schemas.py          # 请求/响应模型
│   ├── application/
│   │   ├── services/               # 数据源、规则集、作业规划、查询、认证与维护
│   │   └── worker/                 # Worker 处理器与端口协议
│   ├── config/settings.py          # LOGHARVEST_* 配置（含 JWT 与管理员）
│   ├── domain/
│   │   ├── archive/                # 压缩包发现与流式行读取
│   │   ├── models/                 # entities.py 七张业务表 + auth.py 三张认证表
│   │   ├── rules/                  # 规则编译与匹配
│   │   └── services/               # 口令哈希、TokenService 与登录限流
│   ├── infrastructure/
│   │   ├── database.py             # 引擎、连接参数与测试建表函数
│   │   ├── match_writer.py         # matches 批量写入与清理
│   │   ├── migrations.py           # run_migrations() 与 BUSINESS_TABLES
│   │   └── task_queue.py           # PostgreSQL 队列与作业终态汇总
│   ├── main.py                     # FastAPI 入口（lifespan 里执行迁移）
│   └── worker/                     # Worker 常驻循环与 __main__ 入口
├── tests/                          # pytest 用例（api / application / domain / infrastructure / scripts / worker）
├── pyproject.toml
├── uv.lock
└── .python-version
```
