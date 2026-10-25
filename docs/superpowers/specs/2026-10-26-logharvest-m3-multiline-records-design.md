# LogHarvest M3 多行记录规则设计

- 状态：已评审（2026-10-26）
- 日期：2026-10-26
- 适用里程碑：M3
- 关联设计：[压缩日志提取方案](2026-10-24-compressed-log-extraction-design.md)
- 当前实现基线：M0–M2c 已交付；单文件级 `partial` 已交付；规则集配置是版本化 JSON

## 1. 背景与目标

v1 按物理行执行规则：一个压缩包成员内的每一行独立评估，命中记录只保存该行。真实日志中的 Java/Python 堆栈、异常链和多行事件需要把若干连续物理行视为一条逻辑记录，再对整条记录执行关键字、正则、数值门限和字段提取规则。

M3 的目标：

1. 规则集可选声明 `record_start_regex`，命中起始行后把后续非起始行并入同一条记录，直到下一条起始行、成员结束或文件结束。
2. 规则只在记录级别评估一次，不在多行模式下逐行重复评估。
3. 保持现有单行模式的行为、性能与 API 兼容；旧规则集不需要迁移。
4. 用明确的记录上限、超时和日志约束内存与最坏耗时，避免多行记录拖垮 Worker。
5. 前端规则集表单能录入、回显和保存该字段。

## 2. 范围与非目标

### 2.1 范围内

- 规则集级 `record_start_regex` 契约、校验与编译。
- Worker 流式记录组装、记录级规则评估与命中写库。
- 记录大小/行数上限、起始正则超时、无起始行与超限记录的可观测性。
- 规则集 API 持久化与前端表单字段。
- 单行模式的完整回归。

### 2.2 非目标（明确不做）

- 不跨压缩包成员或跨文件组装记录；成员边界强制 flush。
- 不做自动续行猜测（缩进、堆栈特征、时间戳识别）；只按用户提供的起始正则切分。
- 不新增规则类型或复杂规则 DSL。
- 不做按规则单独开启多行：多行模式是规则集级开关，所有规则共享同一套记录。
- v1 不新增 `line_end_no` 列：`matches.line_no` 表示记录起始行；记录跨度通过 `raw_line`（未截断时）或原始文件定位。精确跨度留作后续扩展。
- 不做样本试跑、Monaco/JSON 编辑器；当前前端只有表单模式。
- 不做记录级预过滤、AC 自动机或跨记录窗口优化。
- 不改变 `matches` 表结构、API 响应形状或数据库迁移版本。

## 3. 术语

- **物理行**：reader 产出的 `LineRecord`，带成员路径、文件名和行号。
- **记录**：一条或多条连续物理行组成的逻辑日志单元。
- **起始行**：文本匹配 `record_start_regex` 的物理行，开启一条新记录。
- **续行**：当前记录之后、下一条起始行之前的物理行。
- **preamble**：成员中第一条起始行之前的物理行。
- **单行模式**：`record_start_regex` 为空（旧规则集默认行为）。
- **多行模式**：`record_start_regex` 非空。

## 4. 规则契约

### 4.1 `RuleSetConfig` 新增字段

```json
{
  "name": "app-stack",
  "file_glob": "**/*.log",
  "encoding": "utf-8",
  "fallback_encoding": "gb18030",
  "record_start_regex": "^\\d{4}-\\d{2}-\\d{2}[ T]\\d{2}:\\d{2}:\\d{2}",
  "rules": [
    {
      "id": "exception",
      "match": {"type": "keyword", "any": ["Exception", "ERROR"]},
      "severity": "error"
    }
  ]
}
```

约束：

- 字段可选，缺省 `null`；旧规则集 JSON 没有该字段时按 `null` 处理。
- 空字符串或纯空白 → 规则集创建（同名会生成新版本）返回 `422`；要回到单行模式请显式省略或传 `null`。
- 使用 Python `regex` 语法，与现有规则正则同一引擎；不新增单独的 flags 字段，允许模式内联 `(?i)`、`(?m)` 等。
- 起始正则对每个物理行执行 `search`，建议用 `^` 锚定行首；不锚定时，行内任意位置的匹配也会切分记录。
- 规则集创建时编译校验；非法正则返回 `422`，错误信息带规则集/字段上下文。
- 该字段属于规则集配置内容，修改它会生成新的规则集版本；运行中的作业继续使用其固定的旧版本。

### 4.2 `CompiledRuleSet`

```python
@dataclass(frozen=True)
class CompiledRuleSet:
    config: RuleSetConfig
    rules: tuple[CompiledRule, ...]
    record_start: regex.Pattern | None = None
```

`compile_ruleset` 在编译规则的同时编译 `record_start_regex`。`record_start is None` 表示单行模式，现有评估代码路径完全不变。

## 5. 记录组装语义

### 5.1 状态机

对每个 `file_glob` 匹配的成员，按物理行顺序处理：

| 当前状态 | 新行 | 行为 |
|---|---|---|
| 无当前记录 | 匹配起始正则 | 开启新记录，起始行 = 该行 |
| 无当前记录 | 不匹配 | preamble，忽略（仍计入 `processed_lines`） |
| 有当前记录 | 匹配起始正则 | flush 当前记录，再开启新记录 |
| 有当前记录 | 不匹配 | 追加为续行 |
| 有当前记录 | 成员结束 / EOF | flush 当前记录 |
| 已因超限进入跳过态 | 匹配起始正则 | 结束跳过态，开启新记录 |
| 已因超限进入跳过态 | 不匹配 | 跳过，不追加、不评估 |

补充规则：

- 只有在 `file_glob` 匹配的成员内才组装记录；不匹配成员的行只计入进度和心跳。
- 成员边界强制 flush，记录绝不跨成员。
- 空行是合法续行，保留在记录文本中。
- 续行以 `\n` 连接；reader 已剥离行尾 CRLF，不做二次换行归一化。
- 记录的 `member_path`、`file_name` 取起始行所在成员。
- 如果成员内没有任何起始行，不自动回退单行模式，避免同一条规则集在不同文件上产生不同语义。

### 5.2 内部记录对象

```python
@dataclass(frozen=True)
class LogRecord:
    start_line: int
    end_line: int
    text: str
    member_path: str
    file_name: str
    truncated: bool
    oversize_reason: str | None = None
```

`end_line` 是最后一条**已累积**物理行的行号；因超限被跳过的行不计入。`truncated` 为真表示记录没有完整覆盖到下一个起始行/成员结束。行号是**成员内**的物理行号（reader 对每个成员从 1 重新计数）；如果任一物理行的 `LineRecord.truncated` 为真（单行超过 reader 的 1 MiB 上限），记录也标记 `truncated=True`。`oversize_reason` 只在记录因组装器上限被提前 flush 时取 `"max_lines"` / `"max_bytes"`，供 processor 输出 `record_oversized` 日志；assembler 自身不依赖日志。

### 5.3 记录上限

新增两个 Worker 级安全限额：

| 环境变量 | 默认值 | 含义 |
|---|---|---|
| `LOGHARVEST_RECORD_MAX_LINES` | `2000` | 单条记录最多累积的物理行数 |
| `LOGHARVEST_RECORD_MAX_BYTES` | `1048576` | 单条记录最多累积的 UTF-8 字节数（含续行分隔符） |

两个值都必须大于 0；非法值在 Settings 校验阶段启动失败。

超限行为：

1. 当前已累积的记录立即 flush 并参与规则评估，标记 `truncated=True`。
2. 触发超限的续行及其后的续行进入跳过态，不再追加、不再评估，直到下一条起始行或成员结束。
3. 被跳过的物理行仍计入 `processed_lines`，心跳/取消检查照常进行。
4. 该记录产生的命中 `raw_truncated=True`；结构化日志记录一次 `record_oversized`，字段包含 `member_path`、`start_line`、`lines`、`bytes`、`reason`（`max_lines`/`max_bytes`）。
5. 超限不改变任务终态：任务仍可 `succeeded`；如果同一成员还有归档级损坏，按既有 `partial`/`failed` 语义处理。

单行本身超过字节上限时：如果它是起始行，仍开启一条记录（reader 已按 1 MiB 截断单行），记录立即标记 `truncated=True` 并进入跳过态；如果它是续行，则按上面的规则 flush 当前记录并跳过该续行。

### 5.4 无起始行

- preamble 不参与规则评估。
- 一个成员处理完时，如果该成员有行但起始正则一次未命中，记录一次 warning `record_start_not_found`，字段包含 `member_path`、`lines`。这通常意味着起始正则配置错误或文件类型不匹配。
- 不因此把任务判 `partial`/`failed`：空结果可能是合法输入，但必须能在日志中看到。

## 6. 匹配与结果语义

### 6.1 评估

- 多行模式对每条记录调用一次 `evaluate_line(compiled, record.text, record.start_line)`；函数名保留（它评估的是一段文本），不改变关键字/正则/门限实现。
- 多行模式不再逐行评估规则，避免同一条记录重复命中。
- 关键字 `any`/`all` 在整条记录上匹配，可以跨换行。
- 正则使用 `search`；`flags` 语义不变。未开 `m` 时 `^`/`$` 匹配整条记录的边界，开了 `m` 后匹配每一物理行。
- `threshold` 的字段仍来自正则命名分组；数字字段可以出现在记录的任意续行。
- 每条规则对每条记录最多产生一条 `RuleMatch`；不同规则可以分别命中。
- 规则正则超时仍按现有语义跳过该规则并记 warning；日志中的行号使用记录起始行。

### 6.2 命中落库

- `MatchRecord.line_no` = 记录起始行；不新增结束行列。
- `raw_line` = 记录文本按现有 `RAW_LINE_LIMIT`（4096 UTF-8 字节）截断后的内容，可能包含换行。
- `raw_truncated` = 记录因上限/单行截断而未完整累积，或 `raw_line` 存储截断，二者任一为真。
- `processed_lines` = 物理行数（含 preamble、续行和因超限跳过的行）。
- `matched_rows` = 规则命中条数，不是记录数或行数。
- 多行模式下，`line_no` 是记录锚点；实际命中的文本可能位于后续续行。前端列名仍为“行号”，tooltip 说明“多行记录为起始行”。

### 6.3 前端展示

- `matches` 表的 `raw_line` 样式增加 `white-space: pre-wrap`，保留记录内的换行；保留 3 行折叠。
- “已截断”tooltip 改为“原始记录超过存储/匹配上限，后端只保存截断内容（raw_truncated = true）”。
- 不新增列；不新增详情抽屉。

## 7. 组件与数据流

### 7.1 记录组装器

新增 `service/src/application/worker/records.py`：

```python
class RecordAssembler:
    def __init__(
        self,
        start_pattern: regex.Pattern,
        *,
        max_lines: int,
        max_bytes: int,
        start_timeout_ms: int = 50,
    ) -> None: ...

    def feed(self, line: LineRecord) -> list[LogRecord]:
        """消费一条物理行，返回因下一条起始行/超限而完成的记录。"""

    def flush(self) -> list[LogRecord]:
        """成员结束或 EOF 时冲刷当前记录。"""

    @property
    def start_matches(self) -> int: ...

    @property
    def preamble_lines(self) -> int: ...

    def reset_member(self) -> None:
        """成员边界：冲刷并重置统计。"""
```

- `feed` 负责起始行判断、追加、超限 flush 与跳过态。
- `flush` 在成员边界和 EOF 调用。
- `reset_member()` 只重置 `start_matches`/`preamble_lines` 等成员统计；调用前必须先用 `flush()` 处理当前记录。processor 的成员边界顺序是：`flush()` → 读取统计并记录日志 → `reset_member()`。
- 起始正则 `search` 使用 `timeout=start_timeout_ms / 1000`；超时抛 `RecordStartError(member_path, line_no, timeout_ms)`。
- 组装器不依赖数据库/日志；可独立单测。
- `start_matches`/`preamble_lines` 供 processor 在成员结束时记录可观测性日志。

### 7.2 Processor

`TaskProcessor` 增加 `record_max_lines` / `record_max_bytes` 构造参数（默认取 Settings 值，测试可直接覆盖）。处理流程分两条路径：

- **单行模式**：`compiled.record_start is None`，保留现有逐行评估代码，行为与性能不变。
- **多行模式**：
  1. 维护 `current_member`；成员路径变化时先 `flush()` 上一条记录并评估，再读取成员统计记录可观测性日志，最后 `reset_member()`，然后判断新成员是否匹配 `file_glob`。
  2. 对匹配成员调用 `assembler.feed(line)`，对返回的每条记录执行评估并写入共享 buffer。
  3. 循环正常结束后 flush EOF 记录。
  4. 取消/丢租约异常路径不 flush：此时不应再写命中，保持现有取消/租约语义。
  5. 心跳、取消检查、批量写入、租约栅栏、重试清场和成员级 `partial` 汇总全部复用现有逻辑。
  6. 归档成员读取错误导致扫描提前结束时，循环仍正常结束，flush 已累积记录后按 `_member_outcome` 写 `partial`/`failed`。

新增异常处理：

- `RecordStartError` 在 `process` 中优先捕获，写 `failed` 终态并 `retryable=False`，错误信息包含成员路径、行号和超时毫秒数。起始正则超时是规则配置/数据内容问题，重试同一归档只会再次失败。
- 其他规则正则超时仍按现有逻辑跳过单条规则。

### 7.3 Settings 与 Worker 接线

`Settings` 新增：

```python
record_max_lines: int = Field(default=2000, gt=0)
record_max_bytes: int = Field(default=1_048_576, gt=0)
```

`worker/main.py` 把两项传入 `TaskProcessor`。`service/README.md` 的配置表补充对应变量（部署方可在 `.env` 中覆盖）。

### 7.4 API 与前端

- 后端 `RuleSetConfig` 自动获得新字段；`validate_rule_set_config` 通过 `compile_ruleset` 校验。无新增端点。
- `RuleSetDetailRead.config` 是原始 JSON：旧版本可能没有 `record_start_regex` 键；前端按可选字段处理。
- 前端 `RuleSetConfig` 类型新增 `record_start_regex?: string | null`。
- 规则集表单新增“记录起始正则”输入：
  - 留空 → 提交 `null`，单行模式。
  - 回显旧版本时 `source.config.record_start_regex ?? ""`。
  - hint 说明命中起始行后后续行合并、建议用 `^` 锚定、留空保持单行模式。
  - Python 正则语法不在前端校验，交给后端 422 原文回显。
- 不引入 JSON/Monaco 编辑器。

## 8. 错误处理与可观测性

| 场景 | 行为 |
|---|---|
| `record_start_regex` 非法/空白 | 规则集创建（同名新版本）返回 `422`，不写入数据库 |
| 起始正则运行时超时 | 任务 `failed`，`retryable=False`，错误含成员/行号/超时 |
| 记录超过行数/字节上限 | flush 已累积部分，`raw_truncated=True`，跳过后续续行，warning `record_oversized`，任务不失败 |
| 成员内没有起始行 | warning `record_start_not_found`（每成员一次），不改变任务状态 |
| 归档成员损坏 | 先 flush 已累积记录，再按现有成员级 `partial`/`failed` 语义处理 |
| 作业取消/租约丢失 | 不 flush 未完成记录，按现有 `cancelled`/`LeaseLost` 路径处理 |
| 规则正则超时 | 跳过该规则并 warning，沿用现有语义 |

所有日志走 structlog，字段至少包含 `member_path`、`start_line`、`lines`、`bytes`、`reason` 中的适用项。

## 9. 兼容性与发布

### 9.1 兼容性

- `record_start_regex` 缺省 `null`：旧规则集与旧作业完全按单行模式运行，现有后端测试必须全绿（当前 SQLite 全量基线 336 条，PG 集成 15 条另计；M3 新增用例另行统计）。
- 不新增数据库列、不新增 Alembic 迁移；规则集配置本来就是 JSON。
- 修改该字段会生成新的规则集版本；作业固定旧版本，不受影响。
- `matches` API 字段不变；`line_no` 语义从“物理行”扩展为“记录起始行”，单行模式下二者相同。
- 前端旧版本编辑新规则集时，如果其 `RuleSetConfig` 类型缺少字段，提交会丢掉该字段；需要前后端一起升级。

### 9.2 Worker 升级顺序（强制）

旧 Worker 的 Pydantic `RuleSetConfig` 会忽略未知字段；如果先创建多行规则集、后升级 Worker，旧 Worker 会静默按单行模式处理并写入错误结果。

发布顺序必须是：

1. 先滚动升级全部 Worker 到支持 M3 的版本。
2. 再在 Web 上创建/保存带 `record_start_regex` 的规则集。
3. 最后创建使用该规则集的作业。

在 `service/README.md` 的发布/升级说明和前端字段 hint 中标注这一顺序。

## 10. 测试策略

### 10.1 单元测试

新增 `tests/application/worker/test_records.py`，覆盖 `RecordAssembler`：

- 起始行开启记录，续行追加，下一条起始行 flush 上一条。
- preamble 忽略；空行保留；单成员无起始行。
- 成员边界 flush，记录不跨成员。
- EOF flush。
- 行数上限、字节上限、单行超上限、超限后跳过直到下一条起始行。
- `truncated` 标记、`start_matches`/`preamble_lines` 统计。
- 起始正则超时抛 `RecordStartError`。
- Unicode 字节计数与 `\n` 连接。

扩展 `tests/domain/rules/test_evaluator.py`：

- 关键字 `any`/`all` 跨行匹配。
- 正则 `search` 与 `m` flag 在多行文本上的行为。
- 命名分组/数值门限字段来自续行。
- 单行文本结果与现有测试一致。

### 10.2 Processor / API / 前端

- Processor：zip 多行记录只产生一组命中；`line_no` 为起始行；`processed_lines` 为物理行数；成员边界不串记录；超限 warning；起始正则超时任务永久失败；损坏成员 flush 后 `partial`；取消/租约路径不 flush；单行模式回归。
- API：`record_start_regex` 正常保存/回显；非法正则与空白字符串返回 422；旧配置缺字段仍可读取；新配置 `model_dump` 含 `null`。
- 前端：`pnpm typecheck`、`pnpm build`；本机内置浏览器是隐藏页，不做视觉确认，表单以类型检查与接口契约为准。
- PostgreSQL：无新增 SQL；沿用现有 15 条集成测试，不新增迁移测试。

### 10.3 验收命令

```bash
cd service
uv run ruff check .
uv run mypy
uv run pytest -q
LOGHARVEST_TEST_PG_URL="postgresql+psycopg://logharvest:logharvest@127.0.0.1:5433/logharvest" \
  uv run pytest -q tests/integration

cd ../web
pnpm typecheck
pnpm build
```

## 11. 方案取舍

| 决策 | 选择 | 备选与理由 |
|---|---|---|
| 起始正则位置 | 规则集级字段 | 每条规则各自起始会导致同一条日志被不同规则切成不同记录，评估次数和内存不可控 |
| 记录组装 | 流式 `RecordAssembler` | 全文件分组需要读完整文件，违背 500 GB/16 GB 约束 |
| 多行模式下逐行评估 | 不做 | 会重复命中且浪费 CPU；记录级评估一次即可 |
| `line_end_no` | 本版不加 | 避免迁移与前后端列变更；起始行 + raw_line 足够定位，精确跨度后续再加 |
| 超限行为 | 截断已累积部分并跳过直到下一条起始行 | 拆分记录会改变语义且产生重复起始行；失败任务过于严厉 |
| 起始正则超时 | 任务永久失败 | 静默跳过会把应该切分的行并入记录，产生错误结果 |
| preamble | 忽略并 warning | 回退单行会造成同一规则集语义漂移；把 preamble 并入首条记录会污染匹配 |
| 原始文本存储 | 沿用 4096 字节截断 | 保存 1 MiB 记录会让千万级命中表膨胀；字段提取结果仍完整保留 |

## 12. 验收标准

- [ ] 旧规则集（无 `record_start_regex`）行为与当前完全一致，现有测试全绿。
- [ ] 多行模式按成员独立组装，起始行/续行/嵌套起始/空行/EOF/成员边界的语义符合第 5 节。
- [ ] 记录级评估只产生一组规则命中，`line_no` 为起始行，`processed_lines`/`matched_rows` 口径符合第 6 节。
- [ ] 行数/字节上限、单行超限、跳过态、`raw_truncated` 与结构化日志可验证。
- [ ] 起始正则非法返回 422；运行时超时任务 `retryable=False` 失败并带定位信息。
- [ ] 归档成员损坏时先 flush 已累积记录，再按现有 `partial`/`failed` 语义收尾。
- [ ] API 详情对旧/新配置都能返回；前端表单能录入、回显、保存该字段。
- [ ] `matches` 表保留换行展示与截断提示，不新增列。
- [ ] ruff、mypy、SQLite 全量、PG 集成、前端 typecheck/build 全部通过。
- [ ] 文档记录 Worker 升级顺序、限额变量与多行语义。

## 13. 后续扩展

- `line_end_no` 或 `record_line_count` 列，支持精确跨度跳转。
- 样本试跑：粘贴多行样本，返回记录切分与规则命中预览。
- 按规则选择单行/多行模式，或按规则指定不同的起始正则。
- 记录级预过滤（例如先按时间窗口/关键字粗筛）与超限指标上报。
- 记录跨成员/跨文件（需要暂存层，属于另一项架构工作）。
