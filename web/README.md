# LogHarvest Web

LogHarvest 前端，派生自 [vue-pure-admin](https://github.com/pure-admin/vue-pure-admin)（MIT，
Copyright (c) 2020-present, pure-admin）。本目录保留上游 `LICENSE` 原文；框架层（layout/router/
store/http/components/style）来自该模板，业务页面为 LogHarvest 自有代码。

移植时删除了模板的演示业务（system/monitor/permission/result/guide/about/welcome 等视图、
`src/api/system`、裁剪/二维码/打字机/图片验证码等组件）与重型插件（echarts、vxe-table、
plus-pro-components、intro.js、version-rocket、localforage、cropperjs、qrcode、typeit），
以及 `public/` 下未被引用的音视频与 wasm 演示资源。

## 开发

```bash
cd web
pnpm install
pnpm dev          # http://localhost:8848，/api 代理到 http://127.0.0.1:8000
pnpm typecheck
pnpm build
```

## 与后端联调

前端只依赖 `/api` 一个前缀：开发期由 Vite 代理（`vite.config.ts` 里显式写 `127.0.0.1`，
避免 `localhost` 解析到 `::1` 命中别的 8000 绑定），生产期由 Nginx 反代到同一个前缀。

```bash
# 1) 后端：用 SQLite 起控制面（纯 API 联调不需要 PostgreSQL）
cd ../service
uv sync --python 3.12
export LOGHARVEST_DATABASE_URL="sqlite:///./web-dev.db"
#    建表：dev 下 `LOGHARVEST_AUTO_CREATE_TABLES=true` 会在启动时执行 Alembic upgrade head，
#    也可以手动：
uv run --python 3.12 alembic upgrade head

# 2) 建管理员（密码来自 LOGHARVEST_ADMIN_PASSWORD，至少 8 位；不给会拒绝执行）
LOGHARVEST_ADMIN_PASSWORD='devpass123' uv run --python 3.12 -m scripts.seed_admin

# 3) 起 API
uv run --python 3.12 uvicorn src.main:app --port 8000

# 4) 起前端并登录（用户名默认 admin）
cd ../web && pnpm dev
```

- **为什么 SQLite 够用**：数据源/规则集/作业/文件任务/命中结果/Worker 这些读写都是可移植 SQL，
  浏览器侧的列表、详情、概览、排空操作都能跑通。
- **什么时候必须上 PostgreSQL**：只有任务队列与 Worker 常驻循环——队列为
  `SELECT ... FOR UPDATE SKIP LOCKED`，Worker 不发心跳会被维护任务降级为离线。
  也就是说「在页面上点执行维护 / 看 Worker 领取任务」这类联调要 PostgreSQL + 真 Worker。
- 联调用的临时库记得清掉：`rm -f service/web-dev.db*`（已在根 `.gitignore` 中忽略）。

### SSE 为什么用 `fetch` 而不是 `EventSource`

作业实时进度走 `GET /api/jobs/{id}/events`（`event: progress`）。`EventSource` 不支持自定义请求头，
带不上 `Authorization: Bearer <accessToken>`（也不能改 `Accept`），所以前端用
`fetch` + `response.body.getReader()` 读流，自己按 `\n\n` 分帧（`src/utils/sse.ts` 的
`parseSseChunk`，跨 chunk 的残帧留在缓冲区里等下一段）。代价是浏览器不会自动重连：
组件里做了退化路径——流断开或响应不可用时改成每 5 秒轮询作业详情，终态后停止。

## 已实现界面（M2b）

| 路由 | 界面 | 数据来源 |
|---|---|---|
| `/home`、`/logharvest/overview` | 运行概览（首页） | `GET /api/system/overview`，失败退回 `GET /api/system/status`；`POST /api/system/maintenance` |
| `/logharvest/jobs`、`/logharvest/jobs/:id` | 作业列表 / 新建 / 详情 + 实时进度 | `GET/POST /api/jobs`、`GET /api/jobs/{id}[/files]`、`POST /api/jobs/{id}/cancel`、SSE |
| `/logharvest/rulesets` | 规则集列表与结构化新建 | `GET/POST /api/rule-sets`、`GET /api/rule-sets/{id}` |
| `/logharvest/datasources` | 数据源列表与新建、路径测试 | `GET/POST /api/data-sources`、`POST /api/data-sources/{id}/test` |
| `/logharvest/matches` | 命中结果游标浏览 | `GET /api/matches` |
| `/logharvest/workers` | Worker 管理与按版本排空 | `GET /api/workers`、`POST /api/workers/{id}/drain\|resume`、`POST /api/workers/drain-version?version=` |

接口约定：业务接口是纯 REST 对象（错误体 `{"detail": "..."}`），列表接口返回**裸数组、没有总数**，
所以所有分页都是窗口式「上一页 / 下一页」（本页不足 `limit` 即视为末段）或游标式
「下一页 / 回到第一屏」，没有页码跳转；只有认证接口
（`POST /api/login`、`POST /api/refresh-token`、`POST /api/logout`、`GET /api/auth/routes`）
使用 `{code, message, data}` 信封。菜单在前端静态定义，服务端没有动态菜单。

**已对本机 8000 端口的 dev 后端实测核对**（只发 GET 请求）：`GET /api/matches` 缺作用域、
只给 `created_from` 一端、只给 `created_before` 都返回 `422` 且 `detail` 原文是
`either job_id or both created_from and created_to are required`（界面提示与此一致）；
`job_id + created_before + id_before` 成对可用（200）；`severity` 是单值等值过滤；
`limit` 上限 500（`/api/workers` 是 200）；`/api/matches`、`/api/workers` 返回裸数组；
`MatchRead` / `WorkerRead` / `SystemStatusRead` 的字段名与 `src/api/logharvestTypes.ts` 逐项相符。

**待合流复核**（契约按后端约定先行实现，当前运行的后端实例还没有这些端点）：
`POST /api/login` 等认证接口、`GET /api/system/overview`、`POST /api/system/maintenance`、
`GET /api/jobs/{id}/files?status=`（状态筛选下推到服务端）、`POST /api/jobs/{id}/files/retry-failed`、
`python -m scripts.seed_admin` 的确切命令与密码来源。

## 验证口径

质量门槛是 `pnpm typecheck` 与 `pnpm build`。本项目的开发环境里内置浏览器是一个隐藏页
（截图失败、`innerWidth` 为 0、`requestAnimationFrame` 冻结），**没有做过任何人工视觉确认**，
因此上述命令只能证明类型与产物可构建，不能证明每个界面渲染正确。

后端接口清单见 `service/README.md`。

## 说明

- 菜单与路由在前端静态定义（`src/router/modules/*.ts`），没有服务端动态菜单。
- `public/platform-config.json` 与 `types/*.d.ts` 是框架启动与 `@/` 别名类型的必需品，删除会导致
  `main.ts` 启动报错或 typecheck 失败。
- 本 fork 未复制上游的 eslint/stylelint/husky/playwright 配置与依赖，因此没有 `pnpm lint`；
  质量门槛为 `pnpm typecheck` 与 `pnpm build`。
