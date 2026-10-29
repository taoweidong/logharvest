#!/usr/bin/env bash
# 串行化跑 UI 阶段的包装：真实栈是共享的（同一 PG、同一批 Worker），两个 ui_flow 同时
# 造作业/建用户就会互相把对方的行数断言搞成假失败，所以这里用目录锁排队。
#
#   bash e2e/ui_run.sh --phase datasources          # 只跑 U7（依赖自动补齐）
#   bash e2e/ui_run.sh                              # 跑全部已注册阶段
#   bash e2e/ui_run.sh --phase workers --tail       # 跑完顺手打印台账里的失败项
#
# 锁：e2e/artifacts/.ui.lock（含持有者与起始时间）。超过 MAX_WAIT 直接报错退出，
# 不去猜锁是不是死了——先看 cat 那个目录里的 owner 文件再决定 rmdir。
set -euo pipefail

E2E_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK_DIR="$E2E_DIR/artifacts/.ui.lock"
MAX_WAIT="${UI_LOCK_MAX_WAIT:-1800}"
SLOT="${UI_LOCK_SLOT:-3600}"

SHOW_TAIL=0
ARGS=()
for a in "$@"; do
  if [ "$a" = "--tail" ]; then SHOW_TAIL=1; else ARGS+=("$a"); fi
done

waited=0
while ! mkdir "$LOCK_DIR" 2>/dev/null; do
  if [ -f "$LOCK_DIR/owner" ]; then
    echo "[ui_run] 锁被占用：$(cat "$LOCK_DIR/owner")（已等待 ${waited}s，上限 ${MAX_WAIT}s）" >&2
  else
    echo "[ui_run] 锁目录存在但缺 owner（陈旧锁？确认无人跑后再 rmdir \"$LOCK_DIR\"；已等待 ${waited}s）" >&2
  fi
  if [ "$waited" -ge "$MAX_WAIT" ]; then
    echo "[ui_run] 等锁超时，放弃。" >&2
    exit 4
  fi
  sleep 20
  waited=$((waited + 20))
done

printf 'pid=%s host=%s start=%s args=%s\n' "$$" "${COMPUTERNAME:-unknown}" "$(date -Is)" "${ARGS[*]:-ALL}" > "$LOCK_DIR/owner"
# 兜底：崩溃式退出（被 kill -9）时锁会留下，靠 SLOT 之后由下一个使用者判定陈旧。
trap 'rm -rf "$LOCK_DIR" 2>/dev/null || true' EXIT

echo "[ui_run] 取锁成功（${LOCK_DIR}），开始：node $E2E_DIR/ui_flow.mjs ${ARGS[*]:-}"
set +e
( cd "$E2E_DIR/.." && node "$E2E_DIR/ui_flow.mjs" "${ARGS[@]}" )
rc=$?
set -e

if [ "$SHOW_TAIL" = "1" ]; then
  PYTHONIOENCODING=utf-8 uv run --python 3.12 python - <<'PY'
import json, pathlib
p = pathlib.Path("e2e/artifacts/ledger_ui_flow.json")
if p.exists():
    d = json.loads(p.read_text(encoding="utf-8"))
    print(f"台账：{d['passed']} 通过 / {d['skipped']} 跳过 / {d['failed']} 失败，共 {d['total']}")
    for c in d["checks"]:
        if not c["ok"]:
            print(f"  !! [{c['phase']}] {c['name']}: {c['detail'][:220]}")
else:
    print("没有台账文件")
PY
fi

echo "[ui_run] 退出码 $rc（台账 e2e/artifacts/ledger_ui_flow.json）"
exit "$rc"
