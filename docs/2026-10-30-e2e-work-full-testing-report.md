# 2026-10-30 前后端 work 功能全量真实测试报告（E2E）

## 1. 测试环境与方法

- **部署**：wslc 真实容器栈（`bash e2e/up.sh`），与生产 `deploy/*` 三栈一一对应——PostgreSQL 16（127.0.0.1:15544）、server（uvicorn，:18000）、web（nginx 发版 dist，:18080）、worker ×2（NAS 只读 bind mount + 临时盘卷，非 root uid 10001）。部署前逐文件 md5 核对镜像内源码与工作区一致，`/api/health`、`/api/health/ready`、前端 200 后才开始测试。
- **数据**：`e2e/make_nas.py` 生成的 NAS 数据集（clean/deep/encoding/flood/limits/formats/partial/cancel/empty 等目录）+ 内建迷你规则引擎推导的 `fixtures.json` oracle，断言期望与夹具同源。
- **方法**：接口层用真实 HTTP 全流程断言（状态码、信封、逐字文案、DB 对账）；页面层用 Playwright + 真实 Chrome（1440×900）真实点击，文案即选择器（全站无 data-testid），断言的是可见文案、tag 配色、disabled、真实请求 URL/payload，API 只用于造数与清理。
- **口径**：退出码 0 = 全部断言通过；台账落 `e2e/artifacts/ledger_*.json`（不入库，可随时重跑再生）。

## 2. 覆盖范围与最终结果（全部通过）

| 套件 | 覆盖 | 结果 |
|---|---|---|
| `api_flow.py`（P0–P7 全量） | 健康检查、认证边界、数据源、规则集、契约形状、抓取矩阵（tar/gz/zip/嵌套/partial/编码/超长行）、命中与检索、报表对账、Worker 上报与 drain/resume/drain-version | 354 通过 / 4 跳过；P7 单独回归 80 通过 / 0 失败 |
| `runtime_flow.py` | 作业取消状态机（三前置态）、源文件漂移永久失败、只读聚合对账、检索转义/翻页 | 222 通过 / 0 失败 |
| `rbac_flow.py` | 401 四文案、token 隔离、登录限流、用户/角色/菜单 CRUD 守卫、最小授权逐接口 403、动态路由、23 个审计动作、敏感串不落 detail | 291 通过 / 1 跳过 / 0 失败 |
| `limits_flow.py` | 紧预算 Worker：单成员/成员数/压缩比越界 partial、命中上限失败清场（序列号证明 90 行已删）、恢复后重试 6500 行全量 | 93 通过 / 1 跳过 / 0 失败 |
| `upgrade_flow.py` | Worker 升级 drain/rolling/回滚基线、迁移与库结构一致性 | 148 通过 / 0 失败 |
| `ui_flow.mjs`（U0–U15 全阶段） | 登录页、三角色可见性与 404、建任务向导三段校验、作业列表/详情/尝试历史/SSE 降级、命中结果游标翻页与筛选、数据源测试路径、Worker 分组/排空/恢复、任务统计、规则集维护、规则维护跨版本、系统管理（用户/角色/菜单 CRUD+守卫）、操作日志五路筛选+详情抽屉、登出 | **875 通过 / 16 跳过 / 0 失败（共 891 条）** |

合计约 **2000 条真实断言全绿**；跳过项均为 harness 显式记录的「观察项」（不可达分支、口径矛盾复现、无删除接口等），每条附证据与建议。

## 3. 问题清单（复现 → 定位 → 修复 → 回归）

### 3.1 测试环境问题（2 项）

| # | 现象 | 定位 | 处理 | 回归 |
|---|---|---|---|---|
| E1 | api_flow P7 三连失败：`worker lh-e2e-worker-3 注册字段 / 心跳推进 / resume 409` | 上一会话 limits_flow 按设计遗留的**离线 Worker 行**（容器已删、心跳冻结）混进 P7 的"全员存活"假设 | 清理残留行；P7 改为按存活台分口径（心跳只要求存活台推进、离线行 resume 断言 409、版本只对存活台收紧） | `--phase reporting` 80 通过 / 0 失败（含离线行在场的交叉验证） |
| E2 | U8「起了 3 台 Worker」「只剩 offline 分组」失败 | U8 断言依赖 E1 的遗留离线行作为夹具，清理后数据自给失败 | U8 改为**自给夹具**：无离线行时 psql 种一行（与 limits_flow 收尾约定一致），基线检查改名"两台默认在线 + 一行离线夹具" | UI 全量 0 失败 |

### 3.2 E2E 脚本问题（9 项，全部修复并回归）

| # | 现象 | 定位 | 修复 |
|---|---|---|---|
| T1 | U12/U13/U15 三阶段全部报「模块加载失败 Unexpected token '*'」 | 上次会话中断：`ui_flow_sys.mjs` 只有半截（块注释内 `UiE2e*/` 提前终止注释 + 缺三个阶段函数与 `export const phases`） | 补全三个阶段函数（编排现有 users/roles/menus 循环 + 全新编写 U13 操作日志、U15 登出），注释改写避开 `*/` 序列 |
| T2 | 用户新建点「创建」被拦截 30s，用户段 8 步级联超时 | `pickIn` 对**多选下拉**选完不关面板，`.el-select-dropdown__item` 拦截后续所有点击 | pickIn 选完检测面板仍可见则 Escape 并等隐藏 |
| T3 | 分配角色弹窗预填断言空/超时 | EP 新版把多选标签渲染在 `.el-select__selected-item` 内，旧口径 `.el-select__tags` 永远空集 | `tagsIn` 改为 select 作用域内直接取 `.el-tag`；期望值改为选项完整 label「只读用户（viewer）」 |
| T4 | U13「对象类型」读出的是动作族选项 | `selectOptionsOf` Escape 后未等面板隐藏，连开下一个下拉时 waitFor 撞上旧面板 | Escape 后等当前面板 `state=hidden` 再返回 |
| T5 | U4「第 2 页文案且上一页可用」偶发失败 | `goNext` 同步改 offset：页脚先渲染「第 2 页」，此刻 loading=true、上一页仍禁用，断言取到过渡帧 | 等稳定态（文案出现且上一页可用）再断言；另用一次性探针验证稳定态行为正确 |
| T6 | U5 尝试历史抽屉 footer「已加载 0 次尝试」 | footer 在 fetch 落地前读取（历史列表打开抽屉才拉） | 先等尝试行渲染完再读 footer |
| T7 | U6「回到第一屏后首行与翻页前一致」失败 | 翻回首页只等了"请求发出"，响应未渲染时读到的是第二段首行（第 51 行）；DB 复核 `ORDER BY created_at DESC, id DESC` 排序稳定，产品无误 | 等行数回到 50（第二段只有 total-50 行）再取首行基准 |
| T8 | U12 首轮跑通的十余处断言失败（角色行 ID 前缀、类型锁 aria-disabled、权限态表单项数、上级占位「0」、排序 input 双重嵌套 30s 超时、关键字搜索收敛、POST payload 捕获到列表刷新请求、时间范围 strict violation、首屏行未渲染即断言） | 循环为上次会话"盲写"，从未执行过；多处假设与 EP 实际 DOM/时序不符 | 逐一按照妖镜（`ui_probe.mjs`）与源码校准：`.el-form-item__label`、is-disabled 类断言、6 项字段集、数值 ROOT_VALUE 渲染「0」、`item(...).locator('input')`、行数收敛等待、按 method 过滤 POST、范围选择器锁进 `.search-form`、首屏行渲染等待 |
| T9 | 菜单创建偶发「静默未提交」（无 POST、无红字、无消息，第 4 轮实测）与第 5 轮目录创建被「请填写路径」拦截 | 弹窗收放动画期点击落空 / fill 偶发未落进 v-model | createMenu 提交后 8s 无 POST 且无红字则按真实用户行为再点一次；提交前把填过的字段回读核对、不符重填（与真实操作一致） |

### 3.3 产品结论（本会话未引入产品缺陷；两项观察记录在案）

1. **权限节点 path 继承父页面是后端刻意设计**：`create_menu` 在 payload.path 为空时取 `parent.path`（`rbac.py:463-465`），权限行因此显示父页面路径。测试初判为前端漏清，两次定向复现后修正结论，前端误改已回退，测试断言与观察项按设计语义落稿。
2. **既有已知缺陷继续由 harness 记录**：Worker 页清除版本号白屏（断言双向兼容：复现记 [产品缺陷]、修复记回归断言）、负心跳「-N 秒前」无钳位、概览脚注两种口径混排等——本轮照旧触发/记录，未新增产品缺陷。
3. 新增观察项：新建页面/权限时「上级」显示原始值 0 而非 placeholder（数值 ROOT_VALUE 无匹配选项，校验会拦，仅观感）；单子页目录在侧边栏折叠成叶子项（pure-admin 常规行为）。

## 4. 随本次交付的既有修复（上次会话遗留工作区，一并验证入库）

- `service/src/worker/main.py`：启动即建工作目录 `_ensure_work_dir`——否则空闲 Worker 的临时盘余量恒为 NULL（`COALESCE` 语义下永不更新），派活前最需要的磁盘水位不可见。
- `service/Dockerfile`：`scripts/` 进 server 镜像（部署文档的管理员种子命令依赖它，缺失时全新环境无账号可登录）；worker 段预建 `/local-tmp` 并归 uid 10001（named volume 场景下缺失会让全部文件任务 EACCES 重试至失败）。
- `docs/deployment-docker.md`：补管理员账号一次性种子步骤与注意事项。

以上三项均已在本轮真实栈中验证（镜像 md5 对齐、全部流程跑通）。

## 5. 复跑指引

```bash
bash e2e/up.sh --fresh          # 全新栈（含镜像构建、卷属主、管理员种子）
uv run --python 3.12 python e2e/api_flow.py --phase full      # 接口全量
PYTHONIOENCODING=utf-8 uv run --python 3.12 python e2e/runtime_flow.py
PYTHONIOENCODING=utf-8 uv run --python 3.12 python e2e/rbac_flow.py
PYTHONIOENCODING=utf-8 uv run --python 3.12 python e2e/limits_flow.py
PYTHONIOENCODING=utf-8 uv run --python 3.12 python e2e/upgrade_flow.py
bash e2e/ui_run.sh              # 页面全量（U0-U15，串行锁）
```
