#!/usr/bin/env bash
# 拆除 LogHarvest E2E 栈。默认保留数据卷（便于反复调试）；--volumes 连数据一起清。
#
#   bash e2e/down.sh              # 停容器，保留 pgdata / work 卷和网络
#   bash e2e/down.sh --volumes    # 连数据库与临时盘一起删，下次 up.sh 得到全新环境
set -euo pipefail

E2E_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=e2e/env.sh
source "$E2E_DIR/env.sh"

WITH_VOLUMES=0
for arg in "$@"; do
  case "$arg" in
    --volumes) WITH_VOLUMES=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

# 停止顺序与生产一致：先摘流量（worker 停止领任务），再前端，最后 API 与数据库。
for name in "$WORKER_1_NAME" "$WORKER_2_NAME" "$WEB_NAME" "$SERVER_NAME" "$PG_NAME"; do
  if wslc inspect "$name" >/dev/null 2>&1; then
    echo "removing  : $name"
    wslc rm -f "$name" >/dev/null 2>&1 || echo "  !! $name 删除失败（检查 wslc ps -a）" >&2
  else
    echo "absent    : $name"
  fi
done

if [ "$WITH_VOLUMES" = "1" ]; then
  for vol in "$WORK_VOLUME" "$PG_DATA_VOLUME"; do
    if wslc volume inspect "$vol" >/dev/null 2>&1; then
      echo "removing  : volume $vol"
      wslc volume rm "$vol" >/dev/null 2>&1 || echo "  !! volume $vol 删除失败" >&2
    fi
  done
fi

if [ "$WITH_VOLUMES" = "1" ] || [ -z "$(wslc ps -q --filter network="$E2E_NET" 2>/dev/null)" ]; then
  if wslc network inspect "$E2E_NET" >/dev/null 2>&1; then
    echo "removing  : network $E2E_NET"
    wslc network rm "$E2E_NET" >/dev/null 2>&1 || echo "  !! network 删除失败（还有容器挂在上面）" >&2
  fi
fi

echo "done      : E2E 栈已拆除（NAS 数据集 $E2E_DIR/nas 未删除，需要重建请跑 python e2e/make_nas.py）"
