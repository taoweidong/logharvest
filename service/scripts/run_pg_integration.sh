#!/usr/bin/env bash
# 一次性 PostgreSQL 上跑空库迁移 + 关键索引断言 + 集成套件。
#
# 用途：Jenkins 的 PostgreSQL 门禁；本地有 Docker 时也可直接执行：
#   cd service && bash scripts/run_pg_integration.sh
#
# 容器与数据都是一次性的：脚本退出（含失败）时删除容器；不接触任何既有库。
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null 2>&1; then
    echo "docker is required for the PostgreSQL integration gate" >&2
    exit 1
fi

CONTAINER="${LOGHARVEST_PG_CONTAINER:-logharvest-ci-pg-$$}"
IMAGE="${LOGHARVEST_PG_IMAGE:-postgres:16}"
DB_USER="${LOGHARVEST_PG_USER:-logharvest_ci}"
DB_PASSWORD="${LOGHARVEST_PG_PASSWORD:-logharvest-ci-password-0123456789}"
DB_NAME="${LOGHARVEST_PG_DB:-logharvest_ci}"

cleanup() {
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# 随机宿主端口，避免与本机/其他构建的 5432 冲突。
docker run -d --rm \
    --name "$CONTAINER" \
    -e POSTGRES_USER="$DB_USER" \
    -e POSTGRES_PASSWORD="$DB_PASSWORD" \
    -e POSTGRES_DB="$DB_NAME" \
    -p 127.0.0.1::5432 \
    "$IMAGE" >/dev/null

PORT=""
for _ in $(seq 1 60); do
    PORT=$(docker port "$CONTAINER" 5432/tcp 2>/dev/null | head -n 1 | cut -d: -f2 || true)
    if [[ -n "$PORT" ]] && docker exec "$CONTAINER" pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

if [[ -z "$PORT" ]] || ! docker exec "$CONTAINER" pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; then
    echo "PostgreSQL did not become ready; container logs:" >&2
    docker logs "$CONTAINER" >&2 || true
    exit 1
fi

# 固定测试环境与测试密钥：不受 Jenkins 宿主机上残留的 LOGHARVEST_* 影响。
export LOGHARVEST_ENVIRONMENT=test
export LOGHARVEST_JWT_SECRET_KEY="ci-only-secret-key-0123456789-abcdef"
export LOGHARVEST_DATABASE_URL="postgresql+psycopg://${DB_USER}:${DB_PASSWORD}@127.0.0.1:${PORT}/${DB_NAME}"
export LOGHARVEST_TEST_PG_URL="$LOGHARVEST_DATABASE_URL"

# 空库升级：验证迁移链本身，而不是只验证 create_all。
uv run --python 3.12 alembic upgrade head

uv run --python 3.12 python - <<'PY'
import os

from sqlalchemy import create_engine, inspect

url = os.environ["LOGHARVEST_DATABASE_URL"]
inspector = inspect(create_engine(url))
tables = set(inspector.get_table_names())
assert {"alembic_version", "file_tasks", "matches", "jobs"} <= tables, tables
indexes = {index["name"] for index in inspector.get_indexes("file_tasks")}
assert {"ix_file_tasks_claim", "ix_file_tasks_recover"} <= indexes, indexes
print("PostgreSQL migration smoke passed")
PY

uv run --python 3.12 pytest tests/integration -q
