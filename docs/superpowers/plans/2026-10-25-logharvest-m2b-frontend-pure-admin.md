# LogHarvest M2b Frontend (Pure Admin Port) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `vue-pure-admin` 框架层移植为 LogHarvest 的 `web/` 前端，并实现作业、规则集、数据源、命中结果、Worker 管理五屏与登录，对齐 M2a 后端的 JWT 与纯 REST 接口。

**Architecture:** 从 `E:/GitHub/Hello-FastApi/web`（`vue-pure-admin` 7.0.0，336 源文件）复制框架层、删掉演示业务，只保留 layout/router/store/http/components/style 等骨架；菜单与路由改为前端静态定义（不做服务端动态菜单）；认证走 `{code, message, data}` 信封的三个新接口，业务接口保持纯 REST 对象，因此 LogHarvest 的 API 封装单独放在 `src/api/logharvest.ts`；作业进度用 `fetch` + `ReadableStream` 读取 SSE，因为 `EventSource` 不能携带 `Authorization` 头。

**Tech Stack:** Vue 3 + TypeScript + Vite、Element Plus、`@pureadmin/table`、Pinia、pnpm、vue-tsc、axios。

**Spec:**
- [主设计方案](../specs/2026-10-24-compressed-log-extraction-design.md)（§3 三服务架构、§6 前端界面、§7.4 可观测性）
- [M2a 后端计划](./2026-10-25-logharvest-m2a-backend-auth-ops.md)（认证与接口契约的唯一来源）

## Global Constraints

- 源模板位于 `E:/GitHub/Hello-FastApi/web`，**只读**：不要修改基座项目里的任何文件。
- 目标目录固定为 `E:/GitHub/think/web`；不要碰 `service/`（后端由另一条并行轨道负责）。
- `vue-pure-admin` 是 MIT 授权（`Copyright (c) 2020-present, pure-admin`）。本仓库是公开仓库（`github.com/taoweidong/logharvest`），因此必须：复制 `LICENSE` 原文到 `web/LICENSE`、保留版权与许可声明、把 `package.json` 的 `name`/`author`/`homepage`/`repository`/`bugs` 改为 LogHarvest（不得冒充上游包）、在 `web/README.md` 写明派生自 vue-pure-admin。
- 包管理器强制 pnpm（`preinstall: npx only-allow pnpm`）；命令一律 `cd web && pnpm ...`。
- `package.json` 与 `pnpm-lock.yaml` 必须成对复制，不要手写依赖版本（该 fork 已提前 pin vite ^8 / vue-router ^5 / typescript ^6 / pinia ^4）。
- 后端接口形状以 M2a 计划为准，不得自行猜测字段；不确定的字段先读后端源码确认。
- 不实现后端没有的能力（没有导出就不做导出按钮，没有动态菜单就不做菜单管理页）。
- 本机内置浏览器是隐藏页：截图必失败、几何不可信。视觉验证改为 `pnpm typecheck` + `pnpm build` + 对 dev server 的 HTTP 断言，并明确报告"未能做人工视觉确认"。

---

### Task 1: 移植框架层并可启动

**Files:**
- Create: `web/`（从 `E:/GitHub/Hello-FastApi/web` 复制）
- Delete: 演示 views/components/api（清单见 Step 2）
- Modify: `web/package.json`（改名与署名）
- Modify: `web/src/main.ts`（去掉重型插件）
- Modify: `web/vite.config.ts`（代理到 8000）
- Modify: `web/.env`、`web/.env.development`
- Create: `web/LICENSE`、`web/README.md`

**Interfaces:**
- Consumes: `E:/GitHub/Hello-FastApi/web` 全部框架层
- Produces: `pnpm dev` 可启动、`pnpm typecheck` 与 `pnpm build` 通过的骨架；后续任务在此之上加页面

- [x] **Step 1: 复制框架层**

```bash
mkdir -p /e/GitHub/think/web
cd /e/GitHub/Hello-FastApi/web
cp -r index.html package.json pnpm-lock.yaml pnpm-workspace.yaml tsconfig.json vite.config.ts \
      postcss.config.js types build src public locales .env .env.development LICENSE \
      /e/GitHub/think/web/
ls /e/GitHub/think/web
```
Expected: 目录齐全（`src`、`types`、`build`、`public`、`locales`）。若某文件在基座不存在（例如 `.env` 名称不同），用 `ls -a` 确认后按实际名称复制，不要跳过——`public/platform-config.json` 缺失会让 `main.ts` 抛 `"请在public文件夹下添加platform-config.json配置文件"`，`types/*.d.ts` 缺失会让 `@/` 别名与全局类型失效。

不要复制：`node_modules`、`dist`、`e2e`、`playwright.config.ts`、`playwright-report`、`test-results`、`docker`、`scripts`、`CHANGELOG*`、`README*.md`、`upstream-pkg.json`、`probe-shot.png`、`vite_out.txt`、`vite_err.txt`、`.eslintcache`、`package-lock.json`、`.husky`、`commitlint.config.js`。

- [x] **Step 2: 删除演示内容**

```bash
cd /e/GitHub/think/web
rm -rf src/views/system src/views/monitor src/views/permission src/views/result \
       src/views/guide src/views/about src/views/empty src/views/account-settings src/views/welcome
rm -f src/views/login/components/LoginPhone.vue src/views/login/components/LoginQrCode.vue \
      src/views/login/components/LoginRegist.vue src/views/login/components/LoginUpdate.vue \
      src/views/login/utils/verifyCode.ts
rm -f src/router/modules/about.ts src/router/modules/guide.ts
rm -rf src/api/system src/api/types.ts
rm -rf src/components/ReCropper src/components/ReCropperPreview src/components/ReCountTo \
       src/components/ReQrcode src/components/ReTypeit src/components/ReAnimateSelector \
       src/components/ReImageVerify src/components/ReCol
rm -rf src/utils/localforage src/utils/print.ts
rm -f src/composables/useCrudTable.ts src/composables/useDialogForm.ts src/composables/useSwitchStatus.ts
rm -f src/assets/car.png src/assets/user.jpg
```
保留：`src/layout`(43)、`src/store`(9)、`src/router/{index,utils,enums}.ts` + `modules/{home,remaining}.ts`、`src/directives`(8)、`src/plugins`(4)、`src/style`(10)、`src/config`(2)、`src/utils/{auth,http,message,mitt,progress,responsive,sso,tree,propTypes,preventDefault}.ts`、`src/components/{ReIcon,ReDialog,ReDrawer,ReAuth,RePerms,ReText,ReSegmented,RePureTableBar,ReFlicker}`、`src/assets/{iconfont,svg,status,table-bar,login}`、`src/views/{login,error}`。

注意：`src/router/utils.ts` 用 `import.meta.glob("/src/views/**/*.{vue,tsx}")` 匹配服务端路由，因此 `src/views` 至少要留一个页面——`login` 与 `error` 已满足。

- [x] **Step 3: 去掉重型插件**

Modify `web/src/main.ts`：删除 `useEcharts`、`useVxeTable` 的 import 与对应 `.use()` 调用，删除 `version-rocket` 相关注册；保留 `MotionPlugin`、`useElementPlus`、`Table`(@pureadmin/table)、`PureDescriptions`、i18n、directives、`injectResponsiveStorage`、iconfont 与三个样式入口。然后同步删除 `web/src/plugins/echarts.ts`、`web/src/plugins/vxeTable.ts`，并从 `web/package.json` 的 `dependencies` 移除 `echarts`、`vxe-table`、`@vueuse/motion` 之外的 `plus-pro-components`、`cropperjs`、`typeit`、`qrcode`、`intro.js`、`version-rocket`（保留 `@vueuse/motion`，`views/login/utils/motion.ts` 需要它）。

Run: `cd /e/GitHub/think/web && grep -rn "echarts\|vxeTable\|version-rocket\|plus-pro-components" src build 2>/dev/null | head -20`
Expected: 只剩注释或空结果；若仍有引用（例如 `layout` 里引 echarts 的组件），删除该组件目录并同步删引用，不要留悬空 import。

- [x] **Step 4: 改名与派生声明**

Modify `web/package.json`：

```json
{
  "name": "logharvest-web",
  "version": "0.1.0",
  "description": "LogHarvest 前端（派生自 vue-pure-admin, MIT）",
  "author": "Taowd <taowd@outlook.com>",
  "license": "MIT"
}
```
并删除 `homepage`、`repository`、`bugs` 三处指向上游的字段。保留 `preinstall`、`engines`、`scripts` 原样。

Create `web/README.md`:

```markdown
# LogHarvest Web

LogHarvest 前端，派生自 [vue-pure-admin](https://github.com/pure-admin/vue-pure-admin)（MIT，
Copyright (c) 2020-present, pure-admin）。本目录保留上游 `LICENSE` 原文；框架层（layout/router/
store/http/components/style）来自该模板，业务页面为 LogHarvest 自有代码。

## 开发

cd web
pnpm install
pnpm dev          # http://localhost:8848，/api 代理到 http://127.0.0.1:8000
pnpm typecheck
pnpm build

后端接口见 `service/README.md`。登录需要先用 `python -m scripts.seed_admin` 建管理员。
```

- [x] **Step 5: 环境与代理**

Modify `web/vite.config.ts` 代理：`/api` → `http://127.0.0.1:8000`（`changeOrigin: true`），删除 `/media`（LogHarvest 不提供媒体目录）。
Modify `web/.env`：`VITE_PORT=8848`；`web/.env.development` 保留 `VITE_PUBLIC_PATH=/`、`VITE_ROUTER_HISTORY="hash"`、`VITE_SKIP_CAPTCHA=true`。

Run: `cd /e/GitHub/think/web && pnpm install 2>&1 | tail -15`
Expected: 安装成功（基座的 `node_modules` 链接是坏的，必须全新安装；registry 可达）。

- [x] **Step 6: 验证可启动**

```bash
cd /e/GitHub/think/web && pnpm typecheck 2>&1 | tail -20
(cd /e/GitHub/think/web && pnpm dev >/tmp/vite.log 2>&1 &) ; sleep 12 ; curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8848/ ; curl -s http://127.0.0.1:8848/platform-config.json | head -c 200 ; pkill -f vite
```
Expected: typecheck 无错误；dev server 返回 `200`；`platform-config.json` 可取到 JSON。任何 typecheck 报错都必须在此任务内修掉（多半是 Step 3 的悬空 import），不要留给后续任务。

- [x] **Step 7: 提交**

```bash
git add web
git commit -m "feat: 移植 vue-pure-admin 框架层为 LogHarvest 前端骨架"
```

---

### Task 2: 认证接线（登录页、token 存储、静态菜单）

**Files:**
- Modify: `web/src/utils/http/index.ts:21`（`baseURL`）
- Modify: `web/src/api/user.ts`
- Modify: `web/src/store/modules/user.ts`
- Modify: `web/src/store/modules/permission.ts`
- Modify: `web/src/router/index.ts`
- Modify: `web/src/views/login/index.vue` 与 `utils/static.ts`
- Create: `web/src/api/types.ts`

**Interfaces:**
- Consumes（M2a Task 2 的确切契约）:
  - `POST /api/login` body `{username, password}` → `200 {"code":0,"message":"ok","data":{"avatar":null,"username","nickname","roles":["admin"],"permissions":[],"accessToken","refreshToken","expires":<epoch ms int>}}`；失败 `401 {"detail": "..."}`
  - `POST /api/refresh-token` body `{refreshToken}` → `200 {"code":0,...,"data":{"accessToken","refreshToken","expires"}}`
  - `GET /api/auth/routes` → `200 {"code":0,"message":"ok","data":[]}`
  - `POST /api/logout` → `200 {"code":0,...,"data":null}`
- Produces: `login(username, password): Promise<UserInfo>`、`PureHttp` 在业务接口上返回原始 JSON（不套信封）

- [x] **Step 1: 改 baseURL 与认证接口路径**

Modify `web/src/utils/http/index.ts`：`baseURL: "/api/system"` → `baseURL: "/api"`。保留 `whiteList = ["/refresh-token", "/login"]`、`formatToken` 注入 `Authorization: Bearer <accessToken>`、以及 401 → `logOut()` + 跳 `/login` 的处理。

Create `web/src/api/types.ts`:

```ts
export type Envelope<T> = { code: number; message: string; data: T };

export type LoginData = {
  avatar: string | null;
  username: string;
  nickname: string;
  roles: string[];
  permissions: string[];
  accessToken: string;
  refreshToken: string;
  /** epoch 毫秒，写进 authorized-token cookie 的 expires */
  expires: number;
};

export type RefreshData = Pick<LoginData, "accessToken" | "refreshToken" | "expires">;
```

Rewrite `web/src/api/user.ts`:

```ts
import { http } from "@/utils/http";
import type { Envelope, LoginData, RefreshData } from "./types";

export type LoginPayload = { username: string; password: string };

/** 后端只在认证接口返回 {code,message,data} 信封，业务接口是纯 REST 对象。 */
export const login = (data: LoginPayload) =>
  http.request<Envelope<LoginData>>("post", "/login", { data });

export const refreshToken = (data: { refreshToken: string }) =>
  http.request<Envelope<RefreshData>>("post", "/refresh-token", { data });

export const getAsyncRoutes = () => http.request<Envelope<unknown[]>>("get", "/auth/routes");

export const logout = () => http.request<Envelope<null>>("post", "/logout");
```

- [x] **Step 2: store 适配新响应**

Modify `web/src/store/modules/user.ts` 的 `loginIn`：保留 `data.code === 0` 作为成功闸门，其余字段按 Step 1 的 `LoginData` 读写（`accessToken`/`refreshToken`/`expires`/`roles`/`permissions`）；删除对 `data.data.avatar` 之外不存在字段的引用；`setToken(data.data)` 仍写 `authorized-token` cookie（`expires` 是 epoch ms，与 `utils/auth.ts` 现有实现一致）。
登录失败时后端返回 HTTP 401 + `{"detail": "..."}`：把 `detail` 作为提示文案（`createMessage`），不要显示 `message: undefined`。

Modify `web/src/store/modules/permission.ts` + `web/src/router/index.ts`：不再请求 `getAsyncRoutes()` 合并路由，`handleAsyncRoutes` 路径改为直接返回 `filterAsyncRoutes(routes)` 的静态结果（保留函数签名以免 `layout` 调用点崩）；`getAsyncRoutes` 只在 `permission` store 里保留导出但不再被 `initRouter` 调用。这样菜单完全来自 `src/router/modules/*.ts`。

- [x] **Step 3: 登录页只留账号密码**

Modify `web/src/views/login/index.vue`：只保留用户名/密码表单与登录按钮，删除验证码、手机/扫码/注册 tab 与 `utils/verifyCode.ts` 的引用；`web/src/views/login/utils/static.ts` 里删掉不再使用的 tab 配置项。提交表单调用 Step 1 的 `login()`，成功后 `router.push(decodeURIComponent(...)) || "/logharvest/jobs"`。

Run: `cd /e/GitHub/think/web && pnpm typecheck 2>&1 | tail -20`
Expected: 无错误

- [x] **Step 4: 端到端验证（真后端）**

```bash
cd /e/GitHub/think/service && LOGHARVEST_DATABASE_URL="sqlite:///./web-dev.db" uv run --python 3.12 -m scripts.seed_admin
(cd /e/GitHub/think/service && LOGHARVEST_DATABASE_URL="sqlite:///./web-dev.db" uv run --python 3.12 uvicorn src.main:app --port 8000 >/tmp/uvicorn.log 2>&1 &)
(cd /e/GitHub/think/web && pnpm dev >/tmp/vite.log 2>&1 &)
sleep 12
printf '%s' '{"username":"admin","password":"<你刚种下的密码>"}' | curl -s -X POST -H 'Content-Type: application/json' --data-binary @- http://127.0.0.1:8848/api/login | head -c 240
```
Expected: 通过 vite 代理拿到 `{"code":0,...,"data":{"accessToken":...}}`。之后停掉两个进程并 `rm -f service/web-dev.db`。若登录 401，先直接打 `http://127.0.0.1:8000/api/login` 区分是后端问题还是代理问题。

注：本环境的内置浏览器是隐藏页，截图与几何不可信，因此**不做视觉确认**，并在最终报告里写明这一点。

- [x] **Step 5: 提交**

```bash
git add web
git commit -m "feat: 接入 LogHarvest JWT 登录与静态菜单"
```

---

### Task 3: LogHarvest API 客户端与 SSE 读取

**Files:**
- Create: `web/src/api/logharvest.ts`
- Create: `web/src/api/logharvestTypes.ts`
- Test: `web/src/api/__tests__/sseParser.spec.ts`（若 Step 4 选择不引入测试框架，则改为 `web/src/utils/sse.ts` 的纯函数 + typecheck 覆盖）

**Interfaces:**
- Consumes: 后端只读接口（纯 REST，无信封）：`GET /api/jobs`、`POST /api/jobs`、`GET /api/jobs/{id}`、`GET /api/jobs/{id}/files?limit&offset&status`、`POST /api/jobs/{id}/cancel`、`POST /api/jobs/{id}/files/retry-failed`、`GET /api/jobs/{id}/events`（SSE）、`GET /api/matches`、`GET /api/rule-sets`、`GET /api/rule-sets/{id}`、`POST /api/rule-sets`、`GET /api/data-sources`、`POST /api/data-sources`、`POST /api/data-sources/{id}/test`、`GET /api/workers`、`POST /api/workers/{id}/drain|resume`、`POST /api/workers/drain-version?version=`、`GET /api/system/overview`、`POST /api/system/maintenance`
- Produces: `streamJobProgress(jobId, onEvent, signal): Promise<void>`、每个资源一组 typed 函数

- [x] **Step 1: 类型定义**

Create `web/src/api/logharvestTypes.ts`——字段必须与 `service/src/api/v1/schemas.py` 逐项核对（读后端源码，不要臆测）：

```ts
export type Page<T> = { items: T[]; total: number; limit: number; offset: number };

export type JobStatus = "pending" | "running" | "succeeded" | "partial" | "failed" | "cancelled";
export type TaskStatus = JobStatus;

export type Job = {
  id: number;
  name: string;
  data_source_id: number;
  source_path: string;
  rule_set_id: number;
  status: JobStatus;
  total_files: number;
  total_bytes: number;
  cancel_requested: boolean;
  created_by: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
};

export type FileTask = {
  id: number;
  job_id: number;
  source_path: string;
  file_name: string;
  size_bytes: number;
  status: TaskStatus;
  attempts: number;
  worker_id: string | null;
  processed_lines: number;
  matched_rows: number;
  heartbeat_at: string | null;
  error: string | null;
};

export type Match = {
  id: number;
  job_id: number;
  file_task_id: number;
  rule_id: string;
  severity: string;
  source_path: string;
  member_path: string;
  file_name: string;
  line_no: number;
  raw_line: string;
  raw_truncated: boolean;
  fields: Record<string, unknown>;
  numeric_value: string | null;
  created_at: string;
};

export type WorkerInfo = {
  id: string;
  hostname: string;
  version: string;
  status: "online" | "busy" | "draining" | "offline";
  drain_requested: boolean;
  current_task_id: number | null;
  max_concurrent_copies: number;
  last_heartbeat: string;
};

export type Overview = {
  jobs: Partial<Record<JobStatus, number>>;
  file_tasks: Partial<Record<TaskStatus, number>>;
  matches_24h: number;
  workers: { online: number; by_version: Record<string, number> };
  oldest_pending_seconds: number | null;
};

export type ProgressEvent = {
  job_id: number;
  status: JobStatus;
  total: number;
  by_status: Record<string, number>;
  processed_lines: number;
  matched_rows: number;
};
```

- [x] **Step 2: REST 客户端**

Create `web/src/api/logharvest.ts`（业务接口无信封，`http.request<T>` 直接拿对象；分页参数名固定 `limit`/`offset`）:

```ts
import { http } from "@/utils/http";
import type { FileTask, Job, Match, Overview, Page, WorkerInfo } from "./logharvestTypes";

export const listJobs = (params: { limit?: number; offset?: number }) =>
  http.request<Page<Job>>("get", "/jobs", { params });

export const getJob = (id: number) => http.request<Job>("get", `/jobs/${id}`);

export const listJobFiles = (
  id: number,
  params: { limit?: number; offset?: number; status?: string }
) => http.request<Page<FileTask>>("get", `/jobs/${id}/files`, { params });

export const cancelJob = (id: number) => http.request<{ cancelled: boolean }>("post", `/jobs/${id}/cancel`);

export const retryFailedFiles = (id: number) =>
  http.request<{ retried: number }>("post", `/jobs/${id}/files/retry-failed`);

export const listMatches = (params: {
  job_id?: number;
  severity?: string;
  limit?: number;
  created_before?: string;
  id_before?: number;
}) => http.request<{ items: Match[]; next_cursor: { created_before: string; id_before: number } | null }>("get", "/matches", { params });

export const overview = () => http.request<Overview>("get", "/system/overview");
```
（`listMatches` 的响应形状必须与后端 `routes_matches.py` 实际返回一致：实现前用 `Read` 打开该文件确认游标字段名，若后端返回的是 `cursor` 而非 `next_cursor`，以后端为准并同步修正 `logharvestTypes.ts`。）

- [x] **Step 3: SSE 用 fetch 读（EventSource 不能带 Authorization）**

Create `web/src/utils/sse.ts`:

```ts
import { getToken } from "@/utils/auth";
import type { ProgressEvent } from "@/api/logharvestTypes";

/** 解析 SSE 文本块，返回完整的 `event: progress` 帧载荷。 */
export function parseSseChunk(buffer: string): { events: ProgressEvent[]; rest: string } {
  const frames = buffer.split("\n\n");
  const rest = frames.pop() ?? "";
  const events: ProgressEvent[] = [];
  for (const frame of frames) {
    const dataLine = frame.split("\n").find(line => line.startsWith("data: "));
    if (!dataLine) continue;
    events.push(JSON.parse(dataLine.slice(6)) as ProgressEvent);
  }
  return { events, rest };
}

export async function streamJobProgress(
  jobId: number,
  onEvent: (event: ProgressEvent) => void,
  signal: AbortSignal
): Promise<void> {
  const data = getToken();
  const response = await fetch(`/api/jobs/${jobId}/events`, {
    headers: { Accept: "text/event-stream", Authorization: `Bearer ${data?.accessToken ?? ""}` },
    signal,
  });
  if (!response.ok || !response.body) {
    throw new Error(`progress stream failed: ${response.status}`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parsed = parseSseChunk(buffer);
    buffer = parsed.rest;
    parsed.events.forEach(onEvent);
  }
}
```
（`getToken` 的确切导出名以 `web/src/utils/auth.ts` 为准；实现前读该文件确认，它保存的是 `{accessToken, expires, refreshToken}` JSON。）

- [x] **Step 4: 验证解析函数**

不引入前端测试框架（保持体量最小），改为把 `parseSseChunk` 的边界写进类型与注释，并用一次性脚本验证：

```bash
cd /e/GitHub/think/web && cat > /tmp/sse-check.mjs << 'EOF'
const src = await import("fs").then(m => m.readFileSync("src/utils/sse.ts", "utf8"));
const fn = src.match(/export function parseSseChunk[\s\S]*?\n}\n/)[0]
  .replace("export function", "function");
const { events, rest } = (new Function(`${fn}; return parseSseChunk;`)())(
  'event: progress\ndata: {"job_id":1,"status":"running","total":2,"by_status":{},"processed_lines":3,"matched_rows":4}\n\nevent: progress\ndata: {"job_id":1,"status":"succes'
);
console.log(events.length, JSON.stringify(events[0].status), rest.length);
EOF
node /tmp/sse-check.mjs
```
Expected: 输出 `1 "running" 68`（一帧被解析、残帧留在 rest）。若数值不符，说明分帧或 slice 偏移写错，必须改正后重跑。

Run: `cd /e/GitHub/think/web && pnpm typecheck 2>&1 | tail -10`
Expected: 无错误

- [x] **Step 5: 提交**

```bash
git add web
git commit -m "feat: 添加 LogHarvest 接口客户端与 SSE 进度解析"
```

---

### Task 4: 作业屏（列表 / 新建 / 详情 + 实时进度）

**Files:**
- Create: `web/src/views/logharvest/jobs/index.vue`
- Create: `web/src/views/logharvest/jobs/detail.vue`
- Create: `web/src/views/logharvest/jobs/create-dialog.vue`
- Create: `web/src/router/modules/logharvest.ts`
- Modify: `web/src/router/enums.ts`（追加 rank 常量）

**Interfaces:**
- Consumes: Task 3 的 `listJobs`、`getJob`、`listJobFiles`、`cancelJob`、`retryFailedFiles`、`streamJobProgress`；`listDataSources`/`listRuleSets`（Task 5 提供，本任务先用已实现的 `/data-sources`、`/rule-sets` 列表函数——若尚未存在，就在本任务补进 `logharvest.ts`）
- Produces: 路由 `/logharvest/jobs`、`/logharvest/jobs/:id`

- [x] **Step 1: 注册路由**

Create `web/src/router/modules/logharvest.ts`（照 `home.ts` 的结构，`component: Layout`，子路由懒加载）:

```ts
const LogHarvest = {
  path: "/logharvest",
  name: "LogHarvest",
  redirect: "/logharvest/jobs",
  meta: {
    icon: "ep/search",
    title: "日志分析",
    rank: 2,
  },
  children: [
    {
      path: "/logharvest/jobs",
      name: "Jobs",
      component: () => import("@/views/logharvest/jobs/index.vue"),
      meta: { title: "分析作业", rank: 1 },
    },
    {
      path: "/logharvest/jobs/:id",
      name: "JobDetail",
      component: () => import("@/views/logharvest/jobs/detail.vue"),
      meta: { title: "作业详情", showLink: false, rank: 2 },
    },
  ],
};

export default LogHarvest;
```

- [x] **Step 2: 作业列表**

`views/logharvest/jobs/index.vue` 用 `@pureadmin/table`：列 = `id`、`name`、`status`（tag 着色）、`total_files`、`total_bytes`（GB 保留两位）、`created_at`、操作（详情/取消）。服务端分页绑定 `limit`/`offset`；顶部提供"新建作业"按钮与状态筛选。终态作业（`succeeded|partial|failed|cancelled`）的取消按钮禁用。

- [x] **Step 3: 新建作业对话框**

`create-dialog.vue` 表单字段严格对应 `POST /api/jobs`：`name`、`data_source_id`（下拉，取 `/data-sources`）、`source_path`、`rule_set_id`（下拉，取 `/rule-sets`）、`file_filter`（`recursive` 开关、`max_depth` 数字 1–20、`max_files` 数字 1–20000）。提交时带 `Idempotency-Key` 请求头（前端生成 `crypto.randomUUID()`，同一次提交失败重试用同一个值）。`400`/`409`/`422` 分别提示"路径不合法"、"幂等冲突：请求内容已变更"、"参数校验失败"，文案来自后端 `detail`。

- [x] **Step 4: 详情 + SSE 进度**

`detail.vue`：
- 头部：作业状态、`total_files`/`total_bytes`、开始/结束时间、错误。
- 进度：`streamJobProgress(id, onEvent, controller.signal)` 驱动一个百分比（终态任务数 / `total`）与 `processed_lines`/`matched_rows` 计数。
- 文件任务表格：状态筛选（`status` 参数）、分页、显示 `attempts` 与 `worker_id`。
- 操作：取消作业、重试失败文件（显示返回的 `retried` 数量）。
- 生命周期：`onMounted` 起流，`onBeforeUnmount` 调 `controller.abort()`；作业进入终态时后端会自己结束流，前端捕获 `done` 后停止 loading。
- 失败降级：`streamJobProgress` 抛错时提示并回退到轮询 `getJob`（每 5 秒一次），不要白屏。

- [x] **Step 5: 验证与提交**

```bash
cd /e/GitHub/think/web && pnpm typecheck 2>&1 | tail -10
(cd /e/GitHub/think/service && LOGHARVEST_DATABASE_URL="sqlite:///./web-dev.db" uv run --python 3.12 uvicorn src.main:app --port 8000 >/tmp/u.log 2>&1 &)
cd /e/GitHub/think/service && LOGHARVEST_DATABASE_URL="sqlite:///./web-dev.db" uv run --python 3.12 python -c "print(1)"  # 确认库存在
sleep 8
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8848/
```
Expected: typecheck 无错误；vite 返回 200。清理：`rm -f /e/GitHub/think/service/web-dev.db`，杀掉 vite/uvicorn。

```bash
git add web
git commit -m "feat: 添加分析作业列表、新建对话框与 SSE 实时进度"
```

---

### Task 5: 规则集与数据源屏

**Files:**
- Create: `web/src/views/logharvest/rulesets/index.vue`
- Create: `web/src/views/logharvest/rulesets/edit-drawer.vue`
- Create: `web/src/views/logharvest/datasources/index.vue`
- Create: `web/src/api/logharvestAdmin.ts`
- Modify: `web/src/router/modules/logharvest.ts`

**Interfaces:**
- Consumes: `GET/POST /api/rule-sets`、`GET /api/rule-sets/{id}`、`GET/POST /api/data-sources`、`POST /api/data-sources/{id}/test`
- Produces: 路由 `/logharvest/rulesets`、`/logharvest/datasources`

- [x] **Step 1: 客户端**

Create `web/src/api/logharvestAdmin.ts`：`listRuleSets({limit,offset})`、`getRuleSet(id)`、`createRuleSet(payload)`、`listDataSources({limit,offset})`、`createDataSource(payload)`、`testDataSource(id)`；响应类型分别对应后端 `RuleSetRead`（含 `config`、`created_by`）、`DataSourceRead` 与 `/test` 的 `{exists, is_dir, readable}`。字段以 `service/src/api/v1/schemas.py` 为准。

- [x] **Step 2: 规则集列表 + 新建**

列表页展示 `name`/`version`/规则数/`created_at`，点击进入详情抽屉。`edit-drawer.vue` 是 JSON 编辑器（`el-input type=textarea` + 校验）+ 结构化表单两条路：**只做结构化表单**（更贴合后端校验，不做自由 JSON 提交，避免用户绕过校验）。字段严格按后端 `RuleSetConfig`：`name`、`file_glob`、`encoding`、`fallback_encoding`、`rules[]`（`id`、`severity`、`match.type ∈ {keyword, regex}`、`keyword.any|all` 互斥、`regex.pattern/flags/timeout_ms>0`、可选 `threshold{field,op,value}` 且 `field` 必须是正则命名分组）。提交 `422` 时把后端 `detail` 原文显示为表单错误（后端已做规则编译/pathspec/codec 校验，前端不重复实现同套逻辑，只做互斥项的基本提示）。新建成功后提示"已创建版本 N"（后端同名 max+1）。

- [x] **Step 3: 数据源页**

列表 = `name`/`root_path`/`enabled`/`created_at`；新建表单 = `name`、`root_path`、`description`，`read_only` 固定为 `true` 且只读显示（后端只接受 `true`，前端不提供开关）。每行"测试路径"按钮调 `POST /api/data-sources/{id}/test`，用三个 tag 显示 `exists`/`is_dir`/`readable`。

- [x] **Step 4: 路由与验证**

在 `logharvest.ts` 的 `children` 追加两条（`rank: 2` 规则集、`rank: 3` 数据源）。

Run: `cd /e/GitHub/think/web && pnpm typecheck 2>&1 | tail -10`
Expected: 无错误

```bash
git add web
git commit -m "feat: 添加规则集与数据源维护界面"
```

---

### Task 6: 命中结果浏览屏

> 实现依据 `main` 上的补记「实现期核实到的计划错误」：列表接口返回裸数组、
> `/api/matches` 无 `next_cursor` 且缺作用域返回 **422**（不是 400）。以那一节为准。

**Files:**
- Create: `web/src/views/logharvest/matches/index.vue`
- Modify: `web/src/api/logharvest.ts`（如需补参数）
- Modify: `web/src/router/modules/logharvest.ts`

**Interfaces:**
- Consumes: `GET /api/matches`（强制 `job_id` 或 `created_from`+`created_to`；游标为 `created_before` + `id_before`，成对提供；`severity` 过滤；`limit` ≤ 500）
- Produces: 路由 `/logharvest/matches`

- [x] **Step 1: 查询表单与约束**

必填校验按后端规则实现：要么选 `job_id`（下拉 + 从作业列表取），要么给时间范围 `created_from`/`created_to`（成对）；两者都空时禁用查询按钮并提示后端会返回 `422` 的原因（detail 原文：`either job_id or both created_from and created_to are required`）。`severity` 按后端实际契约做成**单值**下拉 `info|warning|error`（`routes_matches.py` 的 `severity: str | None` 是等值过滤，不支持多选）。

- [x] **Step 2: 游标翻页表格**

列 = `created_at`、`job_id`、`file_name`、`member_path`、`line_no`、`rule_id`、`severity`、`numeric_value`、`raw_line`（超长行显示 `raw_truncated` 标记）。翻页只支持"下一页"（游标语义是 `created_at DESC, id DESC`），顶部按钮"回到第一页"清空游标；不要提供页码跳转，后端没有 offset。把当前游标保存在组件状态里，取自后端返回的最后一行 `{created_at, id}`（后端不返回 `next_cursor`，游标必须前端自己拼）。

- [x] **Step 3: 验证与提交**

Run: `cd /e/GitHub/think/web && pnpm typecheck 2>&1 | tail -10`
Expected: 无错误

```bash
git add web
git commit -m "feat: 添加命中结果游标浏览界面"
```

---

### Task 7: Worker 管理与概览看板

**Files:**
- Create: `web/src/views/logharvest/workers/index.vue`
- Create: `web/src/views/logharvest/overview/index.vue`
- Modify: `web/src/router/modules/logharvest.ts`
- Modify: `web/src/views/welcome` 的替代：把 `home.ts` 的默认子路由指向 `overview`

**Interfaces:**
- Consumes: `GET /api/workers`、`POST /api/workers/{id}/drain|resume`、`POST /api/workers/drain-version?version=`（返回 `{drained, flagged}`）、`GET /api/system/overview`、`POST /api/system/maintenance`
- Produces: 路由 `/logharvest/overview`、`/logharvest/workers`

- [x] **Step 1: Worker 页**

列 = `id`、`hostname`、`version`、`status`（`online/busy/draining/offline` 四色）、`current_task_id`、`max_concurrent_copies`、`last_heartbeat`（相对时间）、`drain_requested`。操作：单台 drain/resume（离线时 resume 按钮禁用，因为后端返回 `409`）；顶部"按版本排空"输入版本号，提交后把返回的 `{drained, flagged}` 原样提示为"已排空 N 台，标记 M 台离线（重启后生效）"。页面文案要显式说明：**这里不执行部署，镜像替换由 Jenkins + Docker Compose 完成**（与主设计 §10 一致）。Worker 列表同样是裸数组 + `limit`（上限 200）/`offset` 窗口翻页，没有总数。

- [x] **2: 概览看板**

`overview/index.vue` 展示 `GET /api/system/overview`：作业状态分布、文件任务状态分布、`matches_24h`、在线 Worker 数与版本分布、`oldest_pending_seconds`（超过 300 秒高亮）。附"执行维护"按钮调 `POST /api/system/maintenance`，把 `{recovered, workers_demoted, jobs_finalized}` 显示为结果提示。图表不引入 echarts（Step 3 of Task 1 已移除该插件），用 Element Plus 的进度条 + 数字卡片即可。**两个端点都由 M2a Task 5 并行补充，本工作树 `service/` 里还没有 → 按约定契约实现，标记为「合流后需复核」；概览失败时退回 `GET /api/system/status`（`{file_tasks, pending_oldest_seconds, workers_online}`）渲染可得字段，不留空。**

- [x] **Step 3: 首页指向概览**

Modify `web/src/router/modules/home.ts`：把默认子路由 `component` 从 `@/views/welcome/index.vue` 改为 `@/views/logharvest/overview/index.vue`，`meta.title` 改为"概览"。（`views/welcome` 目录已在 Task 1 删除，这里必须改掉，否则 typecheck 失败。）骨架阶段的 `views/home/index.vue` 随之下线，`layout/types.ts` 的固定首页标签标题同步为"概览"。

- [x] **Step 4: 验证与提交**

```bash
cd /e/GitHub/think/web && pnpm typecheck 2>&1 | tail -10
cd /e/GitHub/think/web && pnpm build 2>&1 | tail -12
```
Expected: typecheck 无错误；build 成功产出 `dist/`。把 `dist/` 加进根 `.gitignore`（已确认 `dist/`、`node_modules/` 两条模式覆盖 `web/dist`、`web/node_modules`，`git check-ignore` 通过，无需改动）。

```bash
git add web .gitignore
git commit -m "feat: 添加 Worker 管理与概览看板界面"
```

---

### Task 8: 前端文档与端到端联调收口

**Files:**
- Modify: `web/README.md`
- Modify: `service/README.md`（前端如何联调）
- Modify: `README.md`（根）
- Modify: `.env.example`

**Interfaces:**
- Consumes: Task 1-7 全部
- Produces: 可照做的开发/联调步骤，与代码一致的接口清单

- [ ] **Step 1: 写联调文档**

`web/README.md` 追加"与后端联调"小节：后端用 SQLite 起服务（`LOGHARVEST_DATABASE_URL=sqlite:///./web-dev.db`）→ `python -m scripts.seed_admin` → `uvicorn src.main:app --port 8000` → `pnpm dev`（8848，`/api` 代理）。写明队列/Worker 需要 PostgreSQL，但纯 API 联调不需要；以及 SSE 用 `fetch` 读取的原因（`EventSource` 不能带 `Authorization`）。

`README.md`（根）：三服务架构的前端从"待做"改为"已落地"，列出五屏与登录，并如实写明：菜单是前端静态定义（无服务端动态菜单）、未做人工视觉确认（本环境内置浏览器为隐藏页）。

`.env.example`：如新增了前端相关变量则补上；否则不动。

- [ ] **Step 2: 端到端冒烟**

```bash
cd /e/GitHub/think/service && LOGHARVEST_DATABASE_URL="sqlite:///./web-dev.db" uv run --python 3.12 pytest -q
cd /e/GitHub/think/web && pnpm typecheck && pnpm build
```
Expected: 后端全量绿；前端 typecheck/build 绿。清理临时库 `rm -f /e/GitHub/think/service/web-dev.db`。

- [ ] **Step 3: 提交**

```bash
git add web/README.md service/README.md README.md .env.example
git commit -m "docs: 补充前端移植与联调说明"
```

---

## Plan Self-Review

**1. Spec coverage（主设计 §3/§6/§7.4 的前端要求）：**
- 任务创建与状态跟踪 → Task 4（含 SSE 实时进度）。
- 规则录入（规则集版本化）→ Task 5。
- 数据展示（命中结果）→ Task 6。
- Worker 可视化管理（状态/版本/当前任务/drain/resume/按版本排空）→ Task 7；"Web 不执行部署"写进页面文案与文档。
- 登录 → Task 2；概览看板 → Task 7。
- 不覆盖并显式排除：服务端动态菜单与按钮权限、结果导出、Alembic/部署编排（属后端或未实现能力）。

**2. Placeholder scan：** Task 2 Step 2 与 Task 3 Step 2/3 各有一处"以后端实际字段为准，实现前先读源码确认"的指令——这是有意的核对动作（不是 TBD），因为并行轨道下后端字段可能微调；每处都给了具体的文件与预期形状。Task 1 Step 1 对基座文件名留了 `ls -a` 核对，其余步骤都给了可直接执行的命令或代码。

**3. Type consistency：**
- 信封类型只在 `api/types.ts`（认证）出现；业务类型全部在 `api/logharvestTypes.ts`，两处不互相引用。
- `ProgressEvent` 与后端 SSE payload 字段一致：`job_id`/`status`/`total`/`by_status`/`processed_lines`/`matched_rows`。
- 分页：业务列表统一 `Page<T>{items,total,limit,offset}`；命中结果例外（游标），已在 Task 6 Step 2 明确不提供页码跳转。
- Worker 状态枚举与后端 `routes_workers.py`/`mark_dead_workers` 使用的 `online|busy|draining|offline` 一致。
- `drain-version` 返回 `{drained, flagged}` 与 M1b 终审后的实际响应一致（不是 M1b 初版的 `{drained}`）。
- Task 7 Step 3 依赖 Task 1 删除 `views/welcome` 这一事实，顺序不可颠倒。
