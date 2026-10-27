# agent.md — LogHarvest AI 协作规范

本文件是 AI 助手（ZCode 等）在本仓库工作时的强制协作规范。人类协作者同样建议遵守。

## 一、测试门禁（强制，无例外）

1. **每一次 AI 交互**（无论是修 bug、加功能、重构还是改文档之外的任何代码变更），在允许提交代码之前，**必须先执行前端与后端的全部单元测试**。
2. **全部测试通过后才允许提交代码**（`git commit`）。存在任何一条失败用例时，**不得提交**。
3. **测试未通过时不得提交**：必须先定位失败原因并修复，然后重新运行测试验证；修复后全部通过，才可重新提交。
4. 跳过（skip）的用例仅限环境依赖的集成测试（如需要真实 PostgreSQL 的用例），且必须在提交说明中注明跳过原因。
5. 禁止通过注释、删除或弱化测试断言的方式让测试通过；确需变更既有断言时，必须在提交说明中给出理由与行为影响说明。

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

### 一次性完整门禁（推荐在提交前执行）

```bash
cd service && .venv/Scripts/python.exe -m pytest tests -q && cd ../web && pnpm test && pnpm typecheck
```

## 三、提交纪律

1. 提交前先运行上述门禁；全绿才提交。
2. 提交信息使用约定式前缀（`feat:` / `fix:` / `refactor:` / `test:` / `docs:` / `chore:`），一句话说明行为变化。
3. 每次提交应保持单一意图；混合「重构 + 行为变更」的提交不被接受。
4. 重构类提交必须证明行为不变：以测试全绿 + 无断言修改为证据。

## 四、其他约定

1. 后端遵循现有分层：`api`（路由/序列化）→ `application`（用例服务）→ `domain`（纯业务）→ `infrastructure`（持久化/队列）。路由层不写业务逻辑，domain 不依赖框架。
2. 前端业务代码位于 `web/src/views/{logharvest,rules,system}`、`web/src/api`、`web/src/utils/http`；纯逻辑抽到 `.ts` 模块以便单测。
3. 新增/修改接口时，同步维护后端 schema、前端 TS 类型与双方测试，保持契约一致。
4. 涉及数据库变更必须走 Alembic 迁移，并在迁移内保持向后兼容（先加后删）。
