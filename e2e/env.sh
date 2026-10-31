# shellcheck shell=bash
# LogHarvest E2E 真实环境共享配置（被 up.sh / down.sh / run.sh 以 source 方式引入）。
#
# 这套栈只在开发机上跑，端口全部绑 127.0.0.1，凭据是一次性本地口令——写死是为了
# 让"删库重建"零输入可复现，不要把它们当成任何真实环境的凭据复用。
# 每一项都允许用同名环境变量覆盖：E2E_WEB_HOST_PORT=19080 bash e2e/up.sh

# Git Bash 会把 /nas 这类参数改写成 Windows 路径，wslc 的 -v / --entrypoint 参数必须原样传递。
export MSYS_NO_PATHCONV=1

E2E_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$E2E_DIR/.." && pwd)"

# ---- 容器与网络命名 ---------------------------------------------------------
E2E_NET="${E2E_NET:-lh-e2e-net}"
PG_NAME="${PG_NAME:-lh-e2e-pg}"
SERVER_NAME="${SERVER_NAME:-lh-e2e-server}"
WEB_NAME="${WEB_NAME:-lh-e2e-web}"
WORKER_1_NAME="${WORKER_1_NAME:-lh-e2e-worker-1}"
WORKER_2_NAME="${WORKER_2_NAME:-lh-e2e-worker-2}"

# server 在容器网络里的别名：web/nginx.conf 是 proxy_pass http://server:8000。
API_ALIAS="${API_ALIAS:-server}"

# ---- 镜像标签（与 deploy/*/docker-compose.yml 的 build 目标一一对应）---------
SERVICE_IMAGE="${SERVICE_IMAGE:-lh-e2e/service:e2e}"          # target=server
WORKER_IMAGE="${WORKER_IMAGE:-lh-e2e/service:worker}"          # target=worker
WORKER_V2_IMAGE="${WORKER_V2_IMAGE:-lh-e2e/service:worker-v2}" # 升级演练用的"新版本"镜像
WEB_IMAGE="${WEB_IMAGE:-lh-e2e/web:e2e}"
PG_IMAGE="${PG_IMAGE:-postgres:18.6}"

# Dockerfile 里 FROM 的名字：与生产镜像白名单一致（python:3.12 / node:20 / nginx:1.20 /
# postgres:18.6，见 service/Dockerfile 与 web/Dockerfile）。本机镜像未就位时，可用
# BASE_* 覆盖成镜像站地址（如 docker.m.daocloud.io/library/python:3.12），
# up.sh 的 ensure_base 会把它 tag 回 Dockerfile 期望的名字。
BASE_PYTHON="${BASE_PYTHON:-python:3.12}"
BASE_NODE="${BASE_NODE:-node:20}"
BASE_NGINX="${BASE_NGINX:-nginx:1.20}"

# ---- 宿主端口 ---------------------------------------------------------------
PG_HOST_PORT="${PG_HOST_PORT:-15544}"     # 与开发库 5433 完全隔离，避免误 DROP
API_HOST_PORT="${API_HOST_PORT:-18000}"
WEB_HOST_PORT="${WEB_HOST_PORT:-18080}"

# ---- 应用参数 ---------------------------------------------------------------
PG_USER="${PG_USER:-logharvest}"
PG_PASSWORD="${PG_PASSWORD:-e2e-local-only}"
PG_DB="${PG_DB:-logharvest_e2e}"          # E2E 主库
PG_IT_DB="${PG_IT_DB:-logharvest_e2e_it}" # pytest 集成测试库（夹具会 DROP SCHEMA public）
JWT_SECRET_KEY="${JWT_SECRET_KEY:-e2e-only-jwt-secret-key-please-never-reuse-0123456789}"
ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-E2eAdmin#2026}" # 至少 8 位，settings 在非 dev/test 强制校验
NAS_POLICY="${NAS_POLICY:-enforce}"        # 可写即拒绝处理：真实生产口径
WORKER_VERSION="${WORKER_VERSION:-0.1.0}"
WORKER_V2_VERSION="${WORKER_V2_VERSION:-0.2.0}"

# 只读 NAS 与本地临时盘（named volume，容器内属主 10001）。
NAS_HOST_PATH="${NAS_HOST_PATH:-$(cd "$E2E_DIR/nas" && pwd -W)}"
WORK_VOLUME="${WORK_VOLUME:-lh-e2e-work}"
PG_DATA_VOLUME="${PG_DATA_VOLUME:-lh-e2e-pgdata}"

# server / worker 容器共享的应用环境变量（settings 的 LOGHARVEST_ 前缀）。
# 放在这里而不是 up.sh 里，是为了让后加的第三只 Worker（e2e/worker3.sh）不可能漏掉某一项：
# production 环境下 admin_password 缺失会直接让 Settings 校验失败，Worker 起都起不来。
common_env=(
  -e LOGHARVEST_ENVIRONMENT=production
  -e "LOGHARVEST_DATABASE_URL=postgresql://${PG_USER}:${PG_PASSWORD}@${PG_NAME}:5432/${PG_DB}"
  -e "LOGHARVEST_JWT_SECRET_KEY=${JWT_SECRET_KEY}"
  -e "LOGHARVEST_ADMIN_USERNAME=${ADMIN_USERNAME}"
  -e "LOGHARVEST_ADMIN_PASSWORD=${ADMIN_PASSWORD}"
  -e "LOGHARVEST_NAS_READ_ONLY_POLICY=${NAS_POLICY}"
)

# 测试脚本访问入口：默认走 nginx（与浏览器同一条链路，顺带覆盖 proxy/SSE 配置）。
E2E_API_BASE="${E2E_API_BASE:-http://127.0.0.1:${WEB_HOST_PORT}/api}"
E2E_API_DIRECT="${E2E_API_DIRECT:-http://127.0.0.1:${API_HOST_PORT}/api}"
E2E_WEB_BASE="${E2E_WEB_BASE:-http://127.0.0.1:${WEB_HOST_PORT}/}"
export E2E_API_BASE E2E_API_DIRECT E2E_WEB_BASE ADMIN_USERNAME ADMIN_PASSWORD

wslc_rm() { # wslc_rm <容器名>：存在即删（含停止），不存在不报错。
  local name="$1"
  if wslc inspect "$name" >/dev/null 2>&1; then
    wslc rm -f "$name" >/dev/null 2>&1 || true
  fi
}

wait_http() { # wait_http <url> <超时秒> [额外 curl 参数...]：轮询直到 HTTP 2xx/401/403。
  local url="$1" timeout="${2:-60}" i=0 code
  shift 2
  while [ "$i" -lt "$timeout" ]; do
    # 注意：curl 有时在拿到响应头后仍以非零退出（服务启动瞬间连接被重置等），
    # 直接 `|| echo 000` 会把 "200" 和 "000" 拼成 "200000"，case 永远不匹配 —— 曾因此
    # 误判 "server 启动失败"。这里显式忽略退出码并只取前 3 位状态码。
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$@" "$url" 2>/dev/null || true)"
    code="${code:0:3}"
    case "$code" in
      2?? | 401 | 403) echo "ready: $url (HTTP $code)"; return 0 ;;
    esac
    i=$((i + 2))
    sleep 2
  done
  echo "timeout waiting for $url (last HTTP ${code:-000})" >&2
  return 1
}
