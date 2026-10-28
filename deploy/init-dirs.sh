#!/usr/bin/env bash
# LogHarvest 数据盘初始化：在 /usr1 下规划目录并创建跨栈 Docker 网络。
# 幂等：可重复执行；仅首次创建目录、设置属主与网络。
#
# 用法：sudo bash deploy/init-dirs.sh [--base /usr1/logharvest]
#
# 目录规划（所有可归档数据一律落数据盘 /usr1）：
#   /usr1/logharvest/postgres/data    PostgreSQL 数据目录（PG alpine 镜像 uid 70）
#   /usr1/logharvest/postgres/backup  pg_dump 归档（Jenkins 升级前备份 + 每日定时）
#   /usr1/logharvest/worker/tmp       Worker 解压临时盘（按任务隔离，任务结束自动清理）
#   /usr1/logharvest/server/logs      Server 日志归档位（当前日志走 stdout，可接收集器落盘）

set -euo pipefail

BASE="/usr1/logharvest"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --base) BASE="$2"; shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "请用 root/sudo 运行（需要在 /usr1 建目录并 chown）" >&2
  exit 1
fi

echo "==> 规划数据盘目录：$BASE"
mkdir -p \
  "$BASE/postgres/data" \
  "$BASE/postgres/backup" \
  "$BASE/worker/tmp" \
  "$BASE/server/logs"

# postgres:16-alpine 的服务账户是 uid/gid 70；数据与备份目录必须归它所有
echo "==> chown 70:70 postgres/{data,backup}"
chown 70:70 "$BASE/postgres/data" "$BASE/postgres/backup"

# 临时盘与日志目录归 root（容器内 worker 以 root 运行时可写即可）
chmod 750 "$BASE/worker/tmp" "$BASE/server/logs"

echo "==> 创建跨栈外部网络 logharvest-net"
if docker network inspect logharvest-net >/dev/null 2>&1; then
  echo "    已存在，跳过"
else
  docker network create logharvest-net
fi

echo "==> 完成。目录清单："
ls -la "$BASE" "$BASE/postgres" "$BASE/worker" "$BASE/server"
echo
echo "下一步："
echo "  1) cp deploy/{postgres,server,worker}/.env.example 对应目录/.env 并填写"
echo "  2) docker compose -f deploy/postgres/docker-compose.yml --env-file deploy/postgres/.env up -d"
echo "  3) 等待 healthy 后：docker compose -f deploy/server/docker-compose.yml --env-file deploy/server/.env up -d"
echo "  4) docker compose -f deploy/worker/docker-compose.yml --env-file deploy/worker/.env up -d --scale worker=4"
