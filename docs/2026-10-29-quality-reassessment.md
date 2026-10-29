# LogHarvest 系统性质量重评估（第三轮，2026-10-29）

> 本文是对**当前代码**（`main` @ `f72412a` + 本轮工作区）的第三轮全维度复核。
> 前两轮报告（`2026-10-28-quality-assessment-report-and-optimization-plan.md`、
> `2026-10-29-quality-assessment-and-optimization.md`）中的问题清单已大量闭环；
> 本文只记录**当前仍成立**的结论、本轮新关闭项与剩余分阶段方案，已闭环项不再展开。

- 基线实测（本轮）：后端 **685 passed / 1 skipped**（含 19 条真实 PostgreSQL 集成用例，
  `LOGHARVEST_TEST_PG_URL` 指向本机 PG16），覆盖率 **98%**（3,873 语句 / 84 miss），
  `ruff check` / `ruff format --check` / `mypy`（30 文件）全绿；前端 **102 passed / 11 spec**，
  `vue-tsc` 0 错误。E2E 性能基准三场景全绿（见 §3.8）。
- 代码量：后端 69 个 `.py`，前端 141 个 ts/vue。
- 方法：全量门禁（含真实 PG）+ 九维度逐项 grep/抽查 + 遗留项与两份前报告逐条对照。

---

## 一、结论摘要

**三轮审计与两轮实施后，前两轮清单上的全部 P0/P1 代码缺陷均已闭环**（含本轮由并行
工作补上的 Q-04 PG 入 CI、Q-15 partial 作业汇总）。当前**没有新的 P0/P1 代码缺陷**；
剩余风险全部是"需要外部环境或专项排期"的工程项（CSP 浏览器验证、依赖 CVE、大文件
拆分、分区/保留期），本轮再关闭其中可独立验证的**容器硬化**（Q-09）。

| 维度 | 结论 | 本轮证据 |
|---|---|---|
| 代码结构与分层 | 强 | `grep api.v1.schemas\|HTTPException src/application` 仅命中 2 处**文档字符串**，代码零命中 |
| 命名与可读性 | 良好 | 10-29 报告指出的 `client_ip` 注释漂移、README 取消检查频率漂移均已同步 |
| 重复与冗余逻辑 | 后端低、前端登记 | 后端 10 行/80 token 阈值近似 0；前端窗口分页等 4 项 P2 登记 |
| 错误与异常处理 | 强 | 命中上限、失败清场、瞬时 OSError 分类已落地（630→685 用例的增量主体） |
| 边界与空值 | 强 | 解压预算四维、长行截断、tar 结束块、`(?i)` 内联标志下的 hint 放弃（本轮新增） |
| 接口契约一致性 | 强 | 47 端点 response_model 覆盖；前端 40 调用路径 0 漂移（继承 10-29 复核） |
| 安全隐患 | 明显改善 | 可信代理 CIDR、readiness、非 root 容器（本轮）、nginx 安全头（本轮）；剩余 CSP 验证/CVE |
| 性能瓶颈 | 大幅改善 | 部分索引、AC 预筛、编译/配置缓存、批量插入——三场景 E2E 2.0×/1.3×/9.6× |
| 测试覆盖 | 后端 98%、前端纯逻辑全 | PG 集成 19 条从 skip 变为真实执行（本机 + CI 均绿）；剩余缺口为组件/E2E |

## 二、九维度逐项复核（当前代码）

### 2.1 代码结构与分层

- 依赖方向 `api → application → domain ← infrastructure`、`worker → application` 持续成立；
  `grep -rn "api.v1.schemas|HTTPException" src/application` 仅命中 rbac.py/jobs.py 的
  **文档字符串**（描述"路由层翻译 HTTPException"），代码引用为零。
- `TaskProcessor.process` 复杂度热点（Q-13）仍在（本轮性能改动刻意避免拆分以控制回归面）。

### 2.2 命名与可读性

- 前两轮指出的三处文档漂移（取消检查频率、`client_ip` 代理语义、命中上限说明）已全部同步。
- 新增代码（`aho.py`、`RuleIndex`、`_mandatory_literal`）文件头均写明动机与被证伪的替代方案。

### 2.3 重复与冗余逻辑（剩余 P2，均登记）

前端窗口式分页 ×8、错误详情提取 ×3、数据源只读三态 ×2、`listRuleSets` page size 不一致
——保持 10-29 登记，本轮不动（改动面大于收益）。

### 2.4 错误与异常处理

- Worker 四类终态路径（LeaseLost / JobCancelled / PermanentTaskFailure / RetryableTaskFailure）
  + 命中上限（`MatchLimitExceeded`）+ 失败清场全部有断言测试。
- 求值器新增两条防御分支有测试：`(?i)` 内联标志放弃 hint 提取；顶层 `|` 作废全部字面量段。

### 2.5 边界与空值

- 本轮新增边界覆盖：AC 前后缀重叠针（`ab`/`abc` 共现）、空针恒真语义、全折叠关键字
  （`tıtle`/`ſtatus=`）在索引与朴素两条路径的一致性。
- 解压预算/长行/损坏容器边界维持 10-29 结论。

### 2.6 接口契约一致性

- OpenAPI 47 端点（含 readiness）；`RuleIndex` 等新增内部结构不影响契约。
- E2E 跨运行逐规则计数零差异，是最强的契约回归信号。

### 2.7 安全隐患

| 项 | 状态 |
|---|---|
| 登录限流代理坍缩（Q-01） | ✅ 可信代理 CIDR 落地（默认不信任），helpers 9 条用例 |
| 容器非 root（Q-09 部分） | ✅ **本轮**：server/worker uid 10001、web nginx 非 root 监听 8080，init-dirs.sh 属主同步 |
| nginx 安全头（Q-09 部分） | ✅ **本轮**：nosniff / SAMEORIGIN / Referrer-Policy；CSP 模板注释（启用需浏览器验证） |
| 依赖 CVE（Q-07） | ⬜ `python-jose`/`ecdsa` 版本仍受影响；迁移 PyJWT/joserfc 需专项 |
| token 存储 / CSP（Q-06 关联） | ⬜ 非 HttpOnly cookie + localStorage；CSP 模板就绪待验证 |
| PG 凭据 / 密钥管理 | ✅ compose 强制显式口令；Jenkins 凭据注入；`.env` 不入库 |

### 2.8 性能瓶颈

| 项 | 状态 |
|---|---|
| 队列领取/回收索引（Q-03） | ✅ 0006 部分索引；E2E claim 0.12–0.26ms |
| 5k 规则求值 | ✅ **本轮**：AC 预筛 + 必选字面量 hint + 编译/配置缓存——358 → 3,532 行/s（9.6×） |
| 建作业大批次 | ✅ **本轮**：executemany 批量插入，0.50s → 0.14s @2000 任务 |
| `matches` 分区/保留期（Q-12） | ⬜ 千万级命中下的存储演进，排期项 |
| SSE 每观看者每秒 2 查询 | ⬜ 聚合/降频登记为后续 |
| 前端规则集列表 N+1、主包体积（Q-11） | ⬜ 登记 |
| 命中上限为单任务级 | ⬜ 作业级上限/采样需与 Q-12 一起排期 |

### 2.9 测试覆盖

- 后端 **685 passed / 1 skipped**（唯一 skip 是 Windows 不支持 FIFO，属平台限制）；
  覆盖率 98%（84 miss 集中在入口胶水与防御分支）。
- **19 条真实 PG 集成用例在本机真实执行全绿**（含本轮 CLAIM SQL 改动与 0006 迁移）；
  CI 侧 `Jenkinsfile` 已接 `scripts/run_pg_integration.sh`（空库迁移 + 索引断言 + 集成套件）。
- 前端 102 条（纯逻辑 + API 契约 + RBAC 契约）；组件/E2E 仍缺（Q-06 登记）。

## 三、本轮新关闭项（带验证证据）

| 项 | 变更 | 验证 |
|---|---|---|
| 求值预筛索引（性能 P1） | `domain/rules/aho.py`（AC 自动机）、`RuleIndex` 候选索引、`_mandatory_literal` 提取器；`(?i)` 与顶层分支的保守放弃 | 随机规则集 30 轮 + 边界等价性测试；E2E 逐规则零差异；9.6× |
| 求值编译/配置缓存（性能 P1） | WorkerRuntime FIFO 64；PostgresTaskRepository FIFO 256 | 单测（同版本只编译一次、缓存键与对象同一）；真实 PG 集成全绿 |
| 建作业批量插入（性能 P2） | `jobs.create_job` executemany | 既有作业创建用例零修改通过；E2E 2000 任务 0.50s→0.14s |
| 容器非 root（Q-09） | `service/Dockerfile` USER app(uid 10001)、`web/Dockerfile` USER nginx:8080、`init-dirs.sh` chown 同步 | 静态核对（本机无 Docker，构建验证列入部署清单） |
| nginx 安全头（Q-09） | nosniff / SAMEORIGIN / Referrer-Policy；CSP 模板注释 | 静态核对；启用 CSP 前需真实浏览器验证（登记） |
| manifest 对账口径修正（工具） | `e2e_bench.py`：对账范围限定为"仅消息体关键字"，改为跨运行逐规则一致性为主 Oracle | 三场景 E2E OK |

## 四、剩余项分阶段方案（全部为登记项的排期细化）

### Phase A — 发布前必做（小改动、需外部验证）

| # | 项 | 文件 | 验收标准 | 回滚边界 |
|---|---|---|---|---|
| A1 | CSP 启用 | `web/nginx.conf`（模板已注释） | 真实浏览器跑登录/作业创建/SSE/命中四屏零报错后启用；再跑 Playwright 冒烟 | 注释即回滚 |
| A2 | 真实 Docker 构建 + compose 冒烟 | `service/Dockerfile`、`web/Dockerfile`、`deploy/**` | `docker compose build` 成功；非 root 身份确认（`id -u`）；三栈 up + `/api/health` + 前端首页 | revert 配置文件 |
| A3 | 依赖 CVE | `pyproject.toml`（jose→joserfc 或 PyJWT）、`pnpm audit` 修复 | `pip-audit`/`pnpm audit` 清零或显式豁免；全量门禁绿 | 依赖 lock 回退 |

### Phase B — 排期专项（行为/契约影响面大）

| # | 项 | 内容 | 验收标准 |
|---|---|---|---|
| B1 | `matches` 分区与保留期（Q-12） | PG 按月分区 + 保留期清理任务；作业级命中上限联动 | 千万级行写入/查询基准不回退；保留期清理幂等 |
| B2 | 前端体验债（Q-11/Q-13 前端） | 窗口分页 composable、错误提取合并、规则集列表批量计数、Element Plus 按需 + bundle 预算 | 8 个视图行为不变（契约测试先行）；主包 gzip < 300KB |
| B3 | 复杂度热点拆分（Q-13） | `TaskProcessor.process`、`create_job`、9 个大 SFC | 纯行为不变重构；等价性/契约测试全绿为闸 |
| B4 | 前端组件/E2E 与 `strict: true`（Q-06） | Playwright 四屏冒烟 + 隐式 any 清理 | `strict: true` 下 typecheck 0 错误；E2E 入 CI |

### Phase C — 登记待排期

SSE 聚合/降频、作业级命中上限、`copy_to_local`/`workers.last_error` 死配置、双响应信封
统一、多行堆栈规则（M3）。

## 五、本轮验证记录

- 后端：`pytest tests -q`（含 `LOGHARVEST_TEST_PG_URL`）→ **685 passed / 1 skipped**（唯一
  skip = Windows 无 FIFO，平台限制）；`ruff check` / `format --check` 全绿；`mypy` 30 文件 0 错误。
- 前端：`pnpm test` → **102 passed**；`pnpm typecheck` 0 错误。
- E2E（真实 PG + 真实 Worker 子进程 + 双 Oracle）：S1 11.2s（命中 171,137 逐规则一致）、
  S2 53.5s（命中 1,591,672 一致）、S3 5.7s（命中 46,556 一致）。

### 有意不做（本轮）

- CSP 直接启用（需浏览器验证，A1 专项）；Compose 资源上限（需容量规划）；CVE 依赖迁移
  （A3 专项）；大函数/SFC 拆分（B3）；`matches` 分区（B1）。
