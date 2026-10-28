# agent.md — LogHarvest AI 协作规范

本文件是 AI 助手（ZCode 等）在本仓库工作时的强制协作规范。人类协作者同样建议遵守。

## 一、测试门禁（强制，无例外）

1. **每一次 AI 交互**（包括分析、文档、重构、修 bug、加功能等任何改动），在结束前**必须执行前端与后端的全部单元测试**；只要准备提交代码，提交前必须再次执行，不能引用上一次交互的结果。
2. **全部测试通过后才允许提交代码**（`git commit`）。后端 pytest、前端 vitest 任何一条失败或 error，**不得提交**。
3. **测试未通过时不得提交**：必须先定位根因并修复，然后重新运行测试验证；修复后全部通过，才可重新提交。不得用“与本次改动无关”为由跳过失败用例。
4. 提交前还必须同时通过静态门禁：后端 `ruff check`、`ruff format --check`、`mypy`，前端 `pnpm typecheck`。任一项失败同样不得提交。
5. 跳过（skip）的用例仅限环境依赖的集成测试（如需要真实 PostgreSQL 的用例），且必须在提交说明中注明跳过原因与未覆盖风险。
6. 禁止通过注释、删除或弱化测试断言的方式让测试通过；确需变更既有断言时，必须在提交说明中给出理由与行为影响说明。
7. 新增测试必须可重复执行、无顺序/时间/网络依赖；每个用例自建数据，不得依赖前一个用例的副作用。
8. 每次提交说明必须附带门禁命令与结果摘要（通过数、跳过数、退出码）；没有执行证据不得声称“已验证”。

## 二、测试命令（前端与后端）

在仓库根目录 `E:\GitHub\think` 下：

### 后端（service/，Python 3.12 + pytest）

```bash
cd service
.venv/Scripts/python.exe -m pytest tests -q        # Windows / Git Bash
# 或： .venv/bin/python -m pytest tests -q          # Linux/macOS
```

- 退出码必须为 0，输出中不允许出现 `failed` 或 `error`。
- 快速验证单个模块：`.venv/Scripts/python.exe -m pytest tests/api -q`
- 静态检查（提交前建议）：`.venv/Scripts/python.exe -m ruff check src tests`

### 前端（web/，pnpm + vitest + vue-tsc）

```bash
cd web
pnpm test                # vitest run（单元测试）
pnpm typecheck           # vue-tsc --noEmit --skipLibCheck
```

- `pnpm test` 退出码必须为 0；`pnpm typecheck` 不得有类型错误。

### 一次性完整门禁（提交前必须执行）

```bash
cd service \
  && .venv/Scripts/python.exe -m pytest tests -q \
  && .venv/Scripts/python.exe -m ruff check src tests scripts \
  && .venv/Scripts/python.exe -m ruff format --check src tests \
  && .venv/Scripts/python.exe -m mypy \
  && cd ../web \
  && pnpm test \
  && pnpm typecheck
```

## 三、提交纪律

1. 提交前必须运行上述一次性完整门禁；全绿才提交，并在提交说明中记录通过/跳过数量。
2. 提交信息使用约定式前缀（`feat:` / `fix:` / `refactor:` / `test:` / `docs:` / `chore:`），一句话说明行为变化。
3. 每次提交应保持单一意图；混合「重构 + 行为变更」的提交不被接受。
4. 重构类提交必须证明行为不变：以测试全绿 + 无断言修改为证据；行为变更类提交必须新增或修改断言，并在说明中写明影响。
5. 测试失败时的固定顺序：复现 → 定位根因 → 修复 → 重跑全量门禁 → 记录结果；禁止先提交再补测试。

## 四、其他约定

1. 后端遵循现有分层：`api`（路由/序列化）→ `application`（用例服务）→ `domain`（纯业务）→ `infrastructure`（持久化/队列）。路由层不写业务逻辑，domain 不依赖框架。
2. 前端业务代码位于 `web/src/views/{logharvest,rules,system}`、`web/src/api`、`web/src/utils/http`；纯逻辑抽到 `.ts` 模块以便单测。
3. 新增/修改接口时，同步维护后端 schema、前端 TS 类型与双方测试，保持契约一致。
4. 涉及数据库变更必须走 Alembic 迁移，并在迁移内保持向后兼容（先加后删）。
