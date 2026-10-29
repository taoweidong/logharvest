#!/usr/bin/env bash
# LogHarvest E2E 真实环境拉起脚本（wslc，无 docker compose）。
#
# 与生产 deploy/*\/*.yml 三个栈一一对应，只是把 compose 的编排换成 wslc 命令序列：
#   postgres 栈 -> $PG_NAME（外部网络 + 只绑 127.0.0.1）
#   server   栈 -> $SERVER_NAME（网络别名 server，供 web/nginx.conf proxy_pass）+ $WEB_NAME
#   worker   栈 -> $WORKER_1_NAME / $WORKER_2_NAME（NAS 只读 bind mount + 临时盘卷）
#
# 用法：
#   bash e2e/up.sh              # 基础镜像就位后构建 + 拉起全栈（幂等，可重复执行）
#   bash e2e/up.sh --fresh      # 先 down（含数据卷）再拉起，得到全新库
#   bash e2e/up.sh --no-build   # 复用已有镜像，只重建容器
#   bash e2e/up.sh --skip-web   # 不构建/不起前端（只跑 API 流程时用；node 基础镜像还没到位时可先解耦）
set -euo pipefail

E2E_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=e2e/env.sh
source "$E2E_DIR/env.sh"

NO_BUILD=0
FRESH=0
SKIP_WEB=0
for arg in "$@"; do
  case "$arg" in
    --no-build) NO_BUILD=1 ;;
    --fresh) FRESH=1 ;;
    --skip-web) SKIP_WEB=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

hostpath() { (cd "$1" && pwd -W); } # POSIX 目录 -> wslc 认的 Windows 盘符路径
hostfile() { local d; d="$(dirname "$1")"; printf '%s/%s' "$(cd "$d" && pwd -W)" "$(basename "$1")"; } # 同上，用于文件
step() { echo; echo "==== $* ===="; }

# 防并发：同时跑两个 up.sh 会互相截断日志、抢构建上下文（wslc 会报 Device or resource busy）。
LOCK_DIR="$E2E_DIR/artifacts/.up.lock"
mkdir -p "$(dirname "$LOCK_DIR")" 2>/dev/null || true
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "另一个 up.sh 仍在运行（锁目录 $LOCK_DIR 存在）。" >&2
  echo "确认它已死掉后再执行：rmdir \"$(hostpath "$LOCK_DIR")\"" >&2
  exit 3
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

# 前端构建方式：host = 宿主 pnpm build + nginx-only 镜像（默认，node 基础镜像本机拉不动）
#                docker = 走 web/Dockerfile 的 node 多阶段构建（需 node:22-bookworm-slim）
E2E_WEB_BUILD="${E2E_WEB_BUILD:-host}"

if [ "$FRESH" = "1" ]; then
  step "fresh: 先拆栈"
  bash "$E2E_DIR/down.sh" --volumes
fi

# ---- 1. 基础镜像 ------------------------------------------------------------
# 本机直连 registry-1.docker.io 会超时，所以先走镜像站，再 tag 回 Dockerfile 里的名字。
ensure_base() { # ensure_base <镜像站名> <Dockerfile 期望名>
  local mirror="$1" wanted="$2"
  if wslc image inspect "$wanted" >/dev/null 2>&1; then
    echo "base ok   : $wanted"
    return
  fi
  if ! wslc image inspect "$mirror" >/dev/null 2>&1; then
    echo "pulling   : $mirror"
    wslc pull "$mirror"
  fi
  wslc tag "$mirror" "$wanted"
  echo "base tagged: $mirror -> $wanted"
}

step "1/5 基础镜像"
ensure_base "$BASE_UV" "ghcr.io/astral-sh/uv:python3.12-bookworm-slim"
ensure_base "$BASE_PYTHON" "python:3.12-slim-bookworm"
ensure_base "$BASE_NGINX" "nginx:1.27-alpine"
if [ "$SKIP_WEB" = "0" ] && [ "$E2E_WEB_BUILD" = "docker" ]; then
  ensure_base "$BASE_NODE" "node:22-bookworm-slim"
fi
if ! wslc image inspect "$PG_IMAGE" >/dev/null 2>&1; then
  echo "pulling   : $PG_IMAGE"
  wslc pull "$PG_IMAGE"
fi

# ---- 2. 镜像构建 ------------------------------------------------------------
# 构建上下文不用 service/ 本体，而用它的裁剪副本 e2e/artifacts/ctx/service：
#   1) service/uv.lock 里 1389 处依赖 URL 锁死在 mirrors.aliyun.com，该镜像站今天
#      对本机所有路径返回 403，`uv sync --frozen` 必失败；UV_DEFAULT_INDEX 不能重写
#      已锁定的 URL。这里只在副本里把源改写成 files.pythonhosted.org / pypi.org
#      （版本与 sha256 完全不变，已抽样实测下载校验哈希一致），生产 uv.lock 保持不动。
#   2) 顺带按 service/.dockerignore 裁剪，避免把 165MB .venv、SQLite、日志塞进上下文。
CTX_ROOT="$E2E_DIR/artifacts/ctx"
# 上下文目录带 RUN_ID 后缀，每次运行都是全新目录，绝不 rm 上一次可能仍被
# wslc.exe 句柄占用的文件（实测 rm -rf 会 Device or resource busy 并让 set -e 直接退出）。
RUN_ID="${RUN_ID:-$(date +%Y%m%d-%H%M%S)}"
SERVICE_CTX_DIR="$CTX_ROOT/service-$RUN_ID"
WEB_CTX_DIR="$CTX_ROOT/web-$RUN_ID"

prune_old_ctx() { # 尽力回收旧上下文，busy 就留着，反正下次换目录
  local dir
  for dir in "$CTX_ROOT"/service-* "$CTX_ROOT"/web-*; do
    [ -e "$dir" ] || continue
    if [ "$dir" != "$SERVICE_CTX_DIR" ] && [ "$dir" != "$WEB_CTX_DIR" ]; then
      rm -rf "$dir" 2>/dev/null || true
    fi
  done
}

prepare_service_ctx() {
  mkdir -p "$SERVICE_CTX_DIR"
  tar -C "$REPO_DIR/service" \
    --exclude='./.venv' --exclude='./tests' --exclude='./.env' --exclude='./.coverage' \
    --exclude='./coverage.json' --exclude='./README.md' --exclude='./Dockerfile' \
    --exclude='./scripts/__pycache__' --exclude='*/__pycache__' --exclude='*.py[cod]' \
    --exclude='*/.pytest_cache' --exclude='*/.mypy_cache' --exclude='*/.ruff_cache' \
    --exclude='*/.hypothesis' --exclude='*.db' --exclude='*.log' --exclude='*.pid' \
    -cf - . | tar -C "$SERVICE_CTX_DIR" -xf -
  # 注意路径转换：uv 跑的是原生 Windows python，MSYS 的 /e/... 它读不到，必须给盘符路径。
  (cd "$REPO_DIR" && PYTHONIOENCODING=utf-8 uv run --python 3.12 python "$(hostpath "$E2E_DIR")/mirror_lock.py" --dst "$(hostpath "$SERVICE_CTX_DIR")/uv.lock")
  echo "ctx ready : $(hostpath "$SERVICE_CTX_DIR")"
}

step "2/5 镜像构建"
if [ "$NO_BUILD" = "1" ]; then
  echo "skip build (--no-build)"
else
  mkdir -p "$CTX_ROOT"
  prune_old_ctx
  prepare_service_ctx
  SERVER_CTX="$(hostpath "$SERVICE_CTX_DIR")"
  # 上下文里没有 Dockerfile 副本（避免旧句柄占用同一文件），显式 -f 指生产 Dockerfile。
  SERVICE_DOCKERFILE="$(hostfile "$REPO_DIR/service/Dockerfile")"
  time wslc build --target server -f "$SERVICE_DOCKERFILE" -t "$SERVICE_IMAGE" "$SERVER_CTX"
  time wslc build --target worker -f "$SERVICE_DOCKERFILE" -t "$WORKER_IMAGE" "$SERVER_CTX"
  # 升级演练用的"新版本"镜像：同一份代码 + 一个构建期标记文件，
  # 证明 worker 是换了镜像被替换，而不只是改了环境变量。
  time wslc build -t "$WORKER_V2_IMAGE" -f "$(hostpath "$E2E_DIR")/Dockerfile.worker-v2" "$SERVER_CTX"
  if [ "$SKIP_WEB" = "0" ]; then
    if [ "$E2E_WEB_BUILD" = "docker" ]; then
      time wslc build -t "$WEB_IMAGE" "$(hostpath "$REPO_DIR/web")"
    else
      # 宿主构建前端产物，镜像只负责用同一份 nginx.conf 发布 dist（见 e2e/Dockerfile.web）。
      step "2b 前端宿主构建（pnpm）"
      (cd "$REPO_DIR/web" && pnpm install --frozen-lockfile && pnpm build)
      mkdir -p "$WEB_CTX_DIR"
      cp -f "$REPO_DIR/web/nginx.conf" "$WEB_CTX_DIR/nginx.conf"
      cp -r "$REPO_DIR/web/dist" "$WEB_CTX_DIR/dist"
      time wslc build -t "$WEB_IMAGE" -f "$(hostpath "$E2E_DIR")/Dockerfile.web" "$(hostpath "$WEB_CTX_DIR")"
    fi
  else
    echo "skip web image build (--skip-web)"
  fi
fi

# ---- 3. 网络与卷 ------------------------------------------------------------
step "3/5 网络与卷"
wslc network create "$E2E_NET" >/dev/null 2>&1 || true
wslc volume create "$PG_DATA_VOLUME" >/dev/null 2>&1 || true
wslc volume create "$WORK_VOLUME" >/dev/null 2>&1 || true
echo "network   : $E2E_NET"
echo "volumes   : $PG_DATA_VOLUME, $WORK_VOLUME"

# 临时盘卷属主：named volume 若早在"镜像里没有 /local-tmp"的版本下建出来，就是
# root:root 755；Worker 以 uid 10001 跑，mkdir <work_dir>/logharvest 直接 EACCES，
# 于是每个文件任务重试满 max_attempts 后全灭、命中恒为 0，而 /api/health 与心跳一切正常。
# 镜像侧已修（service/Dockerfile worker 段预建 /local-tmp），这里再兜一次：
# 已存在的卷不会因为镜像变了而重新初始化，必须显式 chown。生产用 bind 挂载，
# 属主由 deploy/init-dirs.sh 负责，同一套约束。
step "3b 临时盘卷属主（uid 10001 可写）"
wslc run --rm --user root -v "$WORK_VOLUME:/local-tmp" "$SERVICE_IMAGE" \
  sh -c 'mkdir -p /local-tmp && chown -R 10001:10001 /local-tmp && chmod 775 /local-tmp' >/dev/null
echo "work volume: 10001:10001 0775"

# ---- 4. 数据库 --------------------------------------------------------------
step "4/5 PostgreSQL（宿主 127.0.0.1:$PG_HOST_PORT）"
wslc_rm "$PG_NAME"
wslc run -d --name "$PG_NAME" --network "$E2E_NET" \
  -e POSTGRES_USER="$PG_USER" -e POSTGRES_PASSWORD="$PG_PASSWORD" \
  -e POSTGRES_DB="$PG_DB" -e PGDATA=/var/lib/postgresql/data/pgdata \
  -v "$PG_DATA_VOLUME:/var/lib/postgresql/data" \
  -p "127.0.0.1:${PG_HOST_PORT}:5432" \
  --shm-size 256mb \
  "$PG_IMAGE" >/dev/null

for i in $(seq 1 60); do
  if wslc exec "$PG_NAME" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; then break; fi
  sleep 2
done
wslc exec "$PG_NAME" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1 || {
  echo "postgres 未就绪" >&2; wslc logs --tail 40 "$PG_NAME" >&2; exit 1;
}
echo "pg ready  : $PG_DB"
# pytest 集成测试夹具会 DROP SCHEMA public，只能给它一个专用的一次性库，绝不复用主库。
wslc exec "$PG_NAME" createdb -U "$PG_USER" "$PG_IT_DB" >/dev/null 2>&1 || echo "pg ready  : $PG_IT_DB already exists"

# ---- 5. Server / Worker / Web ----------------------------------------------
step "5/5 应用栈"

# common_env 已上移到 env.sh（与 e2e/worker3.sh 共用，见那里的注释）。

wslc_rm "$SERVER_NAME"
# server 也挂 /nas:ro：创建作业时的只读守卫要在同一台"机器"上看得到挂载表，
# 否则 enforce 策略只会得到 unknown。
wslc run -d --name "$SERVER_NAME" --hostname "$SERVER_NAME" \
  --network "$E2E_NET" --network-alias "$API_ALIAS" \
  "${common_env[@]}" \
  -e LOGHARVEST_AUTO_CREATE_TABLES=true \
  -e "LOGHARVEST_TRUSTED_PROXY_CIDRS=${TRUSTED_PROXY_CIDRS:-}" \
  -v "$NAS_HOST_PATH:/nas:ro" \
  -v "$WORK_VOLUME:/local-tmp" \
  -p "127.0.0.1:${API_HOST_PORT}:8000" \
  "$SERVICE_IMAGE" >/dev/null

wait_http "http://127.0.0.1:${API_HOST_PORT}/api/health" 120 || {
  echo "server 启动失败" >&2; wslc logs --tail 60 "$SERVER_NAME" >&2; exit 1;
}
echo "server ok : http://127.0.0.1:${API_HOST_PORT}/api"

# 管理员账号：应用启动只跑 Alembic + RBAC 种子（菜单/角色），**不建用户**
# （service/src/main.py lifespan；契约 §2.5）。少了这一步，全新环境里
# POST /api/login 永远 401 "invalid credentials"，整条流程卡在 P1。
step "5a 管理员种子（scripts.seed_admin，幂等）"
if ! wslc exec "$SERVER_NAME" python -m scripts.seed_admin; then
  echo "seed_admin 失败（镜像里是否有 /app/scripts？见 service/Dockerfile）" >&2
  wslc logs --tail 40 "$SERVER_NAME" >&2
  exit 1
fi
# 种子只建号，不会覆盖已存在用户的密码：改过 ADMIN_PASSWORD 又复用旧数据卷时必然登录失败，
# 这里用一次真实登录把这种"环境状态漂移"变成明确报错，而不是让后续流程莫名 401。
probe="$(curl -s --max-time 15 -X POST "http://127.0.0.1:${API_HOST_PORT}/api/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"${ADMIN_USERNAME}\",\"password\":\"${ADMIN_PASSWORD}\"}" || true)"
case "$probe" in
  *'"code":0'*) echo "login ok  : $ADMIN_USERNAME" ;;
  *)
    echo "登录探针失败：$probe" >&2
    echo "多半是数据卷里已有同名用户、但口令与当前 ADMIN_PASSWORD 不同（seed_admin 不改已有密码）。" >&2
    echo "解决：bash e2e/down.sh --volumes && bash e2e/up.sh（全新库），或固定历史 ADMIN_PASSWORD。" >&2
    exit 1 ;;
esac

start_worker() { # start_worker <容器名> <镜像> <上报版本号>
  local name="$1" image="$2" version="$3"
  wslc_rm "$name"
  wslc run -d --name "$name" --hostname "$name" --network "$E2E_NET" \
    "${common_env[@]}" \
    -e "LOGHARVEST_WORKER_VERSION=${version}" \
    -e LOGHARVEST_WORK_DIR=/local-tmp \
    -e LOGHARVEST_MATCHES_BATCH_SIZE=1000 \
    -e LOGHARVEST_MAX_MATCHES_PER_TASK=200000 \
    -e LOGHARVEST_WORKER_POLL_SECONDS=1.0 \
    -v "$NAS_HOST_PATH:/nas:ro" \
    -v "$WORK_VOLUME:/local-tmp" \
    "$image" >/dev/null
  echo "worker ok : $name ($image, version=$version)"
}

start_worker "$WORKER_1_NAME" "$WORKER_IMAGE" "$WORKER_VERSION"
start_worker "$WORKER_2_NAME" "$WORKER_IMAGE" "$WORKER_VERSION"

# 起跑前硬校验临时盘可写。这个探针换来的排查成本差好几个数量级：属主不对时
# 栈照样"健康"，但所有作业 failed、命中恒为 0，要一路翻到 file_task.error 才看得见。
for name in "$WORKER_1_NAME" "$WORKER_2_NAME"; do
  if ! wslc exec "$name" touch /local-tmp/.write-probe >/dev/null 2>&1; then
    echo "$name 写不进 /local-tmp（临时盘属主问题）" >&2
    wslc exec "$name" sh -c 'id; ls -ld /local-tmp' >&2 || true
    echo "处理：bash e2e/down.sh --volumes && bash e2e/up.sh，或看上面的 step 3b" >&2
    exit 1
  fi
done
wslc exec "$WORKER_1_NAME" rm -f /local-tmp/.write-probe >/dev/null 2>&1 || true
echo "workdir   : /local-tmp 可写（uid $(wslc exec "$WORKER_1_NAME" id -u 2>/dev/null | tr -d '\r')）"

if [ "$SKIP_WEB" = "0" ]; then
  wslc_rm "$WEB_NAME"
  wslc run -d --name "$WEB_NAME" --network "$E2E_NET" \
    -p "127.0.0.1:${WEB_HOST_PORT}:8080" \
    "$WEB_IMAGE" >/dev/null

  wait_http "$E2E_WEB_BASE" 90 || {
    echo "web 启动失败" >&2; wslc logs --tail 40 "$WEB_NAME" >&2; exit 1;
  }
else
  wslc_rm "$WEB_NAME"
  echo "web skipped (--skip-web)：API 请直接用 $E2E_API_DIRECT"
fi

echo
echo "==== E2E 栈就绪 ===="
echo "前端 / API 代理 : $E2E_WEB_BASE"
echo "API 直连        : $E2E_API_DIRECT"
echo "PostgreSQL      : 127.0.0.1:$PG_HOST_PORT/$PG_DB（集成测试库 $PG_IT_DB）"
echo "NAS（只读）     : $NAS_HOST_PATH -> /nas"
echo "登录账号        : $ADMIN_USERNAME / $ADMIN_PASSWORD"
echo "健康检查        : $E2E_API_DIRECT/health  /  $E2E_API_DIRECT/health/ready"
echo "worker 数       : 2（版本 $WORKER_VERSION，升级演练用 $WORKER_V2_VERSION）"
if [ "$SKIP_WEB" = "1" ]; then
  echo "测试用 API base : export E2E_API_BASE=$E2E_API_DIRECT"
else
  echo "测试用 API base : $E2E_API_BASE（走 nginx 代理，与前端同源）"
fi
