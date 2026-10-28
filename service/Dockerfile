# LogHarvest 控制面 API / Worker 共用镜像（多阶段）。
# 构建上下文 = service/；server 与 worker 通过 --target 选择。
#   docker build -f service/Dockerfile --target server -t logharvest-server ./service
#   docker build -f service/Dockerfile --target worker -t logharvest-worker ./service

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

# 先只拷贝依赖清单，利用层缓存：uv.lock 变了才重装依赖
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --compile-bytecode

FROM python:3.12-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./

# alembic.ini 在 /app，run_migrations 用相对路径定位它；工作目录即 /app
FROM base AS server
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2).status == 200 else 1)"
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS worker
# Worker 需要 /proc/self/mounts 判定 NAS 只读（Linux 上可用）；临时盘挂载点由 compose 提供
CMD ["python", "-m", "src.worker"]
