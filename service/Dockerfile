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

# 非 root 运行（uid/gid 10001，与 deploy/init-dirs.sh 的数据盘属主规划一致）：
# server 无本地写需求；worker 只写 LOGHARVEST_WORK_DIR 挂载点（属主 10001）。
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --create-home app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY src ./src
COPY alembic ./alembic
COPY scripts ./scripts
COPY alembic.ini ./

# scripts/ 必须进镜像：部署文档里的管理员种子命令
#   docker compose ... exec server python -m scripts.seed_admin
# 依赖它。应用启动只种 RBAC（菜单/角色），不建 admin 用户（见 src/main.py lifespan），
# 少了这个目录，全新环境起来后没有任何账号能登录，而镜像内又无法补救（只能改库）。

# alembic.ini 在 /app，run_migrations 用相对路径定位它；工作目录即 /app
FROM base AS server
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2).status == 200 else 1)"
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS worker
# Worker 需要 /proc/self/mounts 判定 NAS 只读（Linux 上可用）；临时盘挂载点由
# compose 提供，宿主机侧需 chown 10001:10001（deploy/init-dirs.sh 已处理）
#
# 挂载点必须在镜像里预先建好并归 app：Docker 首次用 named volume 挂载镜像内不存在的
# 路径时，会以 root:root 755 建目录，uid 10001 的 Worker 随后 mkdir <work_dir>/logharvest
# 直接 EACCES——每个文件任务重试到 max_attempts 全灭，作业 failed，命中恒为 0，而 health
# 与心跳一切正常，故障面只在任务 error 字段里。bind 挂载（生产口径）不受影响，
# 但 named volume / 忘做 init-dirs 的环境会静默废掉整条抓取链路。
RUN mkdir -p /local-tmp && chown app:app /local-tmp
USER app
CMD ["python", "-m", "src.worker"]
