# LogHarvest 系统性质量评估报告与分阶段优化方案（第四轮，2026-11-03）

> 本文是对当前代码（`main` @ `90b5a93`）的第四轮全维度评估。前三轮报告
> （2026-10-28 两份、2026-10-29 两份）清单上的全部 P0/P1 代码缺陷均已闭环，
> 本文只记录**当前仍成立**的结论、本轮新发现与本次实施落地的分阶段方案。

- 基线实测（本轮）：后端 **665 passed / 20 skipped**（20 条为需要真实 PostgreSQL 的
  集成用例，本机未配置 `LOGHARVEST_TEST_PG_URL` 时自动跳过），语句覆盖率 **97%**
  （3,884 语句 / 114 miss）；前端 **102 passed / 11 spec**、`vue-tsc` 0 错误。
  **静态门禁非全绿**：`ruff check` 报 1 个 F401、`ruff format --check` 报 1 个文件
  （`tests/infrastructure/test_database.py`），见 F-01。
- 代码量：后端 69 个 `.py`（8,568 行），前端 141 个 ts/vue（23,880 行，其中
  `ReIcon/data.ts` 3,869 行为图标清单，不计业务代码）。
- 方法：全量门禁实测 + 九维度逐项 grep/精读（processor、task_queue、reader、
  evaluator、auth、dependencies、settings、http 封装、auth.ts、sse.ts 等核心文件
  逐行复核）+ 与第三轮报告逐条对照。

---

## 一、结论摘要

**未发现新的 P0/P1 缺陷。** 前三轮积累的分层纪律、错误分类、边界防护、安全加固
在本轮抽查中全部保持成立。本轮有 1 个新发现的过程性问题（静态门禁残留，P1）与
2 个新的代码级低危发现（P3），另确认 3 项继承登记项（token 存储、依赖 CVE、
SSE/分区性能项）不变。本次同步实施 7 个阶段的增量优化与测试补全（见 §三）。

| 维度 | 结论 | 本轮证据 |
|---|---|---|
| 代码结构与分层 | 强 | `api → application → domain ← infrastructure`、`worker → application` 方向保持；路由层无业务逻辑、`application` 零 HTTPException 引用（仅文档字符串提及） |
| 命名与可读性 | 良好 | processor.py/task_queue.py 等复杂文件的注释解释"为什么"而非"做什么"；`_open_task_predicate` 把三处必须同义的谓词收拢到常量 |
| 重复与冗余逻辑 | 后端低、前端 2 项 | F-03 错误详情提取 2 处实现（第三轮登记 ×3，`logharvest.ts` 已抽公共函数，user store 仍内联）；F-06 `listRuleSets` 魔法数字 500 |
| 错误与异常处理 | 强 | 全部 `except Exception` 均带 `noqa: BLE001` + 理由注释；processor 四类终态 + 命中上限 + 失败清场完整；无裸 except、无吞异常后状态不一致 |
| 边界与空值 | 强 | 解压预算四维、长行截断、mtime 时区、lease 栅栏保持；本轮新发现 1 处分类漂移（F-04，P3） |
| 接口契约一致性 | 良好 | 41/47 端点有 response_model；分页参数 `ge/le` 校验完整；auth/events 5 端点无 response_model（F-05，P3 登记）；前端 40 调用路径契约测试在位 |
| 安全隐患 | 明显改善、3 项继承 | 登录限流/可信代理/时序安全（UNKNOWN_USER_HASH）/非 root 容器保持；继承项：token 存储非 HttpOnly（F-07）、python-jose CVE（F-08）、CSP 待浏览器验证 |
| 性能瓶颈 | 大幅改善 | AC 预筛 9.6×、部分索引、批量插入保持；继承项：SSE 每观看者每秒 2 查询、matches 分区、前端主包体积（F-09） |
| 测试覆盖 | 后端 97%、前端纯逻辑全 | 本轮缺口定位：evaluator 10 行 / passwords 3 行 / aho 1 行纯逻辑分支未覆盖（§2.9）；前端 tree.ts、streamJobProgress 流式边界未测 |

---

## 二、九维度逐项复核（当前代码）

### 2.1 代码结构与分层

依赖方向持续成立。抽查 `src/api/v1/routes_auth.py`：路由层只做参数校验、依赖注入
与审计留痕，口令校验/令牌签发/RBAC 解析分别下沉到 domain/application。
`helpers.py` 文件头明确记录了"`_client_ip` 曾在六个路由文件各存一份"的收拢动机。

**未发现问题。**

### 2.2 命名与可读性

- `task_queue.py:266` `_open_task_predicate`：FINISH_JOB_SQL / CANDIDATES_SQL /
  `_derive_job_status` 三处"未完成任务"谓词从同一组状态常量拼出，注释写明
  "改一处漏一处会让两条 SQL 静默跑偏"——重复治理的正面样板。
- `dependencies.py` 对"TokenService 不缓存 / LoginLimiter 必须缓存"的对比注释
  准确解释了两个相反决策的依据。

**未发现问题。**

### 2.3 重复与冗余逻辑

| 编号 | 位置 | 内容 | 优先级 |
|---|---|---|---|
| F-03 | `web/src/store/modules/user.ts:25` vs `web/src/api/logharvest.ts:24` | 错误详情提取两处实现：`apiErrorDetail` 已统一处理 pydantic 校验数组，user store 仍内联 `error?.response?.data?.detail ?? error?.message`，两处口径不一致（登录 422 校验错时提示原始对象）。第三轮登记 ×3，现收敛为 ×2 | P2 |
| F-06 | `web/src/views/rules/maintenance/index.vue:67` | `listRuleSets({ limit: 500, offset: 0 })` 魔法数字 500 全量拉取（API 默认 `limit: 20`）；规则集超过 500 时下拉静默截断 | P3 |

第三轮登记的"窗口式分页 ×8、数据源只读三态 ×2"经 grep 验证**已不存在**（样式
统一提交 6781d68/6ce409f 期间随组件收敛消除），本轮销项。

### 2.4 错误与异常处理

全仓 `except Exception` 共 12 处，全部带 `noqa: BLE001` 与理由注释（审计失败不得
影响业务、Worker 循环边界兜底等），语义正确。无裸 `except:`、无 f-string SQL
（`_status_in_clause` 的插值仅来自模块内状态常量，非用户输入）、无 print、无 TODO 欠账。

| 编号 | 位置 | 内容 | 优先级 |
|---|---|---|---|
| F-04 | `service/src/application/worker/processor.py:444,446` | `_copy_to_local` 的 `source.stat()` / `source.open("rb")` 抛出的 OSError 落入 `process()` 兜底 `except Exception` → 一律 `retryable=True`。而文件头注释明确"ENOENT/EACCES 是对源已变化的确定性判断，重试只会同样失败"（`_check_source` 阶段 ENOENT 判 permanent）。影响范围：`_check_source` 通过后、复制开始前源文件消失/被换的竞态窗口（大目录扫描与执行间隔越长窗口越大），后果是多烧一次重试尝试 + 多一条 attempt 历史，**最终终态仍正确**（下次领取时 `_check_source` 立即判 permanent） | P3 |

### 2.5 边界与空值

- 解压预算四维（成员/累计/成员数/膨胀比）、4096 行截断、mtime naive 时区钉扎、
  lease 栅栏三处复查（`touch`/`heartbeat`/`complete` 全部带 `attempts` + `status`
  双重 fence）——第三轮结论保持。
- `settings.py` 密钥强度/管理员口令/代理 CIDR 三组校验器保持"拼错即拒启"语义。

**本轮未发现新问题**（F-04 属错误分类而非空值边界，列在 2.4）。

### 2.6 接口契约一致性

- 端点级：13 个路由文件 47 端点，41 个有 `response_model`；分页/数量参数全部带
  `Query(ge=…, le=…)` 上下界（如 `routes_matches.py:28 limit le=500`）。
- 前端侧：`web/src/api/*.ts` 手写类型 + `tests/api/contract.spec.ts`、
  `rbac-contract.spec.ts`、`errors.spec.ts` 三套契约测试在位，本轮抽查
  `logharvestTypes.ts` 注释明确记录了"裸数组无 total、错误体 `{detail}`"的契约口径。

| 编号 | 位置 | 内容 | 优先级 |
|---|---|---|---|
| F-05 | `service/src/api/v1/routes_auth.py`（4 端点）、`routes_events.py`（1 端点） | 信封式响应（`{code,message,data}`）无 response_model，OpenAPI 对这 5 个端点无 schema，契约靠 `ok()` 约定 | P3（登记，不实施：为文档收益改动响应模型过滤行为的收益/风险比不划算） |

### 2.7 安全隐患

保持项（本轮验证仍在位）：登录失败等成本 bcrypt（时序安全）、限流先于任何 bcrypt
执行、可信代理 CIDR 默认不信任、失败留痕不含口令、账号停用不计入限流失败、
nginx 非 root + 安全头、容器 uid 10001。

| 编号 | 内容 | 状态 | 优先级 |
|---|---|---|---|
| F-07 | token 存非 HttpOnly cookie + localStorage；CSP 模板就绪待浏览器验证（第三轮 Q-06） | 继承登记，需专项（改 HttpOnly 涉及无感刷新方案重设计） | P2 |
| F-08 | `python-jose[cryptography]>=3.3.0`（含 ecdsa 传递依赖）仍有公开 CVE（第三轮 Q-07） | 继承登记，迁移 PyJWT/joserfc 需专项 | P2 |
| — | v-html / console.log / 敏感信息入日志 | grep 全仓 0 命中 | — |

### 2.8 性能瓶颈

保持项：5k 规则 AC 预筛 + 编译缓存（9.6×）、0006 部分索引、`executemany` 批量插入、
SSE 短会话轮询（不占长连接）。

| 编号 | 内容 | 状态 | 优先级 |
|---|---|---|---|
| F-09 | ① SSE 每观看者每秒 2 条查询（job + 聚合）；② `matches` 千万级命中无分区/保留期；③ 前端主包 `chunkSizeWarningLimit: 4000` 偏大（Q-11/Q-12） | 继承登记，①②需与容量模型一起排期，③需构建体积基线 | P2 |
| — | `task_queue.py` 单元覆盖 77% | 核心领取/完成 SQL 需要 PG 方言，由 20 条真实 PG 集成用例覆盖；无 PG 时跳过属既定分层，不视为缺陷 | 登记 |

### 2.9 测试覆盖

- 后端 665 passed / 20 skipped；覆盖率 97%（114 miss）。本轮逐行定位高价值缺口：
  - `domain/rules/evaluator.py`：170-171（`\Q` 未闭合）、186（`[` 未闭合）、
    234-236（`{` 未闭合字面量）、285-287（空词表回退 CompiledRule）、335（空词表
    关键字规则进 always）、421（大小写敏感 haystack 分支）——**10 行纯逻辑分支**；
  - `domain/services/passwords.py`：24（空 hash）、27-28（畸形 hash 的 ValueError）；
  - `domain/rules/aho.py`：68（未 build 自动机 `find_all`）；
  - `worker/main.py` 5 行、`worker/metrics.py` 2 行（运行时防御分支）；
  - 其余为 `__main__.py` 入口胶水与 PG 专属 SQL（集成用例覆盖）。
- 前端 102 passed：apiErrorDetail/apiErrorStatus、sse 帧解析与 `streamJobProgress`
  流式边界（非 2xx/断流/尾部残留）、格式化、筛选、rule-form、auth、pendingRequests
  均已覆盖。缺口：
  - `utils/tree.ts`（extractPathList/deleteChildren/buildHierarchyTree 等 8 个纯函数，
    菜单/权限树核心，仓库内 0 条测试）；
  - api 模块的默认分页参数契约（`listRuleSets` limit 20 / `listDataSources` limit 500 /
    `listJobs` 空）无断言保护；
  - `streamJobProgress` 的两处小缺口（body 为 null 的防御分支、Authorization 头形态）。

| 编号 | 内容 | 优先级 |
|---|---|---|
| F-01（流程） | **静态门禁红**：`tests/infrastructure/test_database.py:2` 未使用导入（F401）+ `:30` 格式漂移。说明 1786596（PG18 连接池）提交未执行完整门禁——正是 agent.md §一要防的破窗。本轮阶段 1 立即修复，并把"门禁未跑"作为流程教训记入 agent.md 核验 | **P1** |
| F-02 | mypy 范围仅 30/69 文件（domain/worker/application.worker/config），application/services、api、infrastructure 无类型检查 | P2（阶段 6 评估扩围） |
| F-10 | 上述前端/后端测试缺口 | P2（阶段 3/5 补全） |

---

## 三、分阶段优化方案（本次实施）

原则：每阶段独立提交、独立验收、可单独回滚（单提交 revert）；重构类提交以
"既有断言零修改 + 全量测试绿"证明行为不变；行为修复类提交必须先有失败用例。

### 阶段 1：恢复静态门禁全绿（F-01，P1）

- **目标**：`ruff check` / `ruff format --check` / `mypy` 全绿。
- **涉及**：`service/tests/infrastructure/test_database.py`（删除未使用导入、修格式）。
- **验收**：后端三静态门禁 + pytest 全绿；`git diff` 仅该文件。
- **回滚**：revert 单提交即可。

### 阶段 2：Worker 复制阶段 OSError 分类修复（F-04，P3 修复，测试先行）

- **目标**：复制阶段（`_copy_to_local`/`_copy_chunks`）的 OSError 按 errno 分类，
  ENOENT/EACCES 等判 permanent、瞬时 errno 判 retryable，与 `_check_source` 阶段
  同口径；**不改变**其他异常的兜底行为。
- **涉及**：`service/src/application/worker/processor.py`（仅 `_copy_to_local`/
  `_copy_chunks` 内 stat/open 包一层 `_source_unavailable_error`）；
  `service/tests/application/worker/test_processor.py` 新增 2 条用例
  （复制前源消失→permanent 一次终态；复制中瞬时 EIO→retryable）。
- **验收**：新用例先红后绿；全量 pytest 绿且**既有断言零修改**；ruff/mypy 绿。
- **回滚**：revert 单提交（实现 + 测试同回滚）。

### 阶段 3：后端单元测试补全（F-10 后端部分）

- **目标**：覆盖 §2.9 定位的高价值纯逻辑缺口，覆盖率 97% → ≥98%。
- **涉及**（全部纯新增测试文件/用例，产品代码零改动）：
  - `tests/domain/rules/test_evaluator_edges.py` 扩：`\Q` 未闭合、`[` 未闭合、
    `{` 未闭合、空词表回退、大小写敏感分支；
  - `tests/domain/services/test_passwords.py` 扩：空 hash、畸形 hash；
  - `tests/domain/rules/test_aho.py`（新）：未 build `find_all` 空集、针编号集合；
  - `tests/worker/test_metrics.py` 扩：采集异常兜底。
- **验收**：新增用例全绿；覆盖率 ≥98%；测试可重复（无顺序/时间/网络依赖，每用例自建数据）。
- **回滚**：纯测试新增，revert 不影响产品代码。

### 阶段 4：前端重复收敛（F-03 + F-06，重构）

- **目标**：错误详情提取收敛到单一实现；消除 500 魔法数字。
- **涉及**：
  - 新增 `web/src/api/errors.ts`（纯函数，无依赖，规避 store↔api 环形引用）：
    `apiErrorDetail`/`apiErrorStatus` 从 `api/logharvest.ts` 迁入；
  - `web/src/api/logharvest.ts` 改为 re-export（调用方 import 路径不变）；
  - `web/src/store/modules/user.ts` 登录错误提示改用 `apiErrorDetail`（口径统一后
    登录 422 校验错误从"对象"变为"可读文案"——行为增强，属修复目标本身）；
  - `web/src/views/rules/maintenance/index.vue`：500 提取为命名常量 `ALL_RULE_SETS_LIMIT`
    并注明截断风险。
- **验收**：vitest 全绿且 `tests/api/errors.spec.ts` **既有断言零修改**（证明
  re-export 行为不变）；typecheck 绿；grep 确认错误提取只剩 1 处实现。
- **回滚**：revert 单提交，恢复两处实现。

### 阶段 5：前端单元测试补全（F-10 前端部分）

- **目标**：覆盖 §2.9 前端缺口，用例数 102 → 132。
- **涉及**（纯新增，全部 mock、无真实网络/定时器）：
  - `tests/utils/tree.spec.ts`（新，20 条）：tree.ts 8 个函数的正常/防御/空树分支；
    特征化锁定上游 pure-admin 的实际语义（`extractPathList` 递归返回值被丢弃、
    `deleteChildren` 的 id 为兄弟数组下标），仓库内这两个函数无调用方；
  - `tests/api/params.spec.ts`（新，6 条）：listRuleSets/listDataSources/listJobs/
    listJobFiles/listWorkers/listRuleIndex 的默认分页参数与显式参数下推契约；
  - `tests/api/errors.spec.ts` 扩（2 条）：re-export 入口与 `@/api/errors` 同一实现
    （`toBe` 同引用）、responseDetail 三种形态；
  - `tests/utils/sse.spec.ts` 扩（2 条）：body 为 null 的防御分支、Bearer 头与
    URL 形态（token 不进 query）。
- **验收**：vitest 132 全绿、可重复；typecheck 绿。
- **回滚**：纯测试新增。

### 阶段 6：mypy 扩围评估（F-02，P2）

- **目标**：评估 `src/application/services`、`src/api`、`src/infrastructure` 加入
  mypy 的成本；若错误面小则一并修复注解，否则登记后续专项。
- **实测结论（本轮）**：对未覆盖三层直接运行 mypy，报 **100 处错误（11 文件）**，
  绝大多数是 SQLAlchemy/SQLModel 的严格列类型（`int | None` 主键传入
  `func.count`、`order_by(int)` 等），逐处修复需要先给 35 个文件补齐注解，
  属专项排期——**本轮登记不实施**，mypy 范围保持 30 文件不变。
- **验收（登记口径）**：扩围建议与错误规模记录在本报告，避免后续重复评估。
- **回滚**：不涉及代码变更。

### 阶段 7：agent.md 协作规范核验

- **目标**：确认现有规范与本次要求逐条对应（每次交互必跑前后端单测、全绿才可
  提交、失败先修复再重跑）；补记本轮发现的过程破窗（阶段 1 的门禁残留）作为
  执行证据示例；核对命令与目录结构仍与仓库一致。
- **验收**：文档与仓库现状一致，无过期命令。
- **回滚**：文档变更，revert 无风险。

### 提交拆分（每步提交前执行完整门禁）

1. `docs: 第四轮质量评估报告与分阶段优化方案`
2. `test(service): 修复 test_database 静态门禁残留`
3. `fix(worker): 复制阶段 OSError 按 errno 分类，与检查阶段同口径`（含新用例）
4. `test(service): 补齐 evaluator/passwords/aho/metrics 纯逻辑分支用例`
5. `refactor(web): 错误详情提取收敛到 api/errors.ts，规则集全量上限命名化`
6. `test(web): 补齐 tree/api 参数契约/sse 边界用例`
7. `docs(agent): 协作规范核验与执行证据补记`

（mypy 扩围经阶段 6 评估后登记为专项，不在本轮提交。）

---

## 四、实施结果（2026-11-03 当轮闭环）

| 阶段 | 结果 | 证据 |
|---|---|---|
| 1 门禁修复 | ✅ | `ruff check`/`format --check`/`mypy` 全绿；`git diff` 仅 `test_database.py` |
| 2 OSError 分类 | ✅ | 2 条新用例先红后绿；processor 49 用例全绿、既有断言零修改 |
| 3 后端补测 | ✅ | 665 → 682 passed；evaluator/aho/passwords/metrics 达 100%（原 97/98/84/88%） |
| 4 前端收敛 | ✅ | `detail` 契约读取点唯一化（grep 仅 `api/errors.ts` 一处实现）；102 全绿、既有断言零修改 |
| 5 前端补测 | ✅ | 102 → 132 passed（tree 20 + params 6 + errors 2 + sse 2 + …）；typecheck 0 错误 |
| 6 mypy 扩围 | ⬜ 登记 | 未覆盖三层实测 100 处标注债（11 文件），登记专项 |
| 7 agent.md | ✅ | 规范逐条核对与本次要求一致；补记本轮 F-01 破窗作为执行证据 |

最终门禁（全部提交前复跑）：后端 682 passed / 20 skipped（PG 集成需真实 PG）+
ruff + format + mypy 全绿；前端 132 passed + vue-tsc 0 错误。
