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
#    签名密钥任何环境都必填（含 dev），留空会在启动时直接报错：
export LOGHARVEST_JWT_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
#    建表：dev 下 `LOGHARVEST_AUTO_CREATE_TABLES=true` 会在启动时执行 Alembic upgrade head，
#    也可以手动：
uv run --python 3.12 alembic upgrade head
```

```bash
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

## 已实现界面（M2b + M2c）

| 路由 | 界面 | 数据来源 |
|---|---|---|
| `/home`、`/logharvest/overview` | 运行概览（首页） | `GET /api/system/overview`，失败退回 `GET /api/system/status`；`POST /api/system/maintenance` |
| `/logharvest/jobs`、`/logharvest/jobs/:id` | 作业列表 / 新建 / 详情 + 实时进度 + **尝试历史抽屉** | `GET/POST /api/jobs`、`GET /api/jobs/{id}[/files]`、`POST /api/jobs/{id}/cancel`、`GET /api/jobs/{id}/files/{task_id}/attempts`、SSE |
| `/logharvest/rulesets` | 规则集列表与结构化新建 | `GET/POST /api/rule-sets`、`GET /api/rule-sets/{id}` |
| `/logharvest/datasources` | 数据源列表与新建、路径测试 | `GET/POST /api/data-sources`、`POST /api/data-sources/{id}/test` |
| `/logharvest/matches` | 命中结果游标浏览 | `GET /api/matches` |
| `/logharvest/workers` | Worker 管理（按版本排空）+ **CPU / 内存 / 临时盘空闲 / 最近错误四列** | `GET /api/workers`、`POST /api/workers/{id}/drain\|resume`、`POST /api/workers/drain-version?version=` |

### 尝试历史抽屉（M2c Task 5）

文件任务表的「历史」列打开右侧抽屉，展示 `attempt_no` / `worker_id` / `worker_version` /
`status` / `started_at` / `finished_at` / `processed_lines` / `matched_rows` / `error`。
抽屉区分三种状态，绝不合并：

- `200 []` → 「该任务尚无尝试记录」（一次都没跑过，不是错误）；
- 请求失败 → 抽屉内红色 `el-alert` + 全局提示，原文带上后端 `detail` 与 HTTP 状态码
  （`404` 会额外说明「后端还没有该端点，或该任务不属于本作业」），**不会显示成空表**；
- 返回的不是裸数组 → 直接说「后端未按 `list[FileTaskAttemptRead]` 契约实现」。

### Worker 资源列（M2c Task 5，依赖 Worker 上报）

`cpu_percent` / `memory_bytes` / `temp_disk_free_bytes` / `last_error` 这四列在表里一直存在，
但**直到 M2c Task 4 才有写入方**（此前 `WORKER_HEARTBEAT_SQL` 只更新
`status` / `current_task_id` / `last_heartbeat`）。因此升级前留下的行、以及升级后还没重启的
Worker 都会显示 `—`。界面把 `NULL` 一律渲染成 `—`（未知），**绝不渲染成 `0`**——
`0` 会被读成「CPU 空闲 / 临时盘已满」，这正是本项目反复踩过的假安心。表尾的统计行会说明本页
「至少上报过一项资源」的台数。

### `@pureadmin/table` 的自定义列必须写 `slot`

`@pureadmin/table@3.3.0` 只按列定义里的 `slot` 字段取内容插槽（`dist/index.es.js`：
`s ? t[s]?.() : void 0`，`s` 即 `column.slot`）。**没有 `slot` 时不会回退到与 `prop` 同名的插槽**，
插槽模板被整体忽略、单元格退回裸 `row[prop]` 文本；`typecheck`/`build` 都发现不了。
新增或改列时，凡是有 `<template #xxx="{ row }">` 的列都要同时写 `slot: "xxx"`
（`jobs/detail.vue`、`workers/index.vue` 已按此补齐）。

### `patches/@pureadmin__table@3.3.0.patch`

给组件库的自适应高度补了空引用保护：窗口缩放与路由切换同时发生时，表格已经卸载而
`resize` 回调仍在读 `$refs`，控制台报 `TypeError: Cannot read properties of null
(reading '$refs')`。上游 3.3.0 尚未修复，补丁只加判空、不改变正常路径的行为，由
`pnpm-workspace.yaml` 的 `patchedDependencies` 在 `pnpm install` 时自动应用。
升级 `@pureadmin/table` 后需要重新生成补丁，否则安装时会提示补丁未匹配。

### 列表页统一写法（对齐上游 vue-pure-admin）

系统管理各列表页与上游共用一套结构，新增列表页照此写：

- 表格上方是 `form.search-form.bg-bg_color` 搜索卡片，输入框 `class="w-45!"`，
  搜索/重置按钮带 `useRenderIcon` 图标；
- 表头样式取 `src/views/system/table.ts` 的 `TABLE_HEADER_CELL_STYLE`，
  表格加 `align-whole="center"`、`table-layout="auto"`、`adaptive`；
- `pure-table` 必须绑定插槽给的 `dynamicColumns`（`v-slot="{ size, dynamicColumns }"`）。
  直接用 `columns` 会让「列设置」的显隐、拖拽排序、左右固定全部空转；
- 行内操作按钮加 `class="reset-margin"`；按钮超过 3 个时把低频动作收进「更多」下拉，
  否则窄屏会把操作列裁切到看不见；
- 角色页的菜单授权是右侧面板（表格收窄到 62%，面板内是 `el-tree-v2` + 搜索 +
  展开/全选/父子联动），不是弹窗。默认不联动、勾选父级不会自动勾选子级——
  后端按精确授权存 `menu_ids`，内置角色只读。

接口约定：业务接口是纯 REST 对象（错误体 `{"detail": "..."}`），列表接口返回**裸数组、没有总数**，
所以所有分页都是窗口式「上一页 / 下一页」（本页不足 `limit` 即视为末段）或游标式
「下一页 / 回到第一屏」，没有页码跳转；只有认证接口
（`POST /api/login`、`POST /api/refresh-token`、`POST /api/logout`、`GET /api/auth/routes`）
使用 `{code, message, data}` 信封。业务菜单与路由由后端 `GET /api/auth/routes` 按角色下发，
前端不再维护静态业务菜单；`src/router/utils.ts#addAsyncRoutes` 把目录路由映射为
`EmptyLayout`（只含 `<router-view />`，避免嵌套侧边栏），页面组件按
`src/views/**/*.vue` 的路径字符串解析。

**已对本机 8000 端口的 dev 后端实测核对**（只发 GET 请求）：`GET /api/matches` 缺作用域、
只给 `created_from` 一端、只给 `created_before` 都返回 `422` 且 `detail` 原文是
`either job_id or both created_from and created_to are required`（界面提示与此一致）；
`job_id + created_before + id_before` 成对可用（200）；`severity` 是单值等值过滤；
`limit` 上限 500（`/api/workers` 是 200）；`/api/matches`、`/api/workers` 返回裸数组；
`MatchRead` / `WorkerRead` / `SystemStatusRead` 的字段名与 `src/api/logharvestTypes.ts` 逐项相符。

**历史核对记录**：M2c 期间对 `POST /api/login`、`GET /api/system/overview`、
`POST /api/system/maintenance`、`GET /api/jobs/{id}/files?status=`、尝试历史与
`python -m scripts.seed_admin` 的契约核对均已完成；RBAC 改造后这些接口与新增的
用户/角色/菜单接口已在真实后端实例上联调。

## 验证口径

质量门槛是 `pnpm typecheck` 与 `pnpm build`。RBAC 改造除类型与构建外，还在真实后端
（`wslc` PostgreSQL 容器）+ 全局 Playwright + Chrome 的**真实视口**（1440/1280/390）下
截图并量 DOM，跑完用户/角色/菜单三屏 18 步 CRUD 与"菜单上级选项/用户名规则"探针，
0 console error、0 意外 4xx/5xx；admin 与 viewer 的动态菜单、页面加载与按钮显隐均已验证。
本项目的开发环境里内置浏览器仍是一个隐藏页（截图失败、`innerWidth` 为 0、
`requestAnimationFrame` 冻结），只可用于结构与交互检查，**不能**用于布局与截图结论；
视觉结论一律以 Playwright + Chrome 为准。

## RBAC 管理页与按钮权限

- `src/views/system/users`：用户列表、新建/编辑、分配角色、重置密码、删除、启停；
- `src/views/system/roles`：角色 CRUD、菜单/权限树精确授权（`check-strictly`）、系统角色 admin 只读；
- `src/views/system/menus`：目录/页面/权限三级菜单树管理，内置菜单仅允许修改展示字段；
- `src/api/rbac.ts` / `rbacTypes.ts`：上述页面的纯 REST API 与类型；
- 登录返回的 `permissions`（admin 为 `*:*:*`）驱动 `v-perms`；动态路由的 `meta.auths`
  同时可用于 `v-auth`。既有业务页面的写/操作按钮已按 `jobs:write`、`jobs:operate`、
  `datasources:write`、`rulesets:write`、`workers:operate`、`system:operate` 隐藏。
- 权限或角色变更后需重新登录：前端 `permissions` 是登录时的快照，后端接口权限实时生效。

设计依据、边界保护矩阵、验收记录与已知限制（含"最后一名 admin"的并发定性）见
[`docs/superpowers/specs/2026-10-27-logharvest-rbac-menus-permissions-design.md`](../docs/superpowers/specs/2026-10-27-logharvest-rbac-menus-permissions-design.md)。

质量门槛是 `pnpm typecheck` 与 `pnpm build`。本项目的开发环境里内置浏览器是一个隐藏页
（截图失败、`innerWidth` 为 0、`requestAnimationFrame` 冻结），**未做人工视觉确认**，
因此上述命令只能证明类型与产物可构建，不能证明每个界面渲染正确。
`@pureadmin/table` 的 `slot` 约定（见上）是源码核对 + Node 侧插槽归一化行为验证的结论，
也不是界面截图结论。

后端接口清单见 `service/README.md`。

## 说明

- 业务菜单与路由由后端 `GET /api/auth/routes` 动态下发；`src/router/modules` 只保留首页与
  登录/错误页等静态路由。
- `public/platform-config.json` 与 `types/*.d.ts` 是框架启动与 `@/` 别名类型的必需品，删除会导致
  `main.ts` 启动报错或 typecheck 失败。
- 本 fork 未复制上游的 eslint/stylelint/husky/playwright 配置与依赖，因此没有 `pnpm lint`；
  质量门槛为 `pnpm typecheck` 与 `pnpm build`。
