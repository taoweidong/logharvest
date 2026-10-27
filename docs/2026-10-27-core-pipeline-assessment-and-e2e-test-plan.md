# LogHarvest 核心链路评估与全流程真实测试方案

- 日期：2026-10-27
- 范围：**任务下发 → Worker 领取/执行 → 规则解析/入库** 的后端核心业务；不含前端、RBAC 页面、SSE 展示细节与操作日志/分析页
- 代码基线：`git HEAD = 6ef960a`
- 真实数据：`data/logs/`，50 个 ZIP、665 个成员、**49,736,840 行**、压缩后 **1.04 GB**、解压后 **6.84 GB**
- 结论先说：**单机、无故障、规则集不过宽时的正向链路是正确的**（真实数据行数/成员数/规则计数都能对上）；但**故障恢复、结果一致性、规模容量和可解释性**存在 3 个 P0、8 个 P1，当前版本不适合直接按“500 GB / 1000+ 压缩包 / 千万级命中”的生产口径上线。

> 本文的“最新代码”指审计时的工作区。审计期间工作区还包含未提交的 `operation_logs` / analytics 改动；它对核心链路无影响，但会让全量测试出现 2 条与表集合断言相关的失败，见 §0.2。所有数字、代码位置都以审计时点为准。
>
> 与 [`2026-10-27-implementation-audit-and-optimization.md`](2026-10-27-implementation-audit-and-optimization.md) 的关系：上一轮已登记 D6（分区/保留期）、D8（`max_matches_per_task` 死配置）、D10（`copy_to_local` 不生效）、D11（`workers.last_error` 无写入）；本文不重复 RBAC/文档审计，而是对核心链路做真实数据量化，并把 D8 升为 **P0-3**、把 D6 纳入 **P0-3/P2-13**。

---

## 0. 审计基线与方法

### 0.1 本次实际检查/实验的东西

| 类型 | 内容 |
|---|---|
| 静态代码审查 | `application/services/jobs.py`、`infrastructure/task_queue.py`、`application/worker/processor.py`、`worker/main.py`、`domain/rules/{models,evaluator}.py`、`domain/archive/reader.py`、`infrastructure/match_writer.py`、`domain/models/entities.py`、`application/services/{queries,rule_sets}.py`、API 路由与 schemas |
| 真实数据对账 | 用 `data/logs/APPLOG001.ZIP` 全量扫描，与 `manifest.json` 逐关键字比对；再用独立的 `zipfile + re/in` oracle 扫全量 50 个包 |
| 规则引擎交叉验证 | 同一份 15 条规则，分别走服务端 `compile_ruleset/evaluate_line` 和独立 oracle，逐规则比对 |
| 边界行为实验 | 全 partial 作业状态、损坏容器重试标记、重试清场顺序、空关键字、空 glob、BOM、行号与成员路径 |
| 性能采样 | 单进程 reader+evaluator 吞吐；全量 oracle 吞吐 |
| 未执行 | 真实 PostgreSQL 上的队列/领取/恢复全链路（本机 WSL 处于停止状态，未启动服务）；该项由§5 的真实 PG 测试方案覆盖，现有 `tests/integration/test_postgres_queue.py` 也依赖 `LOGHARVEST_TEST_PG_URL`，本次运行中被跳过 |

### 0.2 测试基线（审计时点）

| 命令 | 结果 |
|---|---|
| `uv run --python 3.12 pytest tests/domain/rules tests/application/worker tests/infrastructure/test_job_finalization.py tests/infrastructure/test_task_queue.py tests/api/test_jobs.py -q` | **132 passed / 25.03s** |
| `uv run --python 3.12 pytest -q`（全量） | **2 failed / 409 passed / 19 skipped / 84.48s** |
| 失败点 | `tests/domain/models/test_entities.py::test_create_db_and_tables_creates_every_core_table` 与 `...uses_module_engine_by_default`：工作区新增的 `operation_logs` 表没有加入 `CORE_TABLES` |

这不是核心链路缺陷，但它是**测试前置条件**：全流程测试开始前，必须先让全量基线转绿，否则“失败”无法归因。审计时未改动工作区代码，只新增本文档。工作区在审计后仍在并行开发（RBAC/审计/分析页等未提交改动），执行测试前请以最新的 `git status` 与 `pytest` 结果为准，并重新核对本文的代码行号。

### 0.3 严重度定义

| 级别 | 含义 | 处理时点 |
|---|---|---|
| **P0** | 在正常故障条件或正常配置下会产生**错误结果、不可恢复的失败、或数据库/磁盘被耗尽** | 上 UAT / 全量跑批前必须修复 |
| **P1** | 明确偏离设计、在规模或故障场景下显著劣化，或缺少可解释/可恢复能力 | 上生产前修复 |
| **P2** | 可维护性、性能优化、文档/数据夹具问题 | 排期处理 |

### 0.4 核心风险速览（TL;DR）

| ID | 问题 | 一句话影响 |
|---|---|---|
| P0-1 | 瞬时源/挂载错误被判永久失败 | NAS 抖动变成一批“假失败” |
| P0-2 | 清场只在下一次尝试开头，失败任务留旧命中 | 失败任务仍能查到旧/半截结果 |
| P0-3 | 无 `max_matches_per_task`，空关键字/空正则可匹配所有行 | 一条误配规则可把 `matches` 写爆 |
| P1-1 | 全部任务 partial 的作业汇总成 failed | 作业终态错误，部分成功被吞 |
| P1-2 | 确定性容器格式损坏仍自动重试 5 次 | 每个坏包空烧约 50 秒 |
| P1-3 | 复制阶段不检查取消，无单任务 deadline | 取消不生效，任务可挂数小时 |
| P1-4 | 队列无 `(status, scheduled_at)` 等索引 | 20k 任务领取退化为反复排序 |
| P1-5 | 只有入库时间，没有日志事件时间 | 延迟跑批的时间轴分析错位 |
| P1-6 | 没有“是否保存原始行”的输出控制 | 7.5M 命中约 1 GB raw，500 GB 规模约 75 GB+ |
| P1-7 | 正则超时静默跳过且不计数 | 漏命中仍可能 succeeded |
| P1-8 | 规划失败不落 job；stat 在 10s deadline 之外 | 失败无审计，单文件 stat 失败拖垮整批 |

---

## 1. 核心链路与不变量

### 1.1 任务下发

```text
POST /api/jobs
  → 校验数据源/规则集/路径/只读守卫
  → discover_archives（递归、后缀、深度、max_files、10s deadline）
  → 对发现文件做第二次 stat
  → insert jobs + 每个压缩文件一条 file_tasks（同一事务）
  → 返回 job；Worker 之后用 SKIP LOCKED 主动领取
```

关键代码：`application/services/jobs.py:79-184`，`domain/archive/reader.py:161-219`，`infrastructure/task_queue.py:16-38`。

### 1.2 Worker 领取与执行

```text
register_worker → drain 检查 → claim_next（CLAIM + 规则快照 + attempt 写入，同一事务）
  → guard_source / _check_root / _check_source / _verify_lease
  → attempts > 1 时 delete_for_task
  → 复制到本地临时盘（分块 touch 续租）
  → 流式解压 + 逐行匹配 + 批量写 matches
  → complete（attempts 栅栏）→ finalize_job
  → 失败：重排 pending 或终态 failed；Worker 崩溃：120s 后 recover_stale
```

关键代码：`worker/main.py:96-191`、`application/worker/processor.py:171-367`、`infrastructure/task_queue.py:91-189`。

### 1.3 规则解析

```text
RuleSetConfig（版本不可变）
  → compile_ruleset：关键字规则预编译、正则编译、threshold 字段必须是命名分组
  → 每行：keyword any/all → regex search(timeout) → named groups → Decimal 门限
  → 命中生成 MatchRecord（rule_id/severity/tags/fields/numeric_value/raw_line/line_no）
```

关键代码：`domain/rules/models.py:1-76`、`domain/rules/evaluator.py:60-163`、`application/worker/processor.py:201-270`。

### 1.4 必须始终成立的 7 条不变量

1. **规则版本固定**：作业创建时绑定具体 `rule_set_id`，运行中新增版本不影响该作业。
2. **NAS 只读**：扫描、复制、解压全程不写源目录。
3. **任务恰好执行一次结果**：同一 `file_task` 的 `matches` 不因重试/恢复产生重复或旧版本残留。
4. **租约栅栏**：旧 Worker 在失去租约后不能写 `matches` 或覆盖终态。
5. **终态正确**：作业状态必须由全部文件任务状态正确汇总，`partial` 不能被吞成 `failed`/`succeeded`。
6. **故障可恢复**：瞬时故障可重试；确定性内容错误不空烧重试；Worker 死亡可被回收。
7. **结果可解释**：每条命中能追溯到压缩包、成员、物理行号、规则版本、原始行；时间维度必须能按“日志事件时间”而非仅“入库时间”分析。

---

## 2. 已验证正确的部分（正向证据）

这一节很重要：核心链路的**正常路径**不是坏的。下面的证据说明问题集中在边界、故障和规模，而不是“规则引擎完全不能用”。

### 2.1 真实数据行数/成员数/关键字完全对账

对 `data/logs/APPLOG001.ZIP` 用服务端 `scan_archive` 全量扫描：

| 指标 | manifest | 实际 | 结果 |
|---|---:|---:|---|
| 行数 | 709,815 | 709,815 | ✅ |
| 成员数 | 11 | 11 | ✅ |
| `ERROR` 关键字 | 32,119 | 32,119 | ✅ |
| `FATAL` | 5,112 | 5,112 | ✅ |
| `connection refused` | 7,373 | 7,373 | ✅ |
| `timeout` | 4,597 | 4,597 | ✅ |
| `segmentation fault` | 965 | 965 | ✅ |
| `out of memory` | 1,370 | 1,370 | ✅ |
| `disk full` | 886 | 886 | ✅ |
| `core dumped` | 951 | 951 | ✅ |
| `auth failed` | 8,581 | **4,859** | ❌ 见 §3.3 P2-8 |

除 `auth failed` 外，行级读取、成员遍历、关键字匹配和 manifest 完全一致。`auth failed` 的差异不是引擎漏匹配，而是生成器 manifest 的期望值错误：`op()` / `audit()` 会把 `auth failed` 计入 manifest，但消息体里并没有这个词（`data/generate_mock_logs.py:234-237, 268-279`）。

### 2.2 规则引擎与独立 oracle 完全对账

在 `APPLOG001.ZIP` 上，用同一份 15 条规则分别执行：

- 服务端：`compile_ruleset` + `evaluate_line`（`regex` 模块、Decimal 门限、命名分组）
- 独立 oracle：`zipfile` + 标准库 `re` + 字符串包含

15 条规则逐条命中数**全部一致**，总计 104,666 条命中；扫描 709,815 行耗时 7.44s（约 95k 行/s，单进程，含解压/解码/匹配）。

这说明：**关键字、正则命名分组、Decimal 门限、行号/成员路径、批内多条规则语义在当前实现下是正确的**。

### 2.3 全量 50 个包的独立期望值

用独立 oracle 扫描 `data/logs` 全部 50 个 ZIP（耗时 218s，4 线程；仅作期望值生成，不是服务端性能）：

| 指标 | 数值 |
|---|---:|
| 压缩包 | 50 |
| 成员 | 665 |
| 总行数 | **49,736,840** |
| 压缩包总字节 | **1,043,580,887** |
| 解压后总字节 | **6,841,517,122**（6.84 GB） |
| 平均压缩比 | 6.56 |
| 平均行字节 | 137.55 |

RS-B（15 条规则，见 §5.4）的全量期望命中：

| rule_id | 期望命中 |
|---|---:|
| `level_error` | 2,325,800 |
| `level_fatal` | 382,186 |
| `conn_refused` | 532,770 |
| `timeout` | 272,489 |
| `auth_failed` | **272,329**（不要用 manifest 的 703,072） |
| `oom` | 99,064 |
| `segv` | 55,811 |
| `disk_full` | 55,628 |
| `core_dumped` | 56,055 |
| `op_failed` | 554,470 |
| `audit_fail` | 583,726 |
| `slow_cost_gt5000` | 1,480,277 |
| `retry_gte5` | 388,889 |
| `usage_gte98` | 182,494 |
| `http_5xx` | 272,083 |
| **合计** | **7,514,071** |

命中率 **15.1%**。按 500 GB / 6.84 GB 约 73 倍外推，同一规则集会产出约 **5.5 亿行** `matches`——这直接对应 §3.1 P0-3（无命中上限、无分区/保留期）。

### 2.4 现有测试覆盖

核心相关 132 条用例全绿，覆盖：

- `test_processor.py`：解压、成员级 partial、编码、长行截断、租约丢失、复制续租、取消检查、永久失败、解压预算、`file_glob` 按成员只算一次
- `test_evaluator.py`：关键字 any/all/大小写、正则命名分组、5 种门限、缺失/非数值字段、超时跳过、非法 flags/规则 ID
- `test_job_finalization.py`：全成功、成功+失败、并发收尾、取消、空作业、pending 阻塞
- `test_task_queue.py`：SQLite 可移植部分（收尾、补偿）
- `test_jobs.py`：创建、幂等、路径逃逸、空目录、取消、重试
- `tests/integration/test_postgres_queue.py`：真实 PG 的领取/租约/恢复/尝试历史（本次未设置 `LOGHARVEST_TEST_PG_URL`，被跳过）

**但现有用例没有覆盖**：全 partial 作业、瞬时 `OSError` 分类、重试清场早于源校验、容器损坏的 retry 标记、取消发生在复制阶段、每任务命中上限、事件时间、队列索引计划。这些正是本文 P0/P1 的来源。

---

## 3. 问题清单

### 3.1 P0（全量跑批前必须修）

#### P0-1 源文件/挂载的瞬时错误被当成永久失败

**代码位置**：

- `application/worker/processor.py:112-131`：`_check_root` 中 `resolve()` 的 `OSError` → `PermanentTaskFailure`
- `application/worker/processor.py:133-156`：`_check_source` 中 `source.stat()` 的**任何** `OSError` → `PermanentTaskFailure`
- `application/worker/processor.py:276-286`：`PermanentTaskFailure` 以 `retryable=False` 写终态，不自动重试
- `infrastructure/task_queue.py:16-38`：领取时不检查数据源挂载是否健康、Worker 是否真的能访问该数据源

**场景**：

- NAS / SMB / NFS 抖动，`stat()` 返回 `EIO`、`ESTALE`、`ETIMEDOUT`、`ENOTCONN`；
- Windows/SMB 上文件被短暂占用，返回共享冲突；
- 挂载点暂时不可见，`source.stat()` 返回 `ENOENT`。

代码把上述所有情况都归为“源文件不存在/不可读”，直接落 `failed` 且 **不自动重试**。4 个 Worker 在 1 分钟挂载抖动里可能永久失败多条任务；1000+ 任务批次可能出现一批“假失败”，只能靠人工重试。这与设计 §7.3 的“挂载盘读取超时、文件被短暂占用 → 可重试”直接冲突。

**为什么是 P0**：这不是性能问题，而是**把可恢复故障变成不可恢复失败**；生产 NAS 抖动是常态，且批量越大暴露越明显。

**修复方向**：

1. 新增错误分类：`TransientSourceError` / `PermanentTaskFailure` / `ContentError`，按 errno 与数据源根健康度判定；
2. `PermanentTaskFailure` 只保留：路径越界、规划后 size/mtime 确实变化、**连续 N 次仍不可读（含权限拒绝）**、格式确定损坏；
3. 在 Worker 领取前做数据源根可达性探针（`stat(root)` + `is_dir`）；根不可达时 Worker 进入 `blocked`/退避，不领取，不把任务写死；
4. 对“根可达但单文件 ENOENT”设置有限次“源缺失重试”；超过次数再永久失败，并把 `workers.last_error` / `file_tasks.error` 写清楚；
5. 增加 `source_error_retry_limit`（建议 3–5 次）与 Failed 前的最后尝试记录。

**验收用例（当前应红，修复后绿）**：

- 模拟 `source.stat` 抛 `OSError(ESTALE)`：任务回到 `pending`、`attempts+1`，下一轮同一 Worker/其他 Worker 可重试；不能写 `failed`。
- 1000 个任务运行中卸载再挂载数据源 60s：没有任务落永久 `failed`；挂载恢复后全部完成，命中数等于 oracle。
- NAS 只读源根不可达时，Worker 日志有明确 `source_unavailable` 结构化事件，管理页可看到原因。

#### P0-2 清场只发生在下一次尝试开头，失败任务会留下旧命中

**代码位置**：

- `application/worker/processor.py:185-196`：顺序是 `guard_source` → `_check_root` → `_check_source` → `_verify_lease` → **然后** `if attempts > 1: delete_for_task`
- `application/worker/processor.py:276-298`：`PermanentTaskFailure` / `SourceNotReadOnly` 直接 `complete(..., retryable=False)`
- `worker/main.py:149-161`：规则编译失败也在 `TaskProcessor` 之外直接 `complete failed`，不会清旧命中
- `application/worker/processor.py:303-312`：普通异常同样走 `failed`，该次尝试已写入的 batch 也不会清
- `infrastructure/task_queue.py:152-189`：`RECOVER_STALE_SQL` 在 attempts 耗尽时直接把任务置 `failed`，不清理该任务已有命中

**场景 A（重试后永久失败）**：

1. 第 1 次尝试已写入 500 条 `matches`；
2. Worker 崩溃或数据库短暂失败，`COMPLETE_SQL` 把任务重排为 `pending`；
3. 第 2 次尝试先做 `_check_source`，发现源文件 mtime/size 变了（或挂载根不可达、路径越界）→ `PermanentTaskFailure`；
4. `delete_for_task` 没有执行，任务 `failed`，但 **500 条上一轮命中仍然留在 `matches` 里**，并且可能来自旧文件内容。

**场景 B（最后一次尝试失败）**：

- 第 N 次（最后一次）尝试写入了部分 batch，随后通用异常/被 kill 回收导致 `failed`；
- 没有下一次尝试来触发 `delete_for_task`，这些部分命中永久保留，任务却是 `failed`。

这直接违反设计 §7.3 的“重试清场：Worker 领取后若 attempts > 1，先按 file_task_id 删除该任务之前写入的 matches，再重新处理”。当前只在“通过了所有源校验且还有下一次尝试”的路径上清场。

**为什么是 P0**：结果是**错误数据可见**；如果作业失败后不再重试，脏命中会永久保留，后续按 job/rule 查询会得到旧源或不完整的结果。更糟的是永久失败路径会用 `processed_lines=0, matched_rows=0` 覆盖任务进度，而旧 `matches` 仍然存在——任务计数和结果表会自相矛盾。

**修复方向**：

- 首选：在领取事务里（`attempts+1` 同事务）删除该任务旧 `matches`；失败则整个领取回滚，任务保持 pending；
- 或者在 `_verify_lease` 之后、任何源校验之前清场（源校验失败也要清）；
- 在 `failed` 终态写回前删除该任务当前尝试的 `matches`；`partial` 保留，但查询/导出必须能按 `file_tasks.status` 过滤或标记；
- 更强的方案：`matches` 增加 `attempt_no`/`generation` + `task_status`，查询只展示 `succeeded`/`partial` 的行，旧代数据不可见；这样清场可以异步；
- 对规则编译失败、processor 构造失败等“还没进入处理就失败”的路径也要清。

**验收用例（当前应红）**：

- 第 1 次尝试写入 batch 后抛异常 → 任务 pending；
- 第 2 次尝试把源文件 size 改掉 → 任务 failed；
- 断言 `matches where file_task_id=...` 为 0；当前会留下第 1 次的记录。
- 第 N 次（最后一次）尝试写入 batch 后抛异常 → 任务 failed；断言 `matches` 为 0（或按状态过滤不可见）。
- 再走一次手动重试成功 → 该任务命中数等于 oracle，**无重复**（不是“恰好两倍”）。

#### P0-3 没有单任务命中上限，且允许“匹配所有行”的规则

**代码位置**：

- `config/settings.py:31`：`max_matches_per_task: int = 200_000`，全仓库零引用（`grep` 只有定义）
- `application/worker/processor.py:242-247`：命中直接 append，buffer 满就写，没有上限判断
- `domain/rules/models.py:7-17`：`KeywordMatch.any` 允许空字符串；`RegexMatch.pattern` 允许空串
- `domain/models/entities.py:119-161`：`matches` 无按月分区、无保留期；`raw_line` 非空且总是写入

**实测边界**：

- `KeywordMatch(any=[""])` 对空行和任意行都命中（已验证）；
- 空正则 `""` 也会命中所有行；
- RS-B 在 6.84 GB / 4974 万行数据上已经产生 **7,514,071 行**命中；按 500 GB 外推约 **5.5 亿行**；
- 如果规则误配为 `any=[""]`，单个 50 包作业会在 `matches` 里写入 4974 万行，按每行约 140 字节 + 3 个索引 + JSONB，轻松吃掉几十 GB，且没有分区/保留期可以按批删除。

**为什么是 P0**：这是**单条规则配置就能把数据库/临时盘写满**的容量炸弹；设计 §8/§11 明确要求 `max_matches_per_task`，当前是死配置。

**修复方向**：

1. 配置校验拒绝空关键字、全空白关键字、空正则；对 `any`/`all` 的每一项做非空 trim 校验；
2. `TaskProcessor` 接 `max_matches_per_task`；达到上限时停止扫描，任务落 `failed`（或 `partial`，但必须带 `match limit exceeded` 错误），绝不能写 `succeeded`；
3. 增加作业级命中上限/采样回落（例如按规则保留前 N 条 + 计数），避免单个作业拖垮库；
4. `matches` 按月分区 + 保留期（设计 §8.3 已要求），至少先加 `created_at` 分区/归档策略；
5. 对 `raw_line` 长度/总量做预算，配合 P1-6 的输出控制。

**验收用例（当前应红）**：

- 规则集 `any=[""]` 在 1 个包上运行，`max_matches_per_task=1000`：任务在 1000 行处停止，状态 `failed`/`partial`，错误包含 `match limit`，Worker 不崩、不继续写；
- 50 包作业完成后 `matches` 行数不超过上限 × 任务数；数据库磁盘增长可预测；
- 空关键字规则集在 `POST /api/rule-sets` 返回 422，不入库。

### 3.2 P1（上生产前必须修）

#### P1-1 全部任务 partial 的作业被汇总成 failed

**代码位置**：`infrastructure/task_queue.py:307-335`，`_derive_job_status`：

```python
failed = sum(counts.get(status, 0) for status in ("failed", "partial"))
if failed and succeeded:
    return "partial"
if failed:
    return "failed"
```

**实测**：`_derive_job_status([{"partial": 5}])` 返回 `"failed"`。

**影响**：如果 50 个压缩包每个都有一个损坏成员、其余成员成功，每个 `file_task` 都是 `partial`，作业最终显示 **failed**——这会误导用户以为整批失败，且 `partial` 的语义在作业层丢失。设计与生成器都强调成员级 partial 是“部分成功”的终态。

**修复**：按下面的矩阵重写（建议）：

| 文件任务情况 | 作业状态 |
|---|---|
| 全部 succeeded | succeeded |
| 有任意 partial、无 cancel | **partial**（包括全部 partial） |
| 无 partial，有 succeeded + failed | partial |
| 全部 failed（无 succeeded/partial） | failed |
| cancel_requested 或全部 cancelled | cancelled |
| 仍有 pending/running（未取消） | 保持 running |

同步更新 `service/README.md` 的作业终态表（当前文档也写着“无成功且存在 failed → failed”，会固化这个错误）。

**验收**：全部任务 partial → 作业 partial；partial + succeeded → partial；partial + failed → partial；全部 failed → failed；取消仍优先 cancelled。

#### P1-2 确定性的容器格式损坏被自动重试 5 次

**代码位置**：

- `domain/archive/reader.py:315-316`（zip）、`:392-397`（tar）、`:445-450`（gzip/bz2/xz）抛 `ArchiveFormatError`
- `application/worker/processor.py:303-312`：通用 `except Exception` → `complete(..., "failed", retryable=True)`（默认值）
- `infrastructure/task_queue.py:91-138`：`will_retry` 为真时回 `pending`，退避 `attempts * 5` 秒

**实测**：把 `b"not a zip"` 交给 `TaskProcessor`，`complete` 的 `retryable` 为 **True**。

**影响**：损坏的压缩包是确定性错误，重试只会得到同样结果。每次失败要等 5/10/15/20 秒退避，5 次约 50 秒；100 个损坏包会浪费数小时和大量无意义尝试记录。设计 §7.3 明确把“格式损坏”列为永久失败。

同理，`worker/main.py:149-161` 的规则编译失败也用了默认 `retryable=True`，会空烧 5 次。

**修复**：

- `TaskProcessor` 单独捕获 `ArchiveFormatError` → `retryable=False`；
- `worker/main.py` 的规则编译/配置失败 → `retryable=False`；
- 泛化的 `Exception` 才保留 retryable（仍建议加错误分类，见 P0-1）。

**验收**：损坏 zip 第一次尝试即 `failed`、`attempts=1`、无 `pending`；尝试历史只有 1 条；作业在最后一个任务失败后立即收尾，不再等 50 秒。

#### P1-3 复制阶段不检查取消，也没有单任务运行时限

**代码位置**：

- `application/worker/processor.py:354-367`：`_copy_chunks` 只做 `_verify_lease`（每 5 秒 `touch`），不调用 `cancel_check`
- `application/worker/processor.py:249-264`：取消检查只在行循环的“每 20k 行或每 5 秒心跳”预算里
- 没有 `TaskProcessor` 的 wall-clock deadline；设计 §7.2 要求“单任务最长运行时间（如 2 小时）”

**场景**：

- 20 GB 压缩包复制要几十分钟到数小时；用户在这期间点取消，任务仍会继续复制，直到复制完成并开始解析后才可能看到取消；
- 一个灾难性正则 + 巨大文件可以让单任务跑数小时甚至更久；`timeout_ms` 只限制“单次 search”，不限制“任务总时长”；
- 文件行数 < 20k 且处理 < 5 秒时，取消检查可能一次都不发生（小文件很快结束，影响小，但说明语义依赖文件规模）。

**修复**：

- `_copy_chunks` 每个 chunk 或每 N 秒检查 `cancel_check` 和 deadline；命中则关闭临时文件、清理目录、`complete("cancelled")`；
- 增加 `task_deadline_seconds`（默认 7200），在复制/行循环/正则前后检查；超时写 `failed`/`timeout`，错误可解释；
- 取消检查的间隔统一为“每 N 行或每 N 秒”，复制阶段也遵守同一 deadline；
- 在 `file_task_attempts` 记录 `cancelled`/`timeout` 与原因。

**验收**：

- 在复制 1 GB 文件时取消，10 秒内任务 `cancelled`，临时目录删除；
- 设置 2 秒 deadline 的任务在 2–3 秒内终止，状态/尝试历史可解释；
- 任务结束后 `work_dir` 无残留。

#### P1-4 队列/收尾关键查询没有索引

**代码位置**：

- `domain/models/entities.py:77-101`：`FileTask` 只有 `job_id` 索引；没有 `(status, scheduled_at)`、`(status, heartbeat_at)`、`(job_id, status)`
- `domain/models/entities.py:47-74`：`Job` 没有 `(status, created_at)`
- `infrastructure/task_queue.py:16-38`：领取 SQL 的谓词是 `status='pending' AND scheduled_at <= now()`，排序 `scheduled_at, id`
- `infrastructure/task_queue.py:152-189`：恢复 SQL 按 `status='running'` + `heartbeat_at`
- `infrastructure/task_queue.py:279-304`：收尾/补偿按 `job_id` + 状态聚合

**影响**：20,000 个 pending 任务时，每次领取都可能对 pending 集合做 Seq Scan + Sort；20,000 次领取意味着**最坏约 4 亿行次**的排序工作（平均也约 2 亿）。即使 1000 个任务，1000 次领取 × 1000 行排序也会明显放大 CPU/延迟。设计 §8.3 明确要求 `(status, scheduled_at)` 与 `(status, heartbeat_at)` 索引，当前缺失。

**修复**：新增 Alembic 迁移（建议 0006）：

```sql
CREATE INDEX ix_file_tasks_pending ON file_tasks (status, scheduled_at, id);
CREATE INDEX ix_file_tasks_running_heartbeat ON file_tasks (status, heartbeat_at);
CREATE INDEX ix_file_tasks_job_status ON file_tasks (job_id, status);
CREATE INDEX ix_jobs_status_created ON jobs (status, created_at);
```

（SQLite 测试可走普通复合索引；若要 PG 部分索引，可再加 `WHERE status='pending'`，但注意测试可移植性。）

**验收**：

- 在 20,000 个 pending 任务上 `EXPLAIN (ANALYZE, BUFFERS)` 领取语句使用索引，不再 Seq Scan + Sort；
- 4 Worker 并发领取 20,000 次，p95 领取延迟目标 < 50 ms；
- `recover_stale` 在 20,000 个 running 任务上 < 1 秒。

#### P1-5 缺少“日志事件时间”，只能按入库时间分析

**代码位置**：

- `domain/models/entities.py:119-161`：`Match` 只有 `created_at`（入库时间），没有 `event_time`
- `application/services/queries.py:49-85`：时间过滤全部用 `created_at`
- `data/generate_mock_logs.py:10-18, 321-346`：数据明确生成 `[2026-10-27 10:00:00.123]` 时间戳与故障爆发窗口，用于“时间轴上的聚集分析”

**影响**：

- 批量日志通常**延迟处理**（例如 10-20 的日志在 10-27 才跑）。按 `created_at` 查“10-20 00:00–01:00”的命中，会得到“10-27 入库的这批”，时间轴完全错位；
- 生成器埋的 1–3 个故障爆发窗口无法通过 API 按事件时间查询；用户只能把 `raw_line` 拉出来自己解析；
- 如果规则用命名分组提取时间，字段在 `fields` JSON 里，没有索引，无法高效按时间过滤。

**修复**：

- `matches` 增加 `event_time`（timestamptz，可空）与索引 `(job_id, event_time, id)`；
- 规则集增加可选的 `timestamp` 提取配置（默认匹配行首 `[YYYY-MM-DD HH:MM:SS.mmm]`，或指定命名字段）；
- 处理器解析失败时置 NULL 并计数，不阻断匹配；
- API 增加 `event_from/event_to`，保留 `created_from/created_to` 用于保留期/摄入审计；
- 游标分页可按 `(event_time, id)` 提供新的查询入口（保留旧入口兼容）。

**验收**：

- 对 APPLOG001 的 `00:00`–`00:10` 事件窗口，按 `event_time` 查询的命中数与独立 oracle 一致；
- 10-20 的日志在 10-27 处理，查询 10-20 的时间窗口仍返回正确结果；
- `event_time` 为 NULL 的行有计数，不静默丢失。

#### P1-6 没有“是否保存原始行”的输出控制，`raw_line` 永远写

**代码位置**：

- `domain/rules/models.py:55-67`：`RuleSetConfig` 没有 `save_raw`/`raw_line_mode`
- `domain/models/entities.py:146`：`raw_line: str` 非空
- `application/worker/processor.py:220-241`：无条件 `_truncate_raw_line` 并写入

**影响**：设计 §5.1 的“输出控制：严重级别、标签、**是否保存原始行**”没有实现。RS-B 的 7.5M 命中每行约 140 字节原始行，仅 raw 数据就约 1 GB；500 GB 外推约 75 GB+，再加 3 个索引、JSONB 字段和 TOAST，容量和写入放大都很大。用户无法对“只关心计数”的规则关闭原始行。

**修复**：

- `RuleSetConfig` / `Rule` 增加 `save_raw_line: bool = True`（或 `raw_line_mode: full|truncated|none`）；
- `Match.raw_line` 改为可空（或空串），处理器按开关写入；
- metrics 记录被省略的 raw 行数；结果 API 对 null 做兼容；
- 与 P0-3 的命中上限、分区/保留期一起做容量治理。

**验收**：`save_raw_line=false` 的规则集在同样数据上 `matches` 体积显著下降（目标 raw 列为 NULL/空），`line_no`/`member_path`/`rule_id`/`numeric_value` 仍正确；查询/导出不报错。

#### P1-7 正则超时被静默跳过，任务仍可能 succeeded

**代码位置**：

- `domain/rules/evaluator.py:130-139`：`except TimeoutError` 仅 `logger.warning`，然后 `continue`
- `application/worker/processor.py:269-270`：`_member_outcome` 只看成员错误，不看规则超时
- 没有任何“超时次数/解码错误/门限解析失败”的计数进入 `TaskResult`/`file_tasks`/`file_task_attempts`

**影响**：

- 同一份日志在高负载下可能超时、低负载下不超时，**结果不确定**；任务却可能 `succeeded`，用户不知道漏了哪些行；
- 规则集里一条灾难性正则（`(a+)+$`）配 50 ms timeout，在 100 万行上最坏要 14 小时，且每行都只留一条 warning；
- 设计 §5.2 要求“超时记录错误并跳过”，但没有要求“可观测 + 可判定”；当前只有日志，无法对账。

**修复**：

- `evaluate_line` 返回/累计 `RuleEvaluationStats`（每条规则的 timeout、decode_error、threshold_unparsable 计数）；
- `TaskResult` 带 stats，写入 `file_tasks`/`file_task_attempts`（可新增 JSON 列或在 error 摘要中）；
- 当超时数 > 0：任务至少 `partial`（或按阈值 fail），不能静默 `succeeded`；
- 加指标/告警；对 `timeout_ms` 设上限（例如 5,000 ms）并限制规则数。

**验收**：`(a+)+$` 在长行上触发超时，任务 `partial`，错误含 `rule_timeout: <rule_id> x N`；对同一数据重跑，统计稳定可见（不再“看起来成功”）。

#### P1-8 规划失败不落作业、10 秒 deadline 不覆盖第二次 stat、单文件 stat 失败拖垮整批

**代码位置**：

- `application/services/jobs.py:101-142`：`discover_archives` 受 `SCAN_DEADLINE_SECONDS=10` 限制
- `application/services/jobs.py:130`：`file_stats = [(path, path.stat()) for path in files]` **在 deadline 之外**
- `application/services/jobs.py:144-172`：Job 在扫描/stat 成功后才创建
- 失败路径直接 `HTTPException(400/503)`，没有 `jobs` 行

**影响**：

- 设计 §7.1 要求“规划失败直接把 jobs.status 置 failed 并记录错误”，当前用户只能看到 HTTP 错误，作业列表里没有记录，无法审计/重试；
- 10 秒只限制 `os.walk`，不限制对 20,000 个文件的第二次 `stat`；NAS 元数据慢时请求可能远超 10 秒；
- 扫描后某个文件被删除/无权限，一次 `stat` 失败会让整个 1000 文件作业 400，已发现的合法文件全部丢弃；理想行为是跳过并记录。

**修复**：

- 先创建 `jobs` 行（例如 `pending`，扫描完成后置 `running`；若新增 `planning` 状态，必须同步状态机与文档），后台/线程池扫描；成功置 `running`，失败置 `failed` + `error`；
- 或者保留同步接口，但把 `stat` 合入 `discover_archives`，用同一个总 deadline，单文件 `stat` 失败计数并跳过；
- deadline 做成配置；`max_files` 与 `max_depth` 继续保持；
- API 响应体带 `job_id`（即使失败），便于审计。

**验收**：

- 规划时目录不可读/路径越界：`jobs` 表有一条 `failed` 记录，`error` 可读；
- 1000 文件 NAS 扫描总时长受总 deadline 约束；
- 扫描后删除 1 个文件：作业仍创建成功，缺失文件被跳过/记录，不整批 400。

### 3.3 P2（排期处理）

| # | 问题 | 位置/证据 | 影响与建议 |
|---|---|---|---|
| P2-1 | 领取是严格 FIFO（`ORDER BY scheduled_at, id`），后创建的大作业会被先创建作业的 20k 任务长时间压住 | `task_queue.py:16-38`；`jobs.py:144-172` 所有任务同刻入队 | 多作业并发时无公平性；建议按 `jobs` 轮转/优先级/每作业并发上限 |
| P2-2 | 自动重试是线性 `attempts * 5` 秒，无指数/抖动；任务回 `pending` 时不清 `worker_id`/`heartbeat_at` | `task_queue.py:113-116`；`COMPLETE_SQL` 未清 worker_id | 抖动/退避风暴；管理视图 pending 任务仍显示旧 Worker；建议指数 + jitter，回队时清 owner/heartbeat |
| P2-3 | drain 后 Worker 永远保持 `draining`，不自动 `offline` | `worker/main.py:104-108`、`_report_status`；README 说 draining/offline | 升级/排空视图不收敛；建议排空后未处理任务时置 offline 或明确状态机 |
| P2-4 | 领取前不检查本地临时盘剩余/内存；`copy_to_local`、`max_concurrent_copies` 仍未生效 | `settings.py:29-31`；`entities.py:26`；README 已登记 | 小磁盘 Worker 会领取并反复失败；建议领取前预检并置 blocked，按数据源/磁盘能力调度 |
| P2-5 | 规则校验缺口：空关键字匹配所有行（单空格等近空白关键字也几乎全命中）；空 glob 静默匹配 0 行；空正则匹配所有行；`timeout_ms` 无上限；`NaN` 门限静默不命中；BOM 未剥离；ZIP 成员名反斜杠未归一化 | `rules/models.py:7-25`；实测空关键字/BOM；`reader.py:222-228` | 误配可造成全量命中或全量漏配；建议增加模型级校验和 BOM/反斜杠归一化 |
| P2-6 | 复制使用源文件名而不是任务 ID；复制后只校验 size，不校验 mtime | `processor.py:340-352` | 设计 §7.2 要求任务 ID 命名、复制后 size+mtime 双校验；Windows 保留名/超长名风险；建议改任务 ID + 双校验 |
| P2-7 | `jobs.error`、`workers.last_error` 从不写入；无 Prometheus 指标；无 decode/threshold/timeout 计数 | `entities.py:74,185`；`pyproject.toml` 有 prometheus 依赖但零 import | 故障不可解释；建议随 P1-7/P1-8 一起补 |
| P2-8 | 生成器 manifest 的 `auth failed` 期望值错误：全局 703,072 vs 实际 272,329；`WARN` 恒为 0（只统计消息体，不含级别） | `generate_mock_logs.py:78-83, 234-237, 268-279` | 测试对账会误判；建议生成器只在消息体真的出现该词时计数，并增加 `level_counts`；或测试只使用独立 oracle |
| P2-9 | 文档漂移：README 说取消检查“每行一次”（实际每 20k 行或 5 秒心跳）；设计 §7.2 说复制文件名用任务 ID、复制后校验 size+mtime（实际源文件名、只校验 size）；设计 §7.3 说指数退避 + 抖动（实际线性 `attempts * 5`）；README 的作业终态表把“全 partial → failed”固化了 | `service/README.md:229-252`、`docs/superpowers/specs/2026-10-24-compressed-log-extraction-design.md:431,454`；代码如左 | 维护者会按错文档写测试；建议与 P1-1/P2-6/P2-2 一起更新 README 与设计说明 |
| P2-10 | 工作区未提交的 `operation_logs` 导致全量测试 2 失败；`CORE_TABLES` 未更新 | `tests/domain/models/test_entities.py:17-32`；工作区 `audit.py`/`0005` | 不是核心缺陷，但全流程测试必须先绿；建议补表集合断言或拆分测试 |
| P2-11 | `matched_text` 计算了但从不入库；`file_task_attempts` 无 `error_code`；`max_matches_per_task` 是死配置 | `rules/models.py:70-76`、`entities.py:104-116`、`config/settings.py:31` | 无用字段/死配置会误导；建议删除或落地（后者随 P0-3） |
| P2-12 | 部分终态无法手动重试（`retry_failed_tasks` 只搬 `failed`，不搬 `partial`） | `jobs.py:213-254` | 成员级损坏可能由临时读错误导致，用户无法重试 partial；建议提供“重试 partial”或明确不支持 |
| P2-13 | 删除/保留：`matches` 无分区、无保留期；`Jobs`/`DataSources`/`RuleSets` 无删除 API | 已知 D6；`routes_*` 无 DELETE | 500 GB 批次后清理困难；建议先做分区/保留期，再补归档 |
| P2-14 | M3 多行堆栈未实现 | 设计/计划已知 | 堆栈日志会被拆成多行；测试应显式断言“当前不支持”，不要把它当回归 |
| P2-15 | 领取事务把 `CLAIM_SQL` + `RULE_SQL` + `START_ATTEMPT_SQL` 绑在一个事务里；任何确定性异常（规则行缺失、attempt 冲突、迁移/数据漂移）都会回滚并把**同一条队头任务**留在 `pending`，反复重试可能堵住整个队列；没有 dead-letter/claim 错误计数 | `task_queue.py:405-446`；`worker/main.py:104-117` | 低概率但爆炸半径大；建议领取后分步处理，或对同一任务的连续 claim 错误计数并在 N 次后置 failed/跳过，同时告警 |

---

## 4. 核心功能优化方案与优先级

### 4.1 目标

在不引入新调度组件的前提下（继续用 PostgreSQL 作为唯一事实源），把核心链路做到：

1. **故障可恢复**：瞬时源错误不落永久失败；Worker 崩溃/重启不丢任务、不重复结果；
2. **结果可信**：重试不残留旧命中；partial 在作业层可见；超时/解码失败可解释；
3. **容量可控**：单任务/单作业命中上限、输出控制、分区/保留期；
4. **可分析**：按日志事件时间查询，按规则/成员/行号追溯；
5. **规模可用**：20k 任务领取有索引、有公平性、有背压。

### 4.2 实施顺序（建议按 4 个 PR 拆）

#### PR-1：错误分类 + 重试清场 + 命中上限（P0）

| 改动 | 文件 | 说明 |
|---|---|---|
| 新增错误类型 `TransientSourceError`、`ContentError`，按 errno/根健康度分类 | `application/worker/processor.py` | `ENOENT/EACCES/ESTALE/EIO/ETIMEDOUT/ENOTCONN` 先重试；路径越界/size/mtime 变化仍永久 |
| 领取前数据源健康探针；不健康时 Worker 置 `blocked`/退避 | `worker/main.py`、`task_queue.py` | 避免把不可达挂载上的任务写死 |
| 重试清场提前到租约校验后、源校验前；或领取事务内删除 | `processor.py`；`task_queue.py:claim_next` | 保证任何永久失败路径都清旧命中 |
| 接入 `max_matches_per_task`；达到上限落 `failed`/`partial` + 明确错误 | `processor.py`、`settings.py` | 不写 succeeded；停止扫描 |
| 规则校验拒绝空关键字/空正则；glob 空串拒绝 | `rules/models.py`、`rule_sets.py` | 防止一条配置写满库 |
| 增加失败路径单测（§4.4） | `tests/application/worker/`、`tests/domain/rules/` | 当前应红，修后绿 |

**验收门槛**：P0-1/2/3 的验收用例全绿；50 包全量 + 一个 match-all 规则不再写爆库。

#### PR-2：终态/取消/超时/格式错误（P1-1/2/3/7）

| 改动 | 文件 | 说明 |
|---|---|---|
| 重写 `_derive_job_status` 的 partial 矩阵 | `task_queue.py`、`service/README.md` | 全 partial → partial |
| `ArchiveFormatError` / 规则编译错误 → `retryable=False` | `processor.py`、`worker/main.py` | 确定性错误不空烧 |
| 复制阶段取消检查 + 任务 deadline | `processor.py`、`settings.py` | 复制/解析都可停止；attempt 记录 cancelled/timeout |
| 规则超时/解码失败/门限解析计数 | `evaluator.py`、`ports.py`、`processor.py` | 进入 TaskResult/attempt error；超时任务不得静默 succeeded |
| 重试退避指数 + jitter；回队清 owner/heartbeat | `task_queue.py` | 与 P2-2 一起做 |
| 增加 PG 集成用例：全 partial、格式损坏、取消/恢复 | `tests/integration/` | 使用真实 PG |

#### PR-3：容量与可分析性（P1-4/5/6/8）

| 改动 | 文件 | 说明 |
|---|---|---|
| Alembic 0006 队列/收尾索引 | `alembic/versions/` | `(status, scheduled_at, id)` 等 |
| 规划失败落 Job.failed + 总 deadline/stat 合并 | `jobs.py`、`reader.py`、`schemas.py` | 失败作业可审计；单文件 stat 失败不整批 400 |
| `matches.event_time` + 索引 + API 参数 | `entities.py`、`processor.py`、`queries.py`、`routes_matches.py` | 事件时间查询 |
| `save_raw_line` 输出控制 | `rules/models.py`、`entities.py`、`processor.py` | raw 行可关；配合分区/保留期 |
| 先做 `matches` 按月分区/保留期（或分表归档） | `alembic/`、维护任务 | 500 GB 批次的结果可清理 |
| `job.error` / `workers.last_error` 写入 | `jobs.py`、`worker/main.py` | 管理页可解释 |

#### PR-4：性能与公平性（P2 + 规模）

| 改动 | 说明 |
|---|---|
| 关键字 Aho-Corasick 预过滤 / 规则编译缓存 | 设计 §8.1 要求；当前每条关键字逐行 `in`，正则对所有行执行 |
| 预计算小写关键字，避免每行每词 `.lower()` | 当前实测 9 关键字 ~189k 行/s，规则变多会线性下降 |
| 每作业轮转/并发上限；或按 `jobs` 轮询领取 | 避免后创建作业被 20k 任务饿死 |
| 领取前磁盘/内存预检；`copy_to_local` 生效 | 避免小磁盘 Worker 反复失败 |
| `matches` 批量 COPY/分区写入评估 | 7.5M 行/批次的写入放大治理 |

### 4.3 不建议做的事

- **不要**为了修 P0-1 把 `ENOENT` 一律当永久失败：挂载点不可见时 `ENOENT` 是常态；需要区分“根不可达”和“根可达但文件被删”。
- **不要**在 `TaskProcessor` 里只加一个 `max_matches` 计数却不改变去重/清场：P0-2 仍会产生脏结果。
- **不要**把 `partial` 只当作展示层字段：它会参与作业终态和重试决策，必须在 `_derive_job_status` 与 SQL 收尾查询里保持同义（当前三处已经通过 `_open_task_predicate` 统一，新增状态时不要各写一遍）。
- **不要**在未加索引前直接压 20k 任务做性能结论；先把 P1-4 的索引和 P1-3 的 deadline 做掉。

### 4.4 修复验收用例（建议先写红）

| 用例 | 当前预期 | 修复后 |
|---|---|---|
| `test_transient_stat_error_is_retryable` | ❌ failed/retryable=False | ✅ pending + attempts+1 |
| `test_retry_cleanup_before_source_check` | ❌ 旧 matches 残留 | ✅ 0 行 |
| `test_terminal_failure_clears_matches` | ❌ 最后一次尝试的命中残留 | ✅ 0 行/按状态不可见 |
| `test_match_limit_stops_task` | ❌ 无上限 | ✅ 到上限停止且状态可见 |
| `test_all_partial_tasks_close_job_as_partial` | ❌ failed | ✅ partial |
| `test_invalid_archive_is_not_retried` | ❌ retryable=True | ✅ failed/attempts=1 |
| `test_cancel_interrupts_copy` | ❌ 复制继续 | ✅ 10s 内 cancelled |
| `test_task_deadline_aborts` | ❌ 无 deadline | ✅ timeout |
| `test_regex_timeout_marks_task_partial` | ❌ succeeded | ✅ partial + 计数 |
| `test_claim_query_uses_pending_index`（PG） | ❌ Seq Scan | ✅ Index Scan |
| `test_claim_poison_does_not_block_queue`（PG） | ❌ 队头反复失败 | ✅ 其它任务继续 |
| `test_blank_keyword_rejected` / `test_blank_glob_rejected` | ❌ 入库/静默 | ✅ 422 |
| `test_bom_is_stripped` | ❌ 首行锚定正则不中 | ✅ 命中 |
| `test_planning_failure_creates_failed_job` | ❌ 无 jobs 行 | ✅ failed + error |

---

## 5. 全流程真实测试方案

### 5.1 目标与准出条件

**目标**：用 `data/logs` 的真实形态数据，从 REST API 创建数据源/规则集/作业，启动 4–6 个真实 Worker，走完“扫描 → 领取 → 复制 → 解压 → 逐行匹配 → 批量入库 → 作业收尾 → 结果查询”，并验证：

1. 行数、成员数、每条规则命中数与独立 oracle **逐项相等**；
2. 无重复命中、无旧代残留、无作业悬挂；
3. 故障注入后可恢复，结果仍等于 oracle；
4. 20k 任务领取性能可接受；
5. NAS 源目录零写入（size/mtime/hash 不变）。

**准出（必须全部满足）**：

| 项 | 阈值 |
|---|---|
| 行数/成员数 | 100% 相等 |
| RS-B 15 条规则逐条命中 | 100% 相等（全局 7,514,071） |
| 重复 `(job_id, file_task_id, member_path, line_no, rule_id)` | 0 |
| 失败/取消后旧 `matches` 残留 | 0 |
| 全 partial 作业终态 | `partial` |
| 作业终态与文件任务分布一致 | 100% |
| 4 Worker 并发领取重复 | 0 |
| 20k pending 领取 p95 | < 50 ms（索引后） |
| Worker kill -9 恢复 | ≤ 120s + 退避，无重复 |
| 取消（解析中） | ≤ 10s |
| 取消（复制中） | ≤ 10s（修复后） |
| NAS 源目录 | 0 写入/0 删除/0 重命名，mtime/哈希不变 |
| Worker 进程崩溃 | 0（故障注入除外） |

### 5.2 环境与拓扑

**推荐环境**：Linux（WSL2 Ubuntu 22.04 或真实 Linux）+ PostgreSQL 16 + Python 3.12 + uv。

```text
1 个 FastAPI Server（uvicorn，1 进程）
4–6 个 Worker 进程（不同 LOGHARVEST_WORKER_ID）
1 个 PostgreSQL 16（可丢弃测试库 logharvest_test）
1 个只读挂载/只读权限的日志目录（/srv/lh/ro）
本地临时盘：/srv/lh/work（容量 >= 20 × 最大压缩包，至少 20 GB）
```

**为什么必须在 Linux/WSL**：

- 队列 SQL 使用 `SKIP LOCKED`、`make_interval`、`ON CONFLICT`，SQLite 不能执行；
- NAS 只读守卫在 Windows/SMB 上恒为 `unknown`（`source_guard.py` 的已知限制），`enforce` 不生效；要在 Linux 上用只读 bind mount 或 `chmod -R a-w` 验证；
- 真实生产拓扑是 Linux + NAS 挂载。

**Windows 本地功能测试的例外**：如果只在 Windows 上跑，设置 `LOGHARVEST_NAS_READ_ONLY_POLICY=warn`，并把“只读强制拒绝”用例放到 Linux 环境执行；不要把 Windows 的 `unknown` 结果当作只读保护已验证。

### 5.3 数据分层

| 层 | 数据 | 规模 | 用途 |
|---|---|---|---|
| D0 冒烟 | `python data/generate_mock_logs.py --quick --out /srv/lh/quick` | 4 包 × 10 成员 × 1–2k 行 | 1–2 分钟验证 API/Worker/规则链路 |
| D1 功能/对账 | 当前 `data/logs`（50 ZIP） | 665 成员 / 49,736,840 行 / 6.84 GB | 本文主对账数据集 |
| D2 边界夹具 | `corrupt_member.zip`、`truncated.tar.gz`、`empty.zip`、`bomb.zip`、`gb18030.zip`、`bom.zip`、`long_line.zip`、`nested/a.log`、`bad.zip` | 小 | 验证 partial/格式/编码/长行/glob/预算/取消 |
| D3 队列规模 | 20,000 个 100 行小 ZIP | 2M 行 | 领取/索引/公平性/背压 |
| D4 容量规模 | 1,000 个包（`--archives 1000 --min-files 4 --max-files 6 --min-lines 1000 --max-lines 2000`） | ~7.5M 行 / ~1 GB 解压 | 1000+ 文件约束的完整跑批 |
| D5 500 GB（可选/最终） | 真实 NAS 样本或生成 ~4,000 个默认规格包 | ~500 GB 解压 | 生产容量/时长/分区/保留期演练 |

**注意**：

- 不要把 D2 的坏包混进 D1 的 happy-path 作业；否则作业 `partial/failed` 会让“应全成功”的对账失效。D2 单独建目录/作业。
- 生成器默认 50 包是 6.84 GB，不是 500 GB；不要把 D1 的结果外推成“已证明 500 GB”。
- `data/logs/manifest.json` 的 `auth failed` 不可作为 oracle（见 P2-8），必须用 §5.5 的独立 oracle。

**D4 规模数据生成示例**（独立目录，不复用 D1）：

```bash
python data/generate_mock_logs.py \
  --out /srv/lh/scale1k \
  --archives 1000 --op-archives 200 \
  --min-files 4 --max-files 6 \
  --min-lines 1000 --max-lines 2000 \
  --workers 8 --seed 42
```

D3 的 20,000 个小 ZIP 建议用独立的 `zipfile` 脚本生成（每个 1 个 `a.log`、100 行），放在 `/srv/lh/scale20k`，不要放进 `/srv/lh/logs`，避免改变 D1 的 50 文件期望值。

### 5.4 规则集与期望值

#### RS-A：关键字基线（用于 manifest 对账）

```json
{
  "name": "rs-a-keyword-baseline",
  "file_glob": "**/*.log",
  "encoding": "utf-8",
  "fallback_encoding": "gb18030",
  "rules": [
    {"id": "level_error", "match": {"type": "keyword", "any": ["ERROR"], "case_sensitive": true}},
    {"id": "level_fatal", "match": {"type": "keyword", "any": ["FATAL"], "case_sensitive": true}},
    {"id": "conn_refused", "match": {"type": "keyword", "any": ["connection refused"], "case_sensitive": true}},
    {"id": "timeout", "match": {"type": "keyword", "any": ["timeout"], "case_sensitive": true}},
    {"id": "auth_failed", "match": {"type": "keyword", "any": ["auth failed"], "case_sensitive": true}},
    {"id": "oom", "match": {"type": "keyword", "any": ["out of memory"], "case_sensitive": true}},
    {"id": "segv", "match": {"type": "keyword", "any": ["segmentation fault"], "case_sensitive": true}},
    {"id": "disk_full", "match": {"type": "keyword", "any": ["disk full"], "case_sensitive": true}},
    {"id": "core_dumped", "match": {"type": "keyword", "any": ["core dumped"], "case_sensitive": true}}
  ]
}
```

期望（全量）：除 `auth_failed=272,329` 外，其余 8 个关键字与 manifest 完全一致（见 §2.1/§2.3）。`WARN` 不要作为规则；manifest 只统计消息体，级别 `[WARN ]` 不在其中。

#### RS-B：15 条业务规则（主对账）

```json
{
  "name": "rs-b-biz-15",
  "file_glob": "**/*.log",
  "encoding": "utf-8",
  "fallback_encoding": "gb18030",
  "rules": [
    {"id": "level_error", "match": {"type": "keyword", "any": ["ERROR"], "case_sensitive": true}, "severity": "error"},
    {"id": "level_fatal", "match": {"type": "keyword", "any": ["FATAL"], "case_sensitive": true}, "severity": "critical"},
    {"id": "conn_refused", "match": {"type": "keyword", "any": ["connection refused"], "case_sensitive": true}, "severity": "error"},
    {"id": "timeout", "match": {"type": "keyword", "any": ["timeout"], "case_sensitive": true}, "severity": "error"},
    {"id": "auth_failed", "match": {"type": "keyword", "any": ["auth failed"], "case_sensitive": true}, "severity": "error"},
    {"id": "oom", "match": {"type": "keyword", "any": ["out of memory"], "case_sensitive": true}, "severity": "critical"},
    {"id": "segv", "match": {"type": "keyword", "any": ["segmentation fault"], "case_sensitive": true}, "severity": "critical"},
    {"id": "disk_full", "match": {"type": "keyword", "any": ["disk full"], "case_sensitive": true}, "severity": "critical"},
    {"id": "core_dumped", "match": {"type": "keyword", "any": ["core dumped"], "case_sensitive": true}, "severity": "critical"},
    {"id": "op_failed", "match": {"type": "keyword", "any": ["result=FAILED"], "case_sensitive": true}, "severity": "warning"},
    {"id": "audit_fail", "match": {"type": "keyword", "any": ["res=fail"], "case_sensitive": true}, "severity": "warning"},
    {"id": "slow_cost_gt5000", "match": {"type": "regex", "pattern": "cost_ms=(?P<cost_ms>\\d+)"}, "threshold": {"field": "cost_ms", "op": "gt", "value": 5000}, "severity": "warning"},
    {"id": "retry_gte5", "match": {"type": "regex", "pattern": "retry=(?P<retry>\\d+)"}, "threshold": {"field": "retry", "op": "gte", "value": 5}, "severity": "warning"},
    {"id": "usage_gte98", "match": {"type": "regex", "pattern": "usage=(?P<usage>\\d+)"}, "threshold": {"field": "usage", "op": "gte", "value": 98}, "severity": "warning"},
    {"id": "http_5xx", "match": {"type": "regex", "pattern": "status=(?P<status>5\\d\\d)"}, "severity": "error"}
  ]
}
```

**期望**：§2.3 的表；合计 **7,514,071**。如果服务端结果与表不一致，先检查规则 ID/大小写/门限，再用 §5.5 的 oracle 定位。

> **命中上限与 happy path 的关系**：RS-B 在 D1 上平均约 15 万命中/任务，但最大的包接近（甚至可能超过）默认 `max_matches_per_task=200_000`。P0-3 修复后，happy path 需要显式把上限调到 500,000（或按 oracle 的 per-archive 最大值 + 10%），否则“命中上限”会把正常任务截断；命中上限本身用 RS-C 的 match-all 规则单独验证，两者不要混在一起。

#### RS-C：边界规则（应被拒绝或应产生特定状态）

| 规则集 | 期望 |
|---|---|
| `any=[""]` | `POST /api/rule-sets` 422（修复后）；修复前会写爆库，仅在 P0-3 用例中使用 |
| `pattern=""` | 422（修复后） |
| `threshold.field` 不在命名分组 | 422（现有） |
| `flags="x"` | 422（现有） |
| `(a+)+$` + 长行 | 任务 `partial`/failed，不能挂死/静默 succeeded（修复后） |
| `file_glob=""` | 422（修复后）；修复前匹配 0 行 |
| UTF-8 BOM + `^\[` | 剥离 BOM 后命中（修复后） |
| `file_glob="**/*.log"` + 成员在 `sub/` | 命中（pathspec 已验证） |

### 5.5 独立 oracle（不要用服务端代码算期望值）

测试期望值必须由**独立于服务端 evaluator** 的实现生成：`zipfile` + 标准库 `re`/`in`。下面是一个可运行的最小脚本（保存到测试机 `/tmp/lh_oracle.py`，不要放进生产镜像）：

```python
import json, re, sys, zipfile
from collections import Counter
from pathlib import Path

ROOT = Path(sys.argv[1])  # data/logs
COST = re.compile(r"cost_ms=(\d+)")
RETRY = re.compile(r"retry=(\d+)")
USAGE = re.compile(r"usage=(\d+)")
STATUS = re.compile(r"status=(5\d\d)")
TOKENS = [
    ("connection refused", "conn_refused"), ("timeout", "timeout"),
    ("auth failed", "auth_failed"), ("out of memory", "oom"),
    ("segmentation fault", "segv"), ("disk full", "disk_full"),
    ("core dumped", "core_dumped"), ("result=FAILED", "op_failed"),
    ("res=fail", "audit_fail"),
]

def count_archive(path):
    c = Counter(); lines = 0; members = 0
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            members += 1
            with z.open(info) as f:
                for raw in f:
                    line = raw.decode("utf-8", "replace").rstrip("\n")
                    lines += 1
                    if "[ERROR]" in line: c["level_error"] += 1
                    if "[FATAL]" in line: c["level_fatal"] += 1
                    for token, rid in TOKENS:
                        if token in line: c[rid] += 1
                    m = COST.search(line)
                    if m and int(m.group(1)) > 5000: c["slow_cost_gt5000"] += 1
                    m = RETRY.search(line)
                    if m and int(m.group(1)) >= 5: c["retry_gte5"] += 1
                    m = USAGE.search(line)
                    if m and int(m.group(1)) >= 98: c["usage_gte98"] += 1
                    if STATUS.search(line): c["http_5xx"] += 1
    return lines, members, c

total = Counter(); all_lines = all_members = 0
per_archive = {}
for path in sorted(ROOT.glob("*.ZIP")):
    lines, members, c = count_archive(path)
    all_lines += lines; all_members += members; total.update(c)
    per_archive[path.name] = {"lines": lines, "members": members, "counts": dict(c)}
print(json.dumps({
    "lines": all_lines, "members": all_members,
    "total": dict(sorted(total.items())),
    "per_archive": per_archive,
}, ensure_ascii=False, indent=2))
```

运行：

```bash
python /tmp/lh_oracle.py /path/to/data/logs > /tmp/lh_oracle.json
```

本次已用同逻辑跑出 §2.3 的全局期望；如果重新生成数据（不同 seed），必须重跑 oracle，不要沿用本文数字。

### 5.6 黄金样本（数据库逐字段断言）

数据文件：`data/logs/APPLOG001.ZIP`，成员：`app_20261020_01.log`，行号 1-based。以下行可直接作为 E2E 的“结果字段是否正确”的黄金样本：

| 行号 | 原始行（节选） | 规则 | 断言 |
|---:|---|---|---|
| 29 | `[2026-10-20 00:00:05.729][ERROR][pid=53138][tid=53201][app_svc.c:97] connection refused host=mq-broker-02 port=6379 retry=9` | `level_error`, `conn_refused`, `retry_gte5` | `source_path` 以 `APPLOG001.ZIP` 结尾；`member_path=app_20261020_01.log`；`file_name=app_20261020_01.log`；`line_no=29`；`retry_gte5.fields={"retry":"9"}`；`numeric_value=9`；`raw_line` 完整 |
| 34 | `[2026-10-20 00:00:06.313][WARN ][pid=53138][tid=53195][app_svc.c:59] slow request uri=/api/v1/login cost_ms=12210 threshold_ms=3000` | `slow_cost_gt5000` | 不应出现 `level_error`；`fields={"cost_ms":"12210"}`；`numeric_value=12210` |
| 118 | `[2026-10-20 00:00:25.679][ERROR][pid=53138][tid=53141][app_svc.c:811] auth failed user=svc_cron reason=PERMISSION_DENIED ip=10.0.240.120` | `level_error`, `auth_failed` | 行号/成员路径正确；`fields={}` |
| 211 | `[2026-10-20 00:00:07.903][ERROR][pid=53138][tid=53189][app_svc.c:198] request timeout uri=/api/v1/reports cost_ms=6091 threshold_ms=5000` | `level_error`, `timeout`, `slow_cost_gt5000` | `fields={"cost_ms":"6091"}`；`numeric_value=6091` |
| 2601 | `[2026-10-20 00:09:33.881][FATAL][pid=53138][tid=53199][app_svc.c:878] out of memory requested_mb=3900 available_mb=193` | `level_fatal`, `oom` | 不应出现 `level_error`；字段为空 |

建议断言 SQL（以实际 job_id 替换）：

```sql
SELECT rule_id, member_path, file_name, line_no, raw_line, fields, numeric_value
FROM matches
WHERE job_id = :job_id
  AND source_path LIKE '%APPLOG001.ZIP'
  AND member_path = 'app_20261020_01.log'
  AND line_no IN (29, 34, 118, 211, 2601)
ORDER BY line_no, rule_id;
```

### 5.7 环境准备与执行命令

#### 5.7.1 PostgreSQL 与后端

```bash
# 1) 可丢弃的 PostgreSQL 测试库（示例：本机 PG16）
createdb -U postgres logharvest_test
createdb -U postgres logharvest_it   # 给 integration 测试用

# 2) 服务端
cd /path/to/think/service
uv sync --python 3.12

export LOGHARVEST_ENVIRONMENT=dev
export LOGHARVEST_JWT_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export LOGHARVEST_ADMIN_PASSWORD='change-me-123'
export LOGHARVEST_DATABASE_URL='postgresql+psycopg://postgres@127.0.0.1:5432/logharvest_test'
# psql 不认 SQLAlchemy 的 +psycopg 驱动后缀，单独留一个纯 libpq DSN 给命令行
export PG_DSN='postgresql://postgres@127.0.0.1:5432/logharvest_test'
export LOGHARVEST_AUTO_CREATE_TABLES=true
export LOGHARVEST_NAS_READ_ONLY_POLICY=warn   # 功能测试；enforce 用例见 5.7.4
export LOGHARVEST_ACCESS_TOKEN_EXPIRE_MINUTES=240  # 大批量测试避免 token 中途过期

uv run --python 3.12 alembic upgrade head
uv run --python 3.12 -m scripts.seed_admin
uv run --python 3.12 uvicorn src.main:app --host 127.0.0.1 --port 8000
```

> `uvicorn` 会占住当前终端；Worker 与 curl 在另一个终端执行。若另开终端，先重新 export `LOGHARVEST_ENVIRONMENT`、`LOGHARVEST_JWT_SECRET_KEY`、`LOGHARVEST_DATABASE_URL`、`PG_DSN` 等变量。

```bash
# 3) 基线必须先绿（当前工作区有 operation_logs 的 2 条失败，先修再跑）
uv run --python 3.12 pytest -q
# 4) PG 集成测试（单独库，fixture 会 DROP SCHEMA）
LOGHARVEST_TEST_PG_URL='postgresql+psycopg://postgres@127.0.0.1:5432/logharvest_it' \
  uv run --python 3.12 pytest tests/integration -v
```

#### 5.7.2 数据准备

```bash
# 只复制 ZIP + manifest，不动 data/logs 原目录
mkdir -p /srv/lh/{logs,work}
cp /path/to/think/data/logs/*.ZIP /path/to/think/data/logs/manifest.json /srv/lh/logs/

# 功能测试：可写目录（guard=warn）
# 只读强制测试：见 5.7.4
```

#### 5.7.3 启动 4 个 Worker

每个 Worker 必须唯一 `LOGHARVEST_WORKER_ID`，否则会共用 `workers` 行并可能互删本地目录（README 已警告）。

> Worker 进程导入 `Settings` 时同样要求 `LOGHARVEST_JWT_SECRET_KEY`（最短 32 字符）。下面的循环假定与 §5.7.1 在**同一个 shell** 中执行（环境变量已 export）；若另开 shell，必须重新 export `LOGHARVEST_ENVIRONMENT` 与 `LOGHARVEST_JWT_SECRET_KEY`。

```bash
cd /path/to/think/service
for i in 1 2 3 4; do
  LOGHARVEST_WORKER_ID="worker-$i" \
  LOGHARVEST_WORKER_VERSION="e2e-$(date +%s)" \
  LOGHARVEST_WORK_DIR=/srv/lh/work \
  LOGHARVEST_DATABASE_URL='postgresql+psycopg://postgres@127.0.0.1:5432/logharvest_test' \
  LOGHARVEST_NAS_READ_ONLY_POLICY=warn \
  uv run --python 3.12 python -m src.worker \
    > "/tmp/lh-worker-$i.log" 2>&1 &
done

# 确认 workers 表
psql "$PG_DSN" -c "select id,status,version,last_heartbeat from workers order by id;"
```

#### 5.7.4 Linux 只读强制验证（P0/P1 的安全不变量）

```bash
# 唯一稳定的“只读”构造：只读 bind mount（需要 root）
mkdir -p /srv/lh-ro
mount --bind /srv/lh/logs /srv/lh-ro
mount -o remount,ro,bind /srv/lh-ro
mount | grep /srv/lh-ro   # 应看到 ro
```

> **不要用 `chmod -R a-w` 冒充只读**：Linux 上 `/proc/self/mounts` 可读且挂载选项是 `rw` 时，`detect_writability()` 会直接判 `writable`，mode 位（`writability_from_mode`）只是“挂载表读不到”时的弱证据。`chmod` 不会让 `enforce` 放行。

然后：

- `LOGHARVEST_NAS_READ_ONLY_POLICY=enforce` 启动服务/Worker；
- 用 `/srv/lh-ro` 建数据源/作业，期望全流程成功；
- **写拒绝验证**：用默认 rw 的 `/srv/lh/logs` 建第二个数据源/作业 → 期望 **409，不规划、不处理**（不需要改权限，rw 挂载本身就是可写证据）；
- 运行前后对只读源做快照：

```bash
DATA_ROOT=/srv/lh-ro
find "$DATA_ROOT" -type f -printf '%p %s %T@\n' | sort > /tmp/nas-before.txt
# 跑完作业后
find "$DATA_ROOT" -type f -printf '%p %s %T@\n' | sort > /tmp/nas-after.txt
diff /tmp/nas-before.txt /tmp/nas-after.txt   # 必须为空
# 可再加 sha256sum 对少量文件做内容校验
```

#### 5.7.5 通过 API 建数据源/规则集/作业

> 功能测试用 `/srv/lh/logs`；只有 §5.7.4 的只读强制测试才把下面的 `/srv/lh-ro` 换成只读挂载目录。两者只能选一个作为本次作业的 `source_path`。

```bash
BASE=http://127.0.0.1:8000
DATA_ROOT=/srv/lh/logs   # 只读强制测试改为 /srv/lh-ro
TOKEN=$(curl -s -X POST "$BASE/api/login" \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"change-me-123"}' | jq -r '.data.accessToken')

# 数据源（root_path 指向本次要扫描的目录）
curl -s -X POST "$BASE/api/data-sources" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"name\":\"e2e-logs\",\"root_path\":\"$DATA_ROOT\",\"description\":\"LogHarvest E2E\"}"

# 规则集：把 §5.4 的 RS-B JSON 存成 /tmp/rs-b.json，外层包 {"name":..., "config":...}
jq -n --slurpfile cfg /tmp/rs-b.json '{name:"rs-b-biz-15", config:$cfg[0]}' > /tmp/rs-b-create.json
curl -s -X POST "$BASE/api/rule-sets" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  --data @/tmp/rs-b-create.json | tee /tmp/rs-b-created.json

# 作业（Idempotency-Key 便于重放验证）
curl -s -X POST "$BASE/api/jobs" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Idempotency-Key: e2e-d1-rsb-20261027" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"e2e-d1\",\"data_source_id\":1,\"source_path\":\"$DATA_ROOT\",\"rule_set_id\":1,
       \"file_filter\":{\"recursive\":true,\"max_depth\":5,\"max_files\":20000}}" \
  | tee /tmp/job.json
```

（`data_source_id`/`rule_set_id` 用上一步返回的实际 id；`source_path` 必须位于数据源 `root_path` 下。）

> **可重复性**：同一条 `Idempotency-Key` 会重放旧作业，数据源/规则集名称也有唯一约束。每次完整重跑前重建测试库（`DROP SCHEMA public CASCADE; CREATE SCHEMA public;` 后 `alembic upgrade head`）或更换名称与 Key；不要指望同 key 再次创建出一份新作业。

#### 5.7.6 观察与收尾

```bash
JOB=$(jq -r '.id' /tmp/job.json)
watch -n 2 "psql \"$PG_DSN\" -c \"
  select status, count(*), sum(processed_lines), sum(matched_rows)
  from file_tasks where job_id=$JOB group by status order by status;\""
tail -f /tmp/lh-worker-*.log
```

作业终态后执行 §5.9 的对账 SQL；若失败，保留 job id、oracle JSON、Worker 日志、PG 计划，不要直接重跑整个作业（先确认 `matches` 是否需要清场）。

### 5.8 用例矩阵

#### A. 任务下发（DISP）

| ID | 场景 | 步骤/注入 | 期望 |
|---|---|---|---|
| DISP-01 | 幂等重放 | 同 Key 同 body 再 POST | 200，同 job id，`file_tasks` 数不变 |
| DISP-02 | 幂等冲突 | 同 Key 不同 body | 409，原 job 不变 |
| DISP-03 | 空目录 | 建空目录作业 | 201，`succeeded`，`total_files=0`，`finished_at` 非空 |
| DISP-04 | 路径越界 | `source_path=/etc` | 400/403，无任务 |
| DISP-05 | 只读拒绝 | rw 挂载 + enforce | 409，不建任务 |
| DISP-06 | max_files | 20,001 个文件 | 400（或按实现），不建半截作业 |
| DISP-07 | 扫描超时 | 慢目录/10s deadline | 503 且错误可读；修复 P1-8 后 `jobs.failed` 可审计 |
| DISP-08 | 后缀过滤 | 只传 `.zip`；只传 `.log` | 前者只含 ZIP；后者 400 |
| DISP-09 | 20k 任务 | D3 数据 | `total_files=20000`；插入/领取不超时；p95 见 P1-4 |
| DISP-10 | 多作业公平性 | 先建大作业再建小作业 | 记录当前 FIFO 现象；修复 P2-1 后小作业不被饿死 |
| DISP-11 | 扫描后删文件 | 扫描与 stat 之间删除 1 个 | 修复 P1-8 后作业仍成功并记录 skipped；当前可能 400 |

#### B. Worker 领取/执行（WRK）

| ID | 场景 | 步骤/注入 | 期望 |
|---|---|---|---|
| WRK-01 | 4 Worker 并发领取 | 8 个 pending，4 线程/进程 | 每个恰好领 1 次，无重复 |
| WRK-02 | 尝试历史 | 查看 `file_task_attempts` | 每次领取 1 行；attempt_no 与 `attempts` 一致 |
| WRK-03 | 租约栅栏 | 手工把 attempts+1 后旧 Worker touch/heartbeat | 返回 False；不写 matches |
| WRK-04 | 复制续租 | 大文件复制 > 2 分钟 | 心跳持续；不被 120s 回收 |
| WRK-05 | kill -9 | 任务 running 时 kill Worker | 120s 后 attempt=timeout，任务重领；最终命中数等于 oracle，无重复 |
| WRK-06 | 源 size 变化 | 规划后改副本 size | 永久 failed，不自动重试；错误明确 |
| WRK-07 | 瞬时挂载错误 | 复制/解析中卸载再挂载 | **修复后**重试成功；当前可能永久 failed（P0-1） |
| WRK-08 | 磁盘满 | 把 work_dir 挂到小 tmpfs | 任务失败但不破坏源；修复 P2-4 后领取前 blocked |
| WRK-09 | 解析中取消 | 大包解析时 POST /cancel | 任务 cancelled，≤10s；作业 cancelled |
| WRK-10 | 复制中取消 | 复制大文件时取消 | **修复后**≤10s；当前会等复制完（P1-3） |
| WRK-11 | 手工重试 | failed 任务 POST retry-failed | attempts 保留；旧 matches 清 0；新尝试写入正确数量 |
| WRK-12 | drain/resume | POST drain；等当前任务；再 resume | drain 期间不领取；resume 恢复；状态可解释 |
| WRK-13 | 同 ID 多进程 | 两个 Worker 用同 ID | 记录已知冲突（README 已警告）；不纳入准出 |
| WRK-14 | 领取前置失败 | 测试库删除 rule_set 行/制造 attempt 冲突 | 该任务 failed/跳过，其他任务继续；不能堵住队列（P2-15） |
| WRK-15 | 任务 deadline | 设置 2s deadline + 大文件 | 超时终止；attempt 记录 timeout（P1-3） |

#### C. 规则解析（RULE）

| ID | 场景 | 数据 | 期望 |
|---|---|---|---|
| RULE-01 | 关键字 any/all/大小写 | 构造行 | 与 evaluator 单测一致；`all` 必须全中 |
| RULE-02 | 正则命名分组 | `cost_ms=...` | `fields` 正确；`numeric_value` 正确 |
| RULE-03 | 门限 gt/gte/lt/lte/eq | 边界值 5000/5001 | 只有满足规则的命中；缺字段/非数值不命中 |
| RULE-04 | 编码 UTF-8/GB18030/混合 | 生成对应 ZIP | 文本解码正确；中文行不被丢弃 |
| RULE-05 | BOM | UTF-8 BOM 首行 | 修复 P2-5 后 `^\[` 命中；当前不命中 |
| RULE-06 | 长行 | 2MB 单行 | 1MB 截断匹配；`raw_truncated=true`；行号不丢 |
| RULE-07 | file_glob | `sub/a.log` vs `**/*.log` | 匹配行为与 pathspec 一致；不匹配成员不评估 |
| RULE-08 | 成员损坏 | `corrupt_member.zip` | 健康成员继续；任务 `partial`；错误含成员名 |
| RULE-09 | 全部成员损坏 | 全坏 zip | `failed`；不自动重试（P1-2） |
| RULE-10 | 解压预算 | bomb.zip / 超大成员 | 单成员超限 → partial；整包超限 → failed 且不重试；Worker 不 OOM |
| RULE-11 | 空关键字/空正则 | RS-C | 422（修复 P0-3/P2-5） |
| RULE-12 | 灾难性正则 | `(a+)+$` + 长行 | 不挂死；任务 partial + 超时计数（P1-7） |
| RULE-13 | 行号/成员路径 | APPLOG001 黄金样本 | 逐字段相等 |
| RULE-14 | 事件时间 | 生成器爆发窗口 | 修复 P1-5 后按分钟聚合与 oracle 一致 |
| RULE-15 | 输出控制 | `save_raw_line=false` | 修复 P1-6 后 raw 为空/NULL，其他字段不变 |
| RULE-16 | 多行堆栈 | 堆栈日志 | 当前按单行处理；断言“未实现 M3”，不作为回归 |

#### D. 全流程（E2E）

| ID | 场景 | 数据/规则 | 期望 |
|---|---|---|---|
| E2E-01 | 冒烟 | D0 + RS-A | 所有任务 succeeded；行数/关键字与 D0 manifest 一致 |
| E2E-02 | 全量 happy path | D1 + RS-B，4 Worker | 50 tasks succeeded；job succeeded；49,736,840 行；7,514,071 命中；逐规则表相等 |
| E2E-03 | 幂等重放 | E2E-02 完成后同 Key 再 POST | 200，同 job，无新任务/新命中 |
| E2E-04 | 失败重试 | 1 个坏包 + 正常包 | 坏包 failed/partial；正常包不受影响；retry-failed 正确 |
| E2E-05 | 取消/恢复 | 运行中 cancel；重启 Worker | 作业 cancelled；无 pending/running 悬挂；无重复 |
| E2E-06 | 多 Worker 并发 | 4–6 Worker 同时跑 D1 | 无重复、无丢失；吞吐记录 |
| E2E-07 | 只读源 | Linux ro 挂载 + enforce | 成功且源快照不变 |
| E2E-08 | 20k 队列 | D3 | 全部完成；领取 p95；无饥饿 |
| E2E-09 | 1000 包 | D4 | 全部完成；时长/内存/磁盘记录 |
| E2E-10 | 500 GB（可选） | D5 真实 NAS | 无 OOM/无磁盘打满；作业可收尾；结果可查询 |

### 5.9 结果对账 SQL

```sql
-- 作业概览
SELECT id, status, total_files, total_bytes, started_at, finished_at, error
FROM jobs WHERE id = :job_id;
-- 期望：succeeded / 50 / 1043580887 / 非空 / error 空

-- 文件任务分布
SELECT status, count(*) AS tasks, sum(processed_lines) AS lines, sum(matched_rows) AS rows
FROM file_tasks WHERE job_id = :job_id GROUP BY status;
-- 期望：50 succeeded；lines=49736840；rows=7514071

-- 逐规则（与 §2.3 表比对）
SELECT rule_id, count(*) FROM matches WHERE job_id = :job_id GROUP BY rule_id ORDER BY rule_id;

-- 重复命中（必须 0）
SELECT job_id, file_task_id, member_path, line_no, rule_id, count(*)
FROM matches WHERE job_id = :job_id
GROUP BY 1,2,3,4,5 HAVING count(*) > 1;

-- 每条命中都应有对应任务且任务 succeeded（happy path）
SELECT count(*) FROM matches m
LEFT JOIN file_tasks t ON t.id = m.file_task_id
WHERE m.job_id = :job_id AND (t.id IS NULL OR t.status <> 'succeeded');
-- 期望：0（若存在 partial 任务，按用例单独允许）

-- 规则版本必须固定在作业创建时的版本
SELECT rule_set_id, rule_version, count(*) FROM matches
WHERE job_id = :job_id GROUP BY 1, 2;
-- 期望：仅作业绑定的 rule_set_id / version

-- 尝试历史（happy path 应 1 条/任务）
SELECT count(*) AS attempts, count(DISTINCT file_task_id) AS tasks,
       count(*) FILTER (WHERE status <> 'succeeded') AS bad
FROM file_task_attempts a JOIN file_tasks t ON t.id = a.file_task_id
WHERE t.job_id = :job_id;
-- 期望：50 / 50 / 0

-- 作业是否悬挂（应为空）
SELECT j.id, j.status, count(*) FILTER (WHERE t.status IN ('pending','running')) AS open
FROM jobs j JOIN file_tasks t ON t.job_id=j.id
GROUP BY j.id HAVING open > 0;
```

**注意**：`matched_rows` 是“命中记录数”（同一行可命中多条规则），不是“唯一行数”。本方案的 7,514,071 是记录数。

### 5.10 故障注入细节

1. **Worker kill -9**：
   ```bash
   psql "$PG_DSN" -c "select id,worker_id,status from file_tasks where job_id=$JOB and status='running';"
   kill -9 <worker_pid>
   # 等 120s；观察 attempts 增加、attempt_no=1 status=timeout、最终 succeeded
   ```
   验收：`matches` 总数为 oracle，且同一 `(task, member_path, line_no, rule_id)` 不重复。

2. **瞬时挂载错误**（P0-1）：
   - 用单独小作业（1–2 个包），在 Worker 刚 `task_claimed` 后 `umount /srv/lh-ro`（或 `chmod 000` 一个父目录）；
   - 观察任务：修复后应回 `pending`、有重试；当前会 `failed`；
   - 恢复挂载后确认成功。

3. **复制中取消**（P1-3）：
   - 用 1 GB 以上包（可将多个生成包拼成一个大 ZIP，或使用真实大包）；
   - 单 Worker 启动，`task_claimed` 后 2 秒 POST cancel；
   - 期望 10 秒内 `cancelled`，本地 work 目录删除；当前会等复制完。

4. **格式损坏**（P1-2）：
   - 用 `bad.zip`（非 ZIP 字节）建作业；
   - 当前会 5 次重试；修复后第 1 次 failed、attempts=1。

5. **重试清场**（P0-2）：
   - 在测试副本上，第 1 次尝试写入一部分后 kill Worker；
   - 把源副本 size 改掉；
   - 等第二次尝试 → 当前 failed 且旧 matches 残留；修复后 0 行。

6. **命中上限**（P0-3）：
   - RS-C 的 `any=[""]` 配 `max_matches_per_task=1000`；
   - 当前无上限；修复后任务在 1000 行停止、状态/错误可见、DB 不爆。

7. **正则超时**（P1-7）：
   - 规则 `(a+)+$`，输入 1MB 的 `a...a` 长行；`timeout_ms=50`；
   - 当前 warning + succeeded；修复后 partial + 计数。

### 5.11 规模与性能

| 项 | 方法 | 目标 |
|---|---|---|
| 20k 任务领取 | D3；4 Worker；`EXPLAIN (ANALYZE, BUFFERS)` + 领取循环计时 | 索引生效；p95 < 50 ms；无 Seq Scan |
| 1000 包 | D4；4 Worker | 全部完成；记录总时长/CPU/内存/DB 增长 |
| 500 GB | D5；4–6 Worker，真实 NAS 只读 | 不 OOM；临时盘不超预算；作业可收尾；matches 可按分区/保留期清理 |
| 规则引擎吞吐 | D1 + RS-B 单进程基线 95k 行/s；4 Worker 应接近线性（受解压/DB 限制） | 记录基线；优化 AC 后目标提升 ≥ 20%；无回归 |
| 写入吞吐 | 7.5M 命中 / 50 包 | 记录批量写入速率、索引大小、WAL 增长 |
| 内存 | 每 Worker RSS | < 2 GB（设计预算） |
| 临时盘 | `du -sh /srv/lh/work` 峰值 | ≤ 20 × 最大压缩包；任务结束清空 |

**性能结论的边界**：本次只测了单进程 reader+evaluator（95k 行/s，15 规则），没有测全链路 4 Worker + PG 写入；不要拿 95k 行/s 直接承诺 500 GB 时长。§5.11 是需要在测试环境实测的指标。

### 5.12 执行阶段与准出

| 阶段 | 内容 | 退出条件 |
|---|---|---|
| Phase 0 | 环境、迁移、种子、全量测试转绿、PG integration | 0 failed；PG integration 全绿 |
| Phase 1 | D0 冒烟 + D1/RS-B happy path + 黄金样本 | 行数/规则/黄金样本全等；无重复 |
| Phase 2 | 故障注入 P0/P1（kill、挂载、取消、坏包、清场、超时） | 每个故障的验收用例通过；无脏数据/悬挂 |
| Phase 3 | D3 20k 队列 + D4 1000 包 + P1-4 索引计划 | p95/吞吐/内存达标；无 Seq Scan |
| Phase 4 | D5 500 GB（可选）+ 复核 NAS 快照 + 结果保留期演练 | 无 OOM/磁盘打满；源零写入；可清理 |
| Phase 5 | 回归 + 准出评审 | §5.1 全满足；风险清单签字 |

---

## 6. 结论

1. **正向链路是可信的**：真实 49,736,840 行、665 个成员、15 条规则的命中与独立 oracle 完全一致；APPLOG001 的行数/成员数/8 个关键字精确对账；这不是“规则引擎不可用”的问题。
2. **真正的风险在边界与规模**：瞬时源错误永久失败（P0-1）、重试清场顺序（P0-2）、无命中上限/匹配所有行（P0-3）会在生产批次中直接造成错误终态或数据库写爆；全 partial 被吞成 failed、格式损坏空烧重试、取消/超时缺失、队列无索引、无事件时间、raw 行无输出控制，会让结果不可解释或无法在 500 GB 规模运行。
3. **优化优先级**：先修 3 个 P0 和 P1 的终态/取消/格式错误/索引；再做事件时间、输出控制、规划失败可审计；最后做 Aho-Corasick、公平调度、分区/保留期和 500 GB 容量演练。
4. **测试方案的关键**：用独立 oracle 而不是 manifest 的 `auth failed`；用 50 包真实数据做逐规则对账；用故障注入验证“恢复后仍等于 oracle”；用 PG 的 `EXPLAIN`/20k 任务验证领取；用 Linux 只读挂载验证 NAS 不变量。当前全量测试还有 2 条工作区 `operation_logs` 相关的红色，必须先转绿再开始全流程。

---

## 附录 A：代码索引（快速定位）

| 主题 | 位置 |
|---|---|
| 作业创建/扫描/第二次 stat | `service/src/application/services/jobs.py:79-184` |
| 扫描截止/文件发现 | `service/src/domain/archive/reader.py:161-219` |
| 领取 SQL / 规则快照 / attempt | `service/src/infrastructure/task_queue.py:16-46, 405-446` |
| 完成/重试/取消/恢复 SQL | `service/src/infrastructure/task_queue.py:91-189` |
| 作业状态推导 | `service/src/infrastructure/task_queue.py:279-335` |
| 源校验 / 租约 / 复制 / 取消 | `service/src/application/worker/processor.py:100-367` |
| 关键字/正则/门限 | `service/src/domain/rules/evaluator.py:60-163` |
| 规则模型/校验 | `service/src/domain/rules/models.py:1-76` |
| 归档读取/预算/成员结果 | `service/src/domain/archive/reader.py:44-541` |
| 结果批量写入 | `service/src/infrastructure/match_writer.py:12-52` |
| 表/索引 | `service/src/domain/models/entities.py:18-185` |
| 结果查询/游标 | `service/src/application/services/queries.py:49-85` |
| Worker 主循环 | `service/src/worker/main.py:96-261` |
| 规则集版本 | `service/src/application/services/rule_sets.py:31-49` |
| 配置开关 | `service/src/config/settings.py:16-79` |

## 附录 B：生成器 manifest 缺陷与修正建议

**现象**：

- `manifest.json.global_keyword_counts["auth failed"] = 703,072`，但独立 oracle 扫描全文只有 **272,329**；
- 单包 APPLOG001：manifest 8,581 vs 实际 4,859；
- `manifest.json.global_keyword_counts["WARN"] = 0`，因为生成器只统计“消息模板返回的命中词”，不统计日志级别 `[WARN ]` 本身。

**原因**：`MessageBuilder.op()` 在 ERROR/FATAL 时以 30% 概率、`audit()` 在 ERROR/FATAL 时**恒定**把 `"auth failed"` 加入 manifest 的命中集合，但消息体里写的是 `result=FAILED` / `res=fail`，没有 `auth failed` 字符串（`data/generate_mock_logs.py:234-237, 268-279`）。

**修正建议**：

1. 要么在 op/audit 的失败消息里真的写入 `auth failed`；
2. 要么不要把它计入 manifest 的 `keyword_counts`；
3. 同时增加 `level_counts`（ERROR/FATAL/WARN 行数）与 `member_line_counts`，让“级别关键字”和“消息关键字”都可以对账；
4. 在 manifest 写入规则集/oracle 版本号，避免数据变了期望值不变。

**测试口径**：在生成器修正前，`auth failed` 与 `WARN` 一律以独立 oracle 为准；不要把 manifest 当唯一事实源。

## 附录 C：建议新增的测试文件

| 文件 | 覆盖 |
|---|---|
| `tests/application/worker/test_processor_retry_cleanup.py` | P0-2 清场顺序、旧命中残留 |
| `tests/application/worker/test_processor_source_errors.py` | P0-1 errno 分类、挂载探针、重试 |
| `tests/application/worker/test_processor_match_limit.py` | P0-3 上限、停止扫描、状态 |
| `tests/application/worker/test_processor_cancel_copy.py` | P1-3 复制中取消、deadline |
| `tests/application/worker/test_processor_format_error.py` | P1-2 损坏容器不重试 |
| `tests/application/worker/test_processor_rule_timeout_stats.py` | P1-7 超时计数、partial |
| `tests/infrastructure/test_job_finalization.py` | P1-1 全 partial → partial |
| `tests/integration/test_postgres_queue_indexes.py` | P1-4 EXPLAIN 使用索引、20k claim |
| `tests/integration/test_postgres_e2e_faults.py` | kill/恢复/取消/重试的真实 PG 断言 |
| `tests/domain/rules/test_rule_validation.py` | P2-5 空关键字/空正则/空 glob/BOM |
| `tests/api/test_jobs_planning_failure.py` | P1-8 规划失败落 failed job |
| `tests/integration/test_matches_event_time.py` | P1-5 事件时间查询（功能落地后） |

---

*本文只做评估与方案，没有修改业务代码；所有“当前应红”的用例都是建议新增的回归用例，修复后再把期望翻绿。*