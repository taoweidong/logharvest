#!/usr/bin/env bash
# 一次性"紧预算 Worker"（第 3 个容器），只为 e2e/limits_flow.py 的解压预算 / 命中上限用例存在。
#
# 为什么用独立容器而不是改现有 Worker：默认预算（1GiB 单成员 / 10000 成员 / 20 万命中）
# 下 api_flow 已经把 limits、flood 判成 succeeded 并全量入库，那才是"正常环境"的口径。
# 要验证的是**越界分支**：把四个预算压到夹具的尺寸附近，才能在同一份 NAS 上同时得到
# partial（单成员/成员数超限，只牺牲该成员）和 failed+清场（命中数超限）两种结局。
# 所以这个容器必须与两个默认 Worker 同时在线、又能通过 drain 独占任务。
#
# 用法：
#   bash e2e/worker3.sh start    # 拉起（存在即先删，幂等）
#   bash e2e/worker3.sh stop     # 删除容器（默认 Worker 接管剩余用例）
#   bash e2e/worker3.sh status   # 打印容器状态
#   bash e2e/worker3.sh budget   # 打印预算常量（KEY=VALUE，limits_flow.py 直接读，避免两处写死）
set -euo pipefail

E2E_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=e2e/env.sh
source "$E2E_DIR/env.sh"

WORKER_3_NAME="${WORKER_3_NAME:-lh-e2e-worker-3}"
WORKER_3_VERSION="${WORKER_3_VERSION:-0.1.0-budget}"

# ---- 预算常量（夹具尺寸见 e2e/make_nas.py 的 build_limits / build_flood）------
# big_member.zip 的 huge.log 解压后 3MiB（LIMITS_HUGE_MEMBER_BYTES），压到 1MiB 必然越界；
# many_members.zip 有 50 个成员，max_members=20 在第 21 个成员处停止整包扫描；
# flood.zip 有 6500 个命中，max_matches_per_task=100 在第 101 个命中处触发失败清场；
# matches_batch_size=30 让命中在触顶前已经真实落库 90 行，清场才不是"本来就没写"。
BUDGET_ENV=(
  -e LOGHARVEST_ARCHIVE_MAX_MEMBER_BYTES=1048576
  -e LOGHARVEST_ARCHIVE_MAX_MEMBERS=20
  -e LOGHARVEST_MAX_MATCHES_PER_TASK=100
  -e LOGHARVEST_MATCHES_BATCH_SIZE=30
)

budget() {
  # BUDGET_ENV 里 "-e" 与 KEY=VALUE 是两个数组元素，只打印等号那种。
  for pair in "${BUDGET_ENV[@]}"; do
    case "$pair" in
      *=*) printf '%s\n' "$pair" ;;
    esac
  done
  printf 'WORKER_3_NAME=%s\n' "$WORKER_3_NAME"
  printf 'WORKER_3_VERSION=%s\n' "$WORKER_3_VERSION"
}

case "${1:-}" in
  budget)
    budget
    ;;
  start)
    wslc_rm "$WORKER_3_NAME"
    wslc run -d --name "$WORKER_3_NAME" --hostname "$WORKER_3_NAME" --network "$E2E_NET" \
      "${common_env[@]}" \
      -e "LOGHARVEST_WORKER_VERSION=${WORKER_3_VERSION}" \
      -e LOGHARVEST_WORK_DIR=/local-tmp \
      -e LOGHARVEST_WORKER_POLL_SECONDS=1.0 \
      "${BUDGET_ENV[@]}" \
      -v "$NAS_HOST_PATH:/nas:ro" \
      -v "$WORK_VOLUME:/local-tmp" \
      "$WORKER_IMAGE" >/dev/null
    # 临时盘写不进去时作业只会一律 failed，先探针再说，别把环境问题算成产品缺陷。
    if ! wslc exec "$WORKER_3_NAME" touch /local-tmp/.write-probe >/dev/null 2>&1; then
      echo "$WORKER_3_NAME 写不进 /local-tmp（临时盘属主问题）" >&2
      wslc exec "$WORKER_3_NAME" sh -c 'id; ls -ld /local-tmp' >&2 || true
      exit 1
    fi
    wslc exec "$WORKER_3_NAME" rm -f /local-tmp/.write-probe >/dev/null 2>&1 || true
    budget | sed 's/^/  budget: /'
    echo "worker ok : $WORKER_3_NAME ($WORKER_IMAGE, version=$WORKER_3_VERSION, 紧预算)"
    ;;
  stop)
    # 先 stop（SIGTERM）再 rm：Worker 在信号退出的 finally 里自报 offline
    # （service/src/worker/main.py），管理面立刻看到离线行。直接 rm -f 是 SIGKILL，
    # 那一行要等 120s 的僵死回收才被降级，用例不该依赖那种延迟。
    if wslc inspect "$WORKER_3_NAME" >/dev/null 2>&1; then
      wslc stop "$WORKER_3_NAME" >/dev/null 2>&1 || true
    fi
    wslc_rm "$WORKER_3_NAME"
    echo "worker rm : $WORKER_3_NAME"
    ;;
  status)
    if ! wslc inspect "$WORKER_3_NAME" >/dev/null 2>&1; then
      echo "absent"
    elif wslc ps | grep -q "$WORKER_3_NAME"; then
      wslc ps | grep "$WORKER_3_NAME"
    else
      echo "$WORKER_3_NAME 已创建但未运行"
    fi
    ;;
  *)
    echo "用法: bash e2e/worker3.sh {start|stop|status|budget}" >&2
    exit 2
    ;;
esac
