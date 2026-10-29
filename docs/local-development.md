# LogHarvest 本地开发指导

> 目标：clone 之后 30 分钟内跑起"登录 + 作业全流程（含真实 Worker）"，并让前后端测试全绿。

## 1. 环境要求

| 工具 | 版本 | 用途 |
|---|---|---|
| Python | 3.12（`service/.python-version` 已固定） | 后端 |
| [uv](https://docs.astral.sh/uv/) | 最新 | 后端依赖与运行（锁文件 `service/uv.lock` 已提交） |
| Node.js | `^20.19.0 || >=22.13.0` | 前端 |
| pnpm | ≥ 9（`corepack enable` 即可） | 前端依赖与脚本 |
| PostgreSQL | 16 | **仅** Worker/队列与真实集成测试需要；纯 API 联调用 SQLite |
| Docker + Compose v2 | 可选 | 一键起 PostgreSQL 或整体部署 |

> Windows 开发提示：NAS 只读守卫在非 POSIX 平台判定恒为 `unknown`（读不到挂载表），`enforce` 不会拦截——这不影响功能开发，但**不要**把 Windows 当作"只读保护已生效"的验证环境。

## 2. 首次启动（后端 + 前端，SQLite 快速路径）

```bash
# 1) 后端依赖
cd service
uv sync --python 3.12

# 2) 配置：复制样例后至少填写 JWT 密钥（任何环境必填，留空拒绝启动）
cp ../.env.example .env
#    生成密钥：
python -c "import secrets; print(secrets.token_urlsafe(48))"   # 填入 LOGHARVEST_JWT_SECRET_KEY

# 3) 建表 + 管理员（迁移在启动时自动执行，这两步可省，但显式跑一次心里有数）
uv run --python 3.12 alembic upgrade head
LOGHARVEST_ADMIN_PASSWORD='devpass123' uv run --python 3.12 -m scripts.seed_admin

# 4) 起 API（http://127.0.0.1:8000）
uv run --python 3.12 uvicorn src.main:app --reload --port 8000
curl http://127.0.0.1:8000/api/health    # {"status":"ok","service":"logharvest"}
```

```bash
# 5) 前端（另开终端）
cd web
corepack enable
pnpm install
pnpm dev          # http://localhost:8848，/api 代理到 127.0.0.1:8000
```

浏览器打开 `http://localhost:8848`，用 `admin / devpass123` 登录。种子已内置 3 个目录、13 个页面、19 个权限节点与 admin/operator/viewer 三个角色。

## 3. 完整链路（PostgreSQL + Worker）

页面上点"执行维护"、看 Worker 领取任务，**必须** PostgreSQL + 真 Worker（队列是 `SKIP LOCKED` 专有 SQL）：

```bash
# 1) 起 PostgreSQL（任选其一）
docker run -d --name logharvest-pg -p 5432:5432 \
  -e POSTGRES_USER=logharvest -e POSTGRES_PASSWORD=logharvest \
  -e POSTGRES_DB=logharvest postgres:16-alpine

# 2) 后端指向 PG 并迁移 + 种子
cd service
export LOGHARVEST_DATABASE_URL="postgresql://logharvest:logharvest@127.0.0.1:5432/logharvest"
uv run --python 3.12 alembic upgrade head
LOGHARVEST_ADMIN_PASSWORD='devpass123' uv run --python 3.12 -m scripts.seed_admin
uv run --python 3.12 uvicorn src.main:app --reload --port 8000

# 3) 起 Worker（另开终端；同机多进程必须各自唯一 WORKER_ID）
cd service
LOGHARVEST_DATABASE_URL="postgresql://logharvest:logharvest@127.0.0.1:5432/logharvest" \
LOGHARVEST_WORKER_ID=worker-1 \
  uv run --python 3.12 python -m src.worker

# 4) 造一批测试压缩日志（仓库自带模拟生成器）
python data/generate_mock_logs.py --help
```

Linux 上验证只读守卫的真实行为：把测试目录以 `ro` 方式 bind-mount 后创建数据源，`POST /api/data-sources/{id}/test` 应返回 `writability: "read_only"`。

## 4. 测试与质量门禁

`agent.md` 是本仓库的强制协作规范：**任何代码变更，提交前必须前后端测试全绿**。

```bash
# 后端（686 条用例；无 PostgreSQL 时 666 通过、20 条自动跳过——19 条集成用例 + 1 条平台相关）
cd service
uv run --python 3.12 pytest -q                       # 全量
uv run --python 3.12 pytest tests/api -q             # 单目录
uv run --python 3.12 pytest -q --cov=src --cov-report=term   # 覆盖率（pytest-cov 已入 venv）
uv run --python 3.12 ruff check src tests && uv run --python 3.12 ruff format --check src tests
uv run --python 3.12 mypy

# 真实 PostgreSQL 集成测试（可选，注意端口 5433 示例；CI 同款一键脚本见 bash scripts/run_pg_integration.sh，需 Docker）
LOGHARVEST_TEST_PG_URL="postgresql+psycopg://logharvest:logharvest@127.0.0.1:5433/logharvest" \
  uv run --python 3.12 pytest tests/integration -q

# 前端（102 条用例 + 类型检查）
cd web
pnpm test         # vitest run
pnpm test:watch   # watch 模式
pnpm typecheck    # vue-tsc
pnpm build        # 生产构建验证
```

测试基建事实（改测试前先读）：

- 后端 `tests/conftest.py` 用 autouse fixture 关闭启动迁移并注入测试密钥；每个用例在内存 SQLite（`StaticPool`）建表，**永不碰开发库**；登录限流单例每用例重置。
- 迁移与模型漂移由 `tests/infrastructure/test_migrations.py` 逐表逐列比对——加表必须写迁移。
- 前端纯逻辑放在 `.ts` 模块（`format.ts` / `rule-form.ts` / `pendingRequests.ts` / `sse.ts`），vitest 配置在 `web/vitest.config.ts`（独立于 vite.config，不加载完整插件链）。

## 5. 前后端契约速查

- 前端只依赖 `/api` 一个前缀：开发期 Vite 代理（`vite.config.ts` 显式 `127.0.0.1`），生产期 Nginx 反代。
- 列表接口是**裸数组**、无 `total`（窗口式翻页，`limit` 默认 50 上限 500）；业务接口错误体是 `{"detail": "..."}`（422 的 detail 是 pydantic 数组）。
- 只有认证四接口（login/refresh/routes/logout）包 `{"code":0,"message":"ok","data":...}` 信封。
- 权限码唯一来源：`service/src/domain/permissions.py`；改权限先加常量再补种子（有测试钉住）。
- 动态路由的 `component` 必须与 `web/src/views` 下相对路径一致（如 `logharvest/jobs/index`），写错是运行期白屏而非构建错误。

## 6. 常用调试入口

| 想看什么 | 去哪 |
|---|---|
| 接口契约与错误码 | `service/README.md` 的 API 表；OpenAPI：`http://127.0.0.1:8000/docs` |
| 队列 SQL（领取/回收/终态） | `service/src/infrastructure/task_queue.py`（文件头注释解释每条语句） |
| Worker 循环语义 | `service/src/worker/main.py`（run_once/run_forever）+ `application/worker/processor.py` |
| 只读判定三态 | `service/src/domain/source_guard.py` + `tests/domain/test_source_guard*.py` |
| 令牌刷新队列 | `web/src/utils/http/pendingRequests.ts`（文件头注释即契约） |
| SSE 分帧 | `web/src/utils/sse.ts` + `service/src/api/v1/routes_events.py` |
| 菜单种子形状 | `service/src/application/services/rbac_seed.py` |

## 7. 常见问题

| 症状 | 原因与处理 |
|---|---|
| 后端启动即失败：`jwt_secret_key is required` | 任何环境都必填且 ≥32 字符，写入 `service/.env` |
| `seed_admin` 拒绝执行 | `LOGHARVEST_ADMIN_PASSWORD` 未设置或少于 8 位 |
| 登录返回 429 | 触发登录限流（10 次失败/300 秒，按用户名+IP）；等待窗口或重启进程 |
| Worker 起不来 / 任务不领取 | `LOGHARVEST_DATABASE_URL` 没指向 PostgreSQL（队列 SQL 是 PG 专有） |
| 页面 403 但角色看起来对 | 权限快照在登录时下发——重新登录；或权限码没在种子中（看测试钉住的报错） |
| 前端某菜单白屏 | `menus.component` 与 `web/src/views` 路径不一致（运行期解析失败） |
| 统计/概览数字对不上 | 窗口按 UTC 日界；`matches_24h` 按入库时间而非日志时间 |
| Windows 上 enforce 不拦截 | 挂载表不可读 → 判定 `unknown`；验证只读保护请用 Linux + ro 挂载 |
| 测试跑完冒出 `service/logharvest.db` | 不会——测试 autouse 强制内存库；若出现是某次手动 `uvicorn` 启动的开发库（已在 .gitignore） |

## 8. 下一步

- 部署到服务器：[Docker 部署说明](deployment-docker.md)
- 接入 CI/CD：[Jenkins 自动化说明](deployment-jenkins.md)
- 理解内部实现：[整体设计架构](architecture.md) / [核心逻辑说明](core-logic.md)
- 协作规范：根目录 [agent.md](../agent.md)（AI 交互测试门禁）
