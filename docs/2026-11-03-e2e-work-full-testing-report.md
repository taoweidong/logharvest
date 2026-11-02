# 2026-11-03 前后端 work 功能全量真实测试报告（E2E）

## 1. 测试环境与方法

- **部署**：wslc 真实容器栈（`bash e2e/up.sh --fresh`，与生产 `deploy/*` 三栈一一对应）——PostgreSQL **18.6**（127.0.0.1:15544）、server（uvicorn，:18000，health 200）、web（nginx 发版 dist，:18080，HTTP 200）、worker ×2（v0.1.0，NAS 只读 bind mount + 临时盘卷，非 root uid 10001）。管理员种子后 `/api/login` 探针通过才开测。
  - 基座钉扎镜像（python:3.12 / nginx:1.20 / postgres:18.6）本机直连 docker.io 超时，按 `up.sh` 预留的 `BASE_*` 口径改走 daocloud 镜像站拉取后 tag 回白名单名；postgres:18.6 用本地既有镜像（`postgres --version` 实测 18.6-1.pgdg13+2 与白名单钉扎一致）打标。
- **数据**：`e2e/make_nas.py` 生成的 NAS 数据集（clean/formats/deep/empty/cancel 等目录）+ `fixtures.json` oracle（26 行 / 14 命中 / 按规则分布），断言期望与夹具同源。
- **方法**：接口层用真实 HTTP 全流程断言（状态码、幂等键、逐字文案、oracle 对账）；页面层用 Playwright 真实 Chrome **真实点击**（文案即选择器），断言可见文案、disabled、确认弹窗、真实请求 URL/payload；视口除 1440×900 外补测 1366×768 / 1280×720（常见笔记本分辨率）。
- **口径**：退出码 0 = 全部断言通过；台账落 `e2e/artifacts/ledger_*.json`（不入库）。

## 2. 覆盖范围与最终结果（全部通过）

| 套件 | 覆盖 | 结果 |
|---|---|---|
| `work_api_flow.py`（本次新增，W0–W11） | work 功能全接口：认证 401/403、数据源/规则集夹具、作业创建契约（幂等重放 200/冲突 409/空白键 400、404×2、越界/缺目录/文件目录 400、空名与 filter 越界 422、非法后缀与 max_files 超限 400、后缀过滤/深度过滤/空目录即时终态）、列表与文件任务分页边界（limit 0/1/500/501、offset -1/超界、status 下推与非法 422）、尝试历史（200 []/跨作业 404）、Worker 真实执行与 oracle 对账（26 行/14 命中/按规则分布/尝试历史字段）、取消状态机（pending 置 cancelled、终态幂等、404、已取消重试 409）、失败重试（SQL 模拟 failed → retried 计数 → Worker 重新领取成功 → 尝试累计 2 次 → 作业回终态）、Worker 接口（排空/恢复/按版本排空/未知版本 {0,0}/离线 409/遗留离线行 409，存活口径）、SSE（终态 1 帧、max_events 封顶与 -1/101 → 422、404、401）、RBAC（viewer 全写 403/operator 可操作可读审计/viewer 审计 403）、审计留痕与 overview 一致性、测试用户清理 | **116 通过 / 0 失败** |
| `api_flow.py`（P0–P7 全量，夹具基座） | 健康检查、认证边界、数据源（e2e-nas 等）、规则集、契约形状、抓取矩阵、命中与检索、报表对账、Worker 上报 | 368 通过 / 4 跳过 / 0 失败 |
| `ui_flow.mjs`（U0–U15 全阶段，真实点击） | 登录页、三角色可见性、建任务向导三段校验、作业列表（筛选/关键字/UTC 日提示/翻页）、作业详情（描述表/SSE 回填/文件任务/尝试历史抽屉/**矮视口表体高度回归断言**）、命中结果、数据源、Worker 分组/排空/恢复、任务统计跳转、规则集/规则维护、系统管理、操作日志、登出 | 见 §2.1 |
| `web` vitest + vue-tsc | 前端单元测试与类型门禁 | 132 通过 / 0 失败；typecheck 干净 |

### 2.1 UI 全量三轮收敛

| 轮次 | 结果 | 失败归因 |
|---|---|---|
| 第 1 轮（改造后首次全量） | 630 通过 / 10 失败 | 全部为**数据污染**：此前手工/API 测试先建了 `work-flow-*` 数据源与 wview/woper 用户，而 ui_flow 的 U0 前置要求夹具 `e2e-nas` 由 `api_flow` 先行创建 |
| 第 2 轮（全新库 + api_flow 先行） | 868 通过 / 3 失败 | 三个 harness 缺陷（见 §3.2 T1–T3），与产品无关 |
| 第 3 轮（修复 T1–T3 后） | **872 通过 / 18 跳过 / 0 失败（共 890 条）** | — |

## 3. 问题清单（复现 → 定位 → 修复 → 回归）

### 3.1 产品缺陷（1 项，已修复并回归）

| # | 现象与复现 | 定位 | 修复 | 回归 |
|---|---|---|---|---|
| P1 | **作业详情页「文件任务」表格塌陷**。复现：登录 → 任一作业「详情」→ 视口高 ≤768px（如 1366×768、1280×720）→ 文件任务区只见表头，数据行被表头盖住（720p 实测表体 wrapper 仅 **5px**）→ 行内「历史」按钮真实点击 5s 超时（`elementFromPoint` 命中 `TH.el-table_*` 表头而非按钮）。1440×900 视口正常（此前 e2e 只在 900 高跑过，故未拦住） | `6ce409f` 表格高度 flex 化把全站表格统一为 `height="100%"` + `.page-panel--fill`（吃满视口剩余高度）。列表页（无前置内容）余量充足；详情页表格上方还有信息卡片（el-card ≈315px）+ 面板头/页脚，剩余高度被压成 45px（表头 40px + 表体 5px）。flex 链本身按设计工作，是「--fill 模式」与「上方大块内容」的组合缺陷 | `detail.vue` 给文件任务面板挂 `files-panel--stacked` 类；`layout.scss` 新增 `.files-panel--stacked .page-body { min-height: 340px }`：高视口仍由 flex 拉满（不露灰底），矮视口表体稳定 300px、由 `.page-panel--fill` 自带 `overflow:auto` 面板内滚动兜底 | 重建 web 镜像后三视口实测表体均 300px（720/768/900）；「历史」按钮真实点击打开抽屉且数据正确；`ui_flow` phase_detail 新增矮视口回归断言（1366×768 表体 ≥200px）常驻防复发 |

### 3.2 E2E 脚本缺陷（3 项，全部修复并回归）

| # | 现象 | 定位 | 修复 |
|---|---|---|---|
| T1 | U4「日期提示在本页内过滤」失败 `{"todayBase":0,"rowsToday":28}` | 两层：① `waitText('分析作业')` 只等标题，行数据在飞行中，基线读到 0；② 断言用 `new Date()` 的 UTC「今天」与**本地时区格式化**的显示文本对账，UTC+8 与 UTC 日错位时必然假失败（产品按 UTC 日过滤的行为自洽，页面脚注也如实标注） | 基线等待首屏行渲染后再取；参照日改为**接口回读最新作业的 `created_at` UTC 日**，断言改为「有匹配行、不超过基线、无匹配日为空态」 |
| T2 | U7「跳转 URL 带上 data_source_id」失败（URL 带 `data_source_id=3`） | 断言钉死 `=1`，假设 e2e-nas 永远是 1 号数据源；其它套件先建数据源时 id 后移 | 改用夹具回读的 `st.nasSource.id` 匹配 |
| T3 | U12「用户列表回到内置三行」actual=5 | `work_api_flow.py` 的 RBAC 阶段创建的 wview/woper 测试用户未清理，污染绝对计数 | `work_api_flow.py` 新增 W11 清理阶段：删除本脚本创建的用户并清扫历史同前缀残留；W7 阶段同时改为存活口径（api_flow P7 按设计遗留的离线 worker-3 行不再让「两台在册」与 resume 409 误报） |

### 3.3 测试环境问题（1 项）

| # | 现象 | 处理 |
|---|---|---|
| E1 | `up.sh` 拉基座镜像直连 docker.io 超时（registry-1.docker.io 连接超时） | nginx:1.20 / python:3.12 走 daocloud 镜像站拉取后 tag；postgres:18.6 复用本地镜像（`postgres --version` = 18.6 与钉扎一致）。全程未改生产 Dockerfile 与 env.sh 默认值 |

## 4. 结论

- work 功能（作业/Worker 任务流水线）前后端 **116 条接口断言 + 872 条页面断言全绿**，真实执行路径与夹具 oracle 完全对账（26 行 / 14 命中 / 按规则分布 / 尝试历史 / SSE 快照）。
- 发现并修复产品缺陷 1 项（详情页表格矮视口塌陷），修复后以多视口实测 + 常驻 e2e 断言双重回归；harness 缺陷 3 项一并修复。
- 未改动任何后端代码；前端改动仅 `detail.vue`（+1 类名与注释）与 `layout.scss`（+1 条兜底规则）。

## 5. 复跑指引

```bash
bash e2e/up.sh --fresh                                    # 全新栈（含镜像构建、卷属主、管理员种子）
PYTHONIOENCODING=utf-8 uv run --python 3.12 python e2e/api_flow.py --phase full   # 夹具基座（e2e-nas 等）
uv run --python 3.12 python e2e/work_api_flow.py          # work 功能接口全量（含自清理）
bash e2e/ui_run.sh                                        # 页面全量（U0-U15，串行锁）
```
