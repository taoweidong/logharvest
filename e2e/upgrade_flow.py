"""LogHarvest 升级演练：排空 -> 滚动替换 -> 崩溃回收 -> Server 重启 -> 迁移幂等 -> 数据面对账。

这是整套 E2E 里唯一会**动容器**的流程，所以它要同时证明"升级能做"和"升级做坏了能发现"。
八个阶段（W0~W8，其中 W2/W3 合写）按真实生产发布顺序排：

- W1 drain：POST /api/workers/drain-version?version= 的 drained/flagged 计数，以及
  "排空后新任务确实不被认领、恢复后立刻被认领"这条排空的**业务效果**。只数状态字段不够——
  契约里 drain 不打断在跑任务，页面与接口都可能在"看起来 draining"时仍在领活。
- W2/W3 rolling：用 e2e/Dockerfile.worker-v2（同一份代码 + 一个构建期标记文件）滚动替换
  worker-1，制造真实的**混合版本窗口**：一行 0.1.0、一行 0.2.0 同时在线。此时版本级排空
  必须只命中 0.1.0，作业仍能被两侧正确处理。注意 Worker 注册是 INSERT ... ON CONFLICT (id)
  的就地 upsert（id 取 hostname），所以"换镜像"在库里表现为同一行 version 变了、status 被
  强拉回 online，而不是新增一行。
- W4 crash：wslc kill（SIGKILL）正在跑任务的 Worker。进程来不及自报 offline，于是真正的
  兜底路径被逼出来：120 秒僵死宽限内不能回收（否则正常在跑的任务会被误抢）；宽限过后由
  POST /api/system/maintenance（等价于 Worker 自己的 300 秒周期清扫）把 running 行退回
  pending 且 attempts 不回退、attempt 历史标 timeout，并把对端 workers 行降级 offline。
  回收后作业必须能重跑完，且命中不重复入库。
- W5 restart：wslc restart server。升级 API 镜像或改配置后重启是最常见动作，要证明：
  health/ready 恢复、**已签发的 JWT 仍可用**（密钥没变，前端会话不被打断）、审计与业务数据
  一条不少、启动期 Alembic + RBAC 种子没报错、Worker 心跳重连后重新变 online。
- W6 migrations：Alembic 幂等与可逆。主库上 upgrade head 重复执行必须是空操作；然后在一个
  **一次性新库**上跑 upgrade head，把表集合与版本号同主库逐项比对——这就是"全新部署"与
  "就地升级"的等价性，只有两者一致才敢让客户直接升。再做 downgrade -1 -> upgrade head 往返，
  验证回滚脚本可用。**这些动作一律禁止指向主库**（脚本里有显式断言），因为 downgrade 真删数据。
- W7 reconcile：升级后重跑数据面夹具（clean / encoding / traversal），命中数按规则逐条对
  oracle；再对全局计数器（/api/system/status 的 file_tasks 分布、workers_online、积压秒数）
  做"收尾必须归零"的检查。最后默认把 Worker 回滚到基线 0.1.0，让后续 UI 流程看到的仍是
  e2e/up.sh 承诺的环境。

前提：bash e2e/up.sh 已拉起全栈，并且构建了 lh-e2e/service:worker-v2（镜像不存在时本流程
会明确失败，不会静默跳过升级阶段）。

运行：
    PYTHONIOENCODING=utf-8 service/.venv/Scripts/python.exe e2e/upgrade_flow.py
    PYTHONIOENCODING=utf-8 service/.venv/Scripts/python.exe e2e/upgrade_flow.py --phase drain,rolling
    PYTHONIOENCODING=utf-8 service/.venv/Scripts/python.exe e2e/upgrade_flow.py --keep-v2

退出码 0 = 全部断言通过；1 = 有 FAIL（明细见 e2e/artifacts/ledger_upgrade_flow.json）；
2 = 参数或环境不对。环境与其他流程一样走 e2e/env.sh 的默认值，可用同名变量覆盖。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from client import (  # noqa: E402
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    API_BASE,
    TERMINAL_JOB_STATUSES,
    ApiClient,
    ApiError,
    Ledger,
    load_fixtures,
)

from api_flow import create_job, detail_text, expect_error, iter_matches, list_all  # noqa: E402

E2E_DIR = Path(__file__).resolve().parent
RUN_ID = time.strftime("%Y%m%d%H%M%S")

# ---- 环境契约（与 e2e/env.sh 一一对应，同名变量可覆盖）------------------------
NET = os.environ.get("E2E_NET", "lh-e2e-net")
PG_NAME = os.environ.get("PG_NAME", "lh-e2e-pg")
SERVER_NAME = os.environ.get("SERVER_NAME", "lh-e2e-server")
WORKER_1_NAME = os.environ.get("WORKER_1_NAME", "lh-e2e-worker-1")
WORKER_2_NAME = os.environ.get("WORKER_2_NAME", "lh-e2e-worker-2")
WORK_VOLUME = os.environ.get("WORK_VOLUME", "lh-e2e-work")
# Worker 的任务目录 = LOGHARVEST_WORK_DIR/logharvest/<worker_id>（service/src/worker/main.py:330）。
# 三台托管 Worker 挂的是同一个 named volume，但清扫只在自己的 <worker_id> 子目录里做，
# 所以 W7 的孤儿目录实验只碰 Worker-1 那一格。
WORK_DIR_ROOT = os.environ.get("E2E_WORK_DIR_ROOT", "/local-tmp/logharvest")
ORPHAN_PROBE_OLD = "e2e-orphan-old"
ORPHAN_PROBE_NEW = "e2e-orphan-new"

SERVICE_IMAGE = os.environ.get("SERVICE_IMAGE", "lh-e2e/service:e2e")
WORKER_IMAGE = os.environ.get("WORKER_IMAGE", "lh-e2e/service:worker")
WORKER_V2_IMAGE = os.environ.get("WORKER_V2_IMAGE", "lh-e2e/service:worker-v2")

PG_USER = os.environ.get("PG_USER", "logharvest")
PG_DB = os.environ.get("PG_DB", "logharvest_e2e")
# 迁移往返用的一次性库：带 RUN_ID 后缀，绝不复用主库或 pytest 集成测试库。
MIG_DB = os.environ.get("E2E_MIG_DB", f"logharvest_e2e_mig_{RUN_ID}")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "e2e-local-only")

PG_HOST = os.environ.get("E2E_PG_HOST_IN_NET", PG_NAME)  # server 容器看到的库主机名
NAS_HOST_PATH = os.environ.get("NAS_HOST_PATH") or str((E2E_DIR / "nas").resolve()).replace("\\", "/")
NAS_ROOT = "/nas"

JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "e2e-only-jwt-secret-key-please-never-reuse-0123456789")
ADMIN_ENV_USERNAME = os.environ.get("ADMIN_USERNAME", ADMIN_USERNAME)
ADMIN_ENV_PASSWORD = os.environ.get("ADMIN_PASSWORD", ADMIN_PASSWORD)
NAS_POLICY = os.environ.get("LOGHARVEST_NAS_READ_ONLY_POLICY", os.environ.get("NAS_POLICY", "enforce"))
BASE_VERSION = os.environ.get("WORKER_VERSION", "0.1.0")
V2_VERSION = os.environ.get("WORKER_V2_VERSION", "0.2.0")

# 僵死回收阈值在 service/src/worker/main.py 里写死 120s，没有环境变量可压。
# 演练就按生产口径等：宽限内断言"还没被回收"，宽限后断言"回收成功"。
STALE_SECONDS = 120
CRASH_WAIT_SECONDS = STALE_SECONDS + 45

COMMON_ENV = [
    "-e", "LOGHARVEST_ENVIRONMENT=production",
    "-e", f"LOGHARVEST_DATABASE_URL=postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:5432/{PG_DB}",
    "-e", f"LOGHARVEST_JWT_SECRET_KEY={JWT_SECRET_KEY}",
    "-e", f"LOGHARVEST_ADMIN_USERNAME={ADMIN_ENV_USERNAME}",
    "-e", f"LOGHARVEST_ADMIN_PASSWORD={ADMIN_ENV_PASSWORD}",
    "-e", f"LOGHARVEST_NAS_READ_ONLY_POLICY={NAS_POLICY}",
]

# 演练造出的作业前缀；同名重跑靠 Idempotency-Key 里的 RUN_ID 区分。
JOB_PREFIX = "e2eup"


# ============================================================================
# wslc / PostgreSQL 外壳
# ============================================================================
def wslc_run(args: list[str], *, timeout: float = 300.0) -> subprocess.CompletedProcess[str]:
    """wslc 调用统一入口：Python 的 subprocess 不经过 MSYS，路径不会被改写，无需 MSYS_NO_PATHCONV。"""
    return subprocess.run(
        ["wslc", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def wslc(*args: str, timeout: float = 300.0) -> str:
    """执行 wslc 并在非 0 退出时抛 RuntimeError（含 stderr，便于定位是容器问题还是断言问题）。"""
    proc = wslc_run(list(args), timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"wslc {' '.join(args)} -> {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:400]}")
    return ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()


def wslc_soft(*args: str, timeout: float = 120.0) -> tuple[int, str]:
    proc = wslc_run(list(args), timeout=timeout)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode, out


def container_exists(name: str) -> bool:
    code, _ = wslc_soft("inspect", name, timeout=60)
    return code == 0


def image_exists(tag: str) -> bool:
    code, _ = wslc_soft("image", "inspect", tag, timeout=120)
    return code == 0


def pg_sql(sql: str, *, database: str | None = None) -> str:
    """在 pg 容器里跑一条 SQL，返回纯文本（-At：无边框、按行）。"""
    return wslc("exec", PG_NAME, "psql", "-U", PG_USER, "-d", database or PG_DB, "-At", "-c", sql, timeout=180)


def alembic(*cmd: str, database_url: str | None = None) -> str:
    """在 server 容器里跑 alembic（/app 下有 alembic.ini，settings 读 LOGHARVEST_DATABASE_URL）。"""
    args = ["exec"]
    if database_url:
        args += ["-e", f"LOGHARVEST_DATABASE_URL={database_url}"]
    args += [SERVER_NAME, "alembic", *cmd]
    return wslc(*args, timeout=300)


def mig_url(db_name: str) -> str:
    """一次性迁移库的连接串。库名必须含 mig，作为"绝不碰主库"的最后一道闸。"""
    assert db_name != PG_DB, "迁移往返禁止指向主库"
    return f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:5432/{db_name}"


def start_worker(name: str, image: str, version: str, *, wait_online: float = 90.0) -> None:
    """按 e2e/up.sh 的同一套参数起 Worker（NAS 只读 bind + 临时盘卷 + 版本号上报）。"""
    if container_exists(name):
        wslc("rm", "-f", name, timeout=120)
    wslc(
        "run", "-d", "--name", name, "--hostname", name, "--network", NET,
        *COMMON_ENV,
        "-e", f"LOGHARVEST_WORKER_VERSION={version}",
        "-e", "LOGHARVEST_WORK_DIR=/local-tmp",
        "-e", "LOGHARVEST_MATCHES_BATCH_SIZE=1000",
        "-e", "LOGHARVEST_MAX_MATCHES_PER_TASK=200000",
        "-e", "LOGHARVEST_WORKER_POLL_SECONDS=1.0",
        "-v", f"{NAS_HOST_PATH}:/nas:ro",
        "-v", f"{WORK_VOLUME}:/local-tmp",
        image,
        timeout=180,
    )


def graceful_stop(name: str) -> None:
    """SIGTERM 后再删：Worker 在退出的 finally 里自报 offline，管理面立刻看到离线行。"""
    if container_exists(name):
        wslc_soft("stop", name, timeout=120)
        wslc_soft("rm", name, timeout=120)


def hard_kill(name: str) -> None:
    """SIGKILL：进程没有 finally 可跑，自报 offline 这条路被物理切断，只剩兜底回收。"""
    wslc_soft("kill", name, timeout=120)
    wslc_soft("rm", "-f", name, timeout=120)


# ============================================================================
# 通用等待
# ============================================================================
def wait_until(fn: Callable[[], Any], *, label: str, timeout: float = 60.0, interval: float = 2.0) -> Any:
    """fn 返回真值即结束；超时抛 TimeoutError（阶段级异常会被台账记成 FAIL）。"""
    deadline = time.time() + timeout
    last: Any = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise TimeoutError(f"等 {label} 超时（{timeout}s），最后状态={str(last)[:200]}")


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def workers_of(api: ApiClient) -> list[dict[str, Any]]:
    return api.get("/workers?limit=200&offset=0", expect_status=200)


def by_id(rows: list[dict[str, Any]], worker_id: str) -> dict[str, Any] | None:
    return next((r for r in rows if r["id"] == worker_id), None)


def managed_rows(api: ApiClient) -> list[dict[str, Any]]:
    """演练只托管这两台 Worker。

    同一张 workers 表里还会有别的流程留下的行（limits 流程用 worker3.sh 起的
    `0.1.0-budget`），把它们算进"两条在线""版本集只剩基线"这类计数断言就是自找假失败，
    算进"SIGKILL 后没有行自报离线"更是把别人早就优雅停掉留下的 offline 读成 bug。
    """
    return [r for r in workers_of(api) if r["id"] in (WORKER_1_NAME, WORKER_2_NAME)]


def foreign_rows(api: ApiClient) -> list[dict[str, Any]]:
    """不属于本次演练的 Worker 行（别的流程留下的机器），用来在起跑前把它们清出场。"""
    return [r for r in workers_of(api) if r["id"] not in (WORKER_1_NAME, WORKER_2_NAME)]


def container_is_running(name: str) -> bool:
    """`wslc ps` 只列运行中的容器，按名字命中即视为在跑（worker3.sh 的 ps 分支同一口径）。"""
    code, out = wslc_soft("ps", timeout=120)
    return code == 0 and name in out


def online_count(api: ApiClient) -> int:
    return len([r for r in managed_rows(api) if r["status"] in ("online", "busy")])


def heartbeat_age_seconds(row: dict[str, Any]) -> float | None:
    """last_heartbeat 是带 Z 的 UTC 串；算年龄用来判"僵死宽限内/外"。"""
    raw = row.get("last_heartbeat")
    if not raw:
        return None
    from datetime import UTC, datetime

    text = str(raw).replace("Z", "+00:00")
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return (datetime.now(UTC) - stamp).total_seconds()


def audit_actions(api: ApiClient, action: str, *, limit: int = 50) -> list[dict[str, Any]]:
    return api.get(f"/operation-logs?action={action}&limit={limit}&offset=0", expect_status=200)


def make_job(api: ApiClient, ctx: dict[str, Any], case: str, *, source_path: str, max_files: int = 20000) -> dict[str, Any]:
    """用既有 e2e-nas 数据源 + e2e-canonical 规则集建作业；名字带阶段，重跑不冲突。"""
    name = f"{JOB_PREFIX}-{case}-{RUN_ID}"
    job = create_job(
        api,
        name=name,
        data_source_id=ctx["nas_ds"]["id"],
        source_path=source_path,
        rule_set_id=ctx["ruleset"]["id"],
        idem=f"{JOB_PREFIX}-{case}-{RUN_ID}",
        file_filter={"recursive": True, "max_depth": 5, "max_files": max_files},
    )
    # 演练自己造的作业逐个记账：对账与重启比对都只认这份名单，
    # 别的流程正在同一个库里并发造数据，全局计数没法拿来精确断言。
    ctx.setdefault("drill_job_ids", []).append(int(job["id"]))
    return job


def job_file_tasks(api: ApiClient, job_id: int) -> list[dict[str, Any]]:
    return api.get(f"/jobs/{job_id}/files?limit=500&offset=0", expect_status=200)


def job_matched_rows(api: ApiClient, job_id: int) -> int:
    """作业命中数：JobRead 里没有这个字段（契约 §6.2），只能按文件任务求和或拉 /matches。"""
    return int(sum(int(t.get("matched_rows") or 0) for t in job_file_tasks(api, job_id)))


def matches_by_rule(api: ApiClient, job_id: int) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in iter_matches(api, page=500, job_id=job_id):
        counts[str(row["rule_id"])] = counts.get(str(row["rule_id"]), 0) + 1
    return counts


def snapshot(api: ApiClient) -> dict[str, Any]:
    """全局计数器快照：升级前后各取一次，用来证明"没丢任务、没留下孤儿"。"""
    status = api.get("/system/status", expect_status=200)
    return {
        "file_tasks": status.get("file_tasks", {}),
        "workers_online": status.get("workers_online"),
        "pending_oldest_seconds": status.get("pending_oldest_seconds"),
        "jobs": len(list_all(api, "/jobs?limit=500&offset=0")),
    }


# ============================================================================
# W0 前置
# ============================================================================
def phase_pre(ledger: Ledger, api: ApiClient, ctx: dict[str, Any]) -> None:
    ledger.phase("W0 前置：全栈健康、两套 Worker 镜像、迁移已在 head")

    api.get("/health", expect_status=200)
    ready = api.call("GET", "/health/ready", expect_status=200)
    ledger.check("/health/ready 通过（升级演练不允许带病起跑）", True, json.dumps(ready, ensure_ascii=False)[:200])

    # 升级演练的被测对象就是"新版本镜像"，它不在就没有可演练的东西，必须硬失败。
    ledger.check(f"新版本 Worker 镜像存在：{WORKER_V2_IMAGE}", image_exists(WORKER_V2_IMAGE),
                 f"bash e2e/up.sh 会构建它；缺失时先跑 up.sh")
    ledger.check(f"基线 Worker 镜像存在：{WORKER_IMAGE}", image_exists(WORKER_IMAGE), WORKER_IMAGE)

    rows = workers_of(api)
    ids = sorted(r["id"] for r in rows)
    ledger.check("两条基线 Worker 容器在跑",
                 container_exists(WORKER_1_NAME) and container_exists(WORKER_2_NAME), ids)
    ledger.check("Worker 行含 id/hostname/version/status/last_heartbeat（页面分组的数据源）",
                 all({"id", "hostname", "version", "status", "last_heartbeat"} <= set(r) for r in rows),
                 json.dumps(rows[0], ensure_ascii=False, default=str)[:260] if rows else "空")
    versions = sorted({r["version"] for r in rows})
    ledger.check("起跑时的原始版本集合已记录（含别人留下的机器，随后清场）",
                 versions == sorted({r["version"] for r in workers_of(api)}),
                 json.dumps([[r["id"], r["version"], r["status"]] for r in rows], ensure_ascii=False)[:300])

    # 全场的计数断言（版本集合、在线台数、"崩溃后没有行自报离线"）都以托管的两台为口径。
    # 别的流程留下的机器若还在跑就会污染这些数字，所以起跑前优雅停掉：只 stop 不 rm，
    # 停机后自报 offline 正是我们想要的状态，W8 收尾时再原样 start 回去。
    foreign_running = [r["id"] for r in foreign_rows(api) if container_is_running(r["id"])]
    for name in foreign_running:
        wslc_soft("stop", name, timeout=120)
    if foreign_running:
        wait_until(lambda: all((by_id(workers_of(api), n) or {}).get("status") == "offline"
                               for n in foreign_running),
                   label=f"非托管 Worker 自报离线：{foreign_running}", timeout=60, interval=2)
    ctx["foreign_stopped"] = foreign_running
    ledger.check("非托管 Worker 已下线（演练池子口径唯一）",
                 all((by_id(workers_of(api), n) or {}).get("status") == "offline" for n in foreign_running),
                 json.dumps([[n, (by_id(workers_of(api), n) or {}).get("status")] for n in foreign_running])
                 if foreign_running else "无非托管容器在跑")

    rows = managed_rows(api)
    versions = sorted({r["version"] for r in rows})
    ledger.equal("起跑版本只有基线版本", versions, [BASE_VERSION])

    # 版本级排空靠 Worker 自报的 version 精确匹配，这里确认它来自环境变量而不是镜像标签。
    reported = {r["id"]: r["version"] for r in rows}
    ledger.check("两条 Worker 都上报同一自报版本（LOGHARVEST_WORKER_VERSION 生效）",
                 len(set(reported.values())) == 1, json.dumps(reported))

    current = alembic("current")
    heads = alembic("heads")
    ledger.check("主库 alembic current 非空（启动期自动迁移确实跑过）", bool(current), current[:200])
    ledger.check("主库已在 head：current 与 heads 同一修订",
                 current.split()[0] == heads.split()[0], {"current": current[:80], "heads": heads[:80]})

    nas = next((d for d in api.get("/data-sources", expect_status=200) if d["name"] == "e2e-nas"), None)
    ledger.check("复用既有只读数据源 e2e-nas", nas is not None, json.dumps(nas, ensure_ascii=False, default=str)[:220])
    if nas is None:
        raise RuntimeError("缺少 e2e-nas 数据源：先跑 e2e/api_flow.py 的 sources 阶段")
    verdict = api.post(f"/data-sources/{nas['id']}/test", expect_status=200)
    ledger.equal("NAS 只读判定仍是 read_only（enforce 前提）", verdict["writability"], "read_only")

    sets = api.get("/rule-sets", expect_status=200)
    canonical = sorted((s for s in sets if s["name"] == "e2e-canonical"), key=lambda s: -s["version"])
    ledger.check("规则集 e2e-canonical 存在", bool(canonical), json.dumps([s["id"] for s in sets]))
    if not canonical:
        raise RuntimeError("缺少 e2e-canonical 规则集：先跑 e2e/api_flow.py 的 rulesets 阶段")
    ctx["nas_ds"] = nas
    ctx["ruleset"] = canonical[0]
    ctx["fixtures"] = load_fixtures()["cases"]
    ctx["baseline"] = snapshot(api)
    ctx["alembic_head"] = heads.split()[0]
    ledger.check("基线快照已记录", True, json.dumps(ctx["baseline"], ensure_ascii=False)[:240])


# ============================================================================
# W1 按版本排空
# ============================================================================
def phase_drain(ledger: Ledger, api: ApiClient, ctx: dict[str, Any]) -> None:
    ledger.phase("W1 按版本排空 0.1.0：计数、不认领新任务、恢复后立刻认领")

    before = workers_of(api)
    online_before = [r for r in before if r["status"] in ("online", "busy") and r["version"] == BASE_VERSION]
    offline_before = [r for r in before if r["status"] == "offline" and r["version"] == BASE_VERSION]

    resp = api.post(f"/workers/drain-version?version={BASE_VERSION}", expect_status=200)
    ledger.equal("drain-version 的 drained = 在线基线 Worker 数", resp["drained"], len(online_before))
    ledger.equal("drain-version 的 flagged = 离线基线 Worker 数", resp["flagged"], len(offline_before))

    rows = wait_until(
        lambda: [r for r in workers_of(api) if r["version"] == BASE_VERSION],
        label="Worker 行可见", timeout=30,
    )
    draining = [r for r in rows if r["status"] == "draining"]
    ledger.check("在线行被翻成 draining（每轮 run_once 开头读 drain_requested）",
                 len(draining) == len(online_before),
                 json.dumps([[r["id"], r["status"]] for r in rows], ensure_ascii=False))
    ledger.check("drain_requested 落库为真", all(r["drain_requested"] is True for r in draining),
                 json.dumps([r["drain_requested"] for r in draining]))

    acts = audit_actions(api, "worker.drain_version")
    ledger.check("审计留痕 worker.drain_version", len(acts) > 0 and
                 acts[0]["detail"].get("version") == BASE_VERSION,
                 json.dumps(acts[0], ensure_ascii=False, default=str)[:260] if acts else "无记录")

    # 排空的真实效果：没有任何在线 Worker 认领，作业只能排队。这一步把"状态字段好看"
    # 和"真的不领活"区分开——后者才是发布窗口里运维依赖的性质。
    status = api.get("/system/status", expect_status=200)
    ledger.equal("system/status 的 online 口径含 draining（契约：online/busy/draining）",
                 status["workers_online"], len([r for r in workers_of(api) if r["status"] != "offline"]))

    job = make_job(api, ctx, "drained", source_path="/nas/clean")
    ctx["drained_job"] = job["id"]
    sleep(25)
    mid = api.get(f"/jobs/{job['id']}", expect_status=200)
    tasks = job_file_tasks(api, job["id"])
    ledger.check("排空期间作业保持排队（没有 Worker 认领）",
                 mid["status"] not in TERMINAL_JOB_STATUSES and not any(t["status"] == "running" for t in tasks),
                 json.dumps({"job": mid["status"], "tasks": [[t["id"], t["status"]] for t in tasks]}, ensure_ascii=False))
    ledger.check("排空期间任务没有 worker_id", all(t["worker_id"] is None for t in tasks),
                 json.dumps([t["worker_id"] for t in tasks], ensure_ascii=False))

    # 逐个恢复：resume 是行级操作（drain-version 没有反向的 resume-version）。
    for row in draining:
        back = api.post(f"/workers/{row['id']}/resume", expect_status=200)
        ledger.check(f"resume 后 {row['id']} 回到 online", back["status"] == "online" and back["drain_requested"] is False,
                     json.dumps(back, ensure_ascii=False))
    wait_until(lambda: all(r["status"] in ("online", "busy") for r in workers_of(api) if r["version"] == BASE_VERSION),
               label="恢复后 Worker 重新认领任务", timeout=90, interval=2)

    done = api.wait_job_terminal(job["id"], timeout=420)
    ledger.equal("恢复后排队的作业被处理成功", done["status"], "succeeded")
    oracle = ctx["fixtures"]["clean"]
    ledger.equal("排空/恢复往返没有影响命中数", job_matched_rows(api, job["id"]), oracle["expect_total_matches"])
    ledger.check("恢复后审计留痕 worker.resume", len(audit_actions(api, "worker.resume")) > 0, "worker.resume")

    # offline 行的排空语义：只打标记、不改状态，等它回来再排。
    offline_rows = [r for r in workers_of(api) if r["status"] == "offline"]
    if offline_rows:
        target = offline_rows[0]
        acted = api.post(f"/workers/{target['id']}/drain", expect_status=200)
        ledger.equal("对 offline 行 drain：状态不变", acted["status"], "offline")
        ledger.equal("对 offline 行 drain：drain_requested=true（回来后再排）", acted["drain_requested"], True)
        expect_error(ledger, "对 offline 行 resume → 409 worker is offline",
                     lambda: api.post(f"/workers/{target['id']}/resume"), 409, contains="worker is offline")
    else:
        ledger.skip("offline 行的 drain/resume 语义", "本轮没有离线 Worker 行可测；W4 崩溃回收后会覆盖")

    expect_error(ledger, "drain-version 空版本 → 422（min_length=1）",
                 lambda: api.post("/workers/drain-version?version="), 422)


def job_match_rows(api: ApiClient, job_id: int) -> list[dict[str, Any]]:
    """/api/matches 没有 offset，只有游标；iter_matches 负责翻到末尾。"""
    return list(iter_matches(api, page=500, job_id=job_id))


# ============================================================================
# W2/W3 滚动替换与混合版本窗口
# ============================================================================
def phase_rolling(ledger: Ledger, api: ApiClient, ctx: dict[str, Any]) -> None:
    ledger.phase("W2 滚动替换 worker-1 -> 0.2.0（同 id upsert，不是新增行）")

    # 停止信号走 SIGTERM：Worker 的 finally 会自报 offline，管理面立刻看到离线行，
    # 这正是生产滚动发布想要的行为（e2e/worker3.sh 里同一套理由）。
    graceful_stop(WORKER_1_NAME)
    row = wait_until(
        lambda: (lambda r: r if r and r["status"] == "offline" else None)(by_id(workers_of(api), WORKER_1_NAME)),
        label=f"{WORKER_1_NAME} 自报 offline", timeout=40, interval=2,
    )
    ledger.check("优雅停止后行状态是 offline 且清掉 current_task_id",
                 row["status"] == "offline" and row["current_task_id"] in (None, 0, ""),
                 json.dumps({k: row[k] for k in ("id", "status", "current_task_id")}, ensure_ascii=False))
    ledger.equal("离线行的版本仍是旧版本（历史留痕，页面按版本分组要用）", row["version"], BASE_VERSION)

    start_worker(WORKER_1_NAME, WORKER_V2_IMAGE, V2_VERSION)
    new = wait_until(
        lambda: (lambda r: r if r and r["status"] in ("online", "busy") and r["version"] == V2_VERSION else None)(
            by_id(workers_of(api), WORKER_1_NAME)),
        label=f"{WORKER_1_NAME} 以 {V2_VERSION} 重新注册", timeout=120, interval=3,
    )
    ledger.check("同 id 重新注册把状态强拉回 online（REGISTER 的 upsert 语义）",
                 new["status"] in ("online", "busy"), json.dumps(new["status"]))
    ledger.equal("注册把 version 就地改成新版本", new["version"], V2_VERSION)
    rows = workers_of(api)
    ledger.check("Worker 总数没有因为替换而增加（是替换不是扩容）",
                 len(rows) == len({r["id"] for r in rows}), json.dumps([r["id"] for r in rows], ensure_ascii=False))

    # 换的是镜像本身，不是只改了版本号：v2 镜像的构建期标记文件是唯一的物理证据。
    marker = wslc("exec", WORKER_1_NAME, "cat", "/etc/logharvest-release", timeout=60)
    ledger.equal("新容器里读到 v2 镜像的构建期标记", marker.strip(), "e2e-release=v2")
    code, _ = wslc_soft("exec", WORKER_2_NAME, "cat", "/etc/logharvest-release", timeout=60)
    ledger.check("对照组 worker-2 读不到该标记（证明标记来自镜像而非共享挂载）", code != 0, f"exit={code}")

    ledger.phase("W3 混合版本窗口：一行 0.1.0 + 一行 0.2.0 同时在线")

    rows = workers_of(api)
    present = sorted({r["version"] for r in rows if r["status"] != "offline"})
    ledger.equal("此刻在线版本集正好是两版混合", present, sorted([BASE_VERSION, V2_VERSION]))

    job = make_job(api, ctx, "mixed", source_path="/nas/clean")
    done = api.wait_job_terminal(job["id"], timeout=420)
    ledger.equal("混合窗口内作业照样成功", done["status"], "succeeded")
    oracle = ctx["fixtures"]["clean"]
    ledger.equal("混合窗口命中数与 oracle 一致（升级没有改变抓取结果）",
                 job_matched_rows(api, job["id"]), oracle["expect_total_matches"])

    # attempt 历史逐次留痕 worker_version：这是"哪个版本跑过这一行"的唯一权威证据。
    def collect_versions(job_id: int) -> list[str]:
        found: list[str] = []
        for task in job_file_tasks(api, job_id):
            for attempt in api.get(f"/jobs/{job_id}/files/{task['id']}/attempts", expect_status=200):
                found.append(attempt["worker_version"])
        return found

    seen_versions: list[str] = collect_versions(job["id"])
    # 谁认领由两台 Worker 抢 SELECT ... FOR UPDATE SKIP LOCKED 的先后决定，而 clean 只有 2 个
    # 文件任务，实测会被轮询快的那台全包（首轮 0.1.0 独占、次轮 0.2.0 独占）。混合窗口的结论
    # 是"两版都真跑过活"，所以样本不够就继续投多文件夹具，直到两个版本都在 attempt 历史里
    # 留痕或预算用尽——这是补齐样本，不是放宽断言。
    extra_cases = [("/nas/formats", 6), ("/nas/deep", 5), ("/nas/limits", 2), ("/nas/clean", 2)]
    both_versions = {BASE_VERSION, V2_VERSION}
    extra_used = 0
    for path, _ in extra_cases:
        if both_versions <= set(seen_versions):
            break
        extra = make_job(api, ctx, f"mixed{extra_used + 1}", source_path=path)
        ledger.equal(f"{path} 作业在混合窗口内跑完", api.wait_job_terminal(extra["id"], timeout=420)["status"], "succeeded")
        seen_versions += collect_versions(extra["id"])
        extra_used += 1
    version_detail = json.dumps(
        {"versions": sorted(set(seen_versions)), "attempts": len(seen_versions), "extra_jobs": extra_used},
        ensure_ascii=False)
    ledger.check("attempt 历史里出现过新版本 Worker 的自报版本", V2_VERSION in seen_versions, version_detail)
    ledger.check("同一窗口里旧版本 Worker 的留痕也没消失（混合而非替换完）",
                 BASE_VERSION in seen_versions, version_detail)

    # 混合窗口里版本级排空必须只命中旧版：这是"按版本滚动"的核心筛选语义。
    online_old = [r for r in workers_of(api) if r["version"] == BASE_VERSION and r["status"] in ("online", "busy")]
    resp = api.post(f"/workers/drain-version?version={BASE_VERSION}", expect_status=200)
    ledger.equal("版本级排空只命中旧版本行", resp["drained"], len(online_old))
    ledger.check("新版本行没有被牵连",
                 all(r["status"] != "draining" for r in workers_of(api) if r["version"] == V2_VERSION),
                 json.dumps([[r["id"], r["version"], r["status"]] for r in workers_of(api)], ensure_ascii=False))
    for row in online_old:
        api.post(f"/workers/{row['id']}/resume", expect_status=200)

    # 第二台替换完成，进入"全量新版本"稳态。
    graceful_stop(WORKER_2_NAME)
    wait_until(lambda: (by_id(workers_of(api), WORKER_2_NAME) or {}).get("status") == "offline",
               label=f"{WORKER_2_NAME} 自报 offline", timeout=40, interval=2)
    start_worker(WORKER_2_NAME, WORKER_V2_IMAGE, V2_VERSION)
    wait_until(lambda: len([r for r in workers_of(api) if r["version"] == V2_VERSION and r["status"] in ("online", "busy")]) == 2,
               label="两条 Worker 都以 0.2.0 在线", timeout=120, interval=3)
    ledger.equal("升级稳态：在线版本集只剩新版本",
                 sorted({r["version"] for r in workers_of(api) if r["status"] != "offline"}), [V2_VERSION])
    ledger.equal("system/status 的 workers_online 跟上到 2",
                 api.get("/system/status", expect_status=200)["workers_online"], 2)
    ctx["upgraded"] = True


# ============================================================================
# W4 全池崩溃 + 服务端维护清扫（回收僵死任务 / 降级死 Worker / 空池排队）
# ============================================================================
def phase_crash(ledger: Ledger, api: ApiClient, ctx: dict[str, Any]) -> None:
    ledger.phase("W4 崩溃回收：SIGKILL 掉整个 Worker 池，只剩服务端兜底")

    # 为什么杀"全部" Worker 而不是只杀归属者：Worker 循环里也有一份同类清扫
    # （每 300s 一次，见 service/src/worker/main.py）。留着同伴就会引入
    # "被周期清扫抢先"的竞态，回收计数字段就变成不确定的 0 或 1。全池死亡才是
    # POST /api/system/maintenance 这个入口被设计出来的真实场景。
    rows = managed_rows(api)
    ledger.check("起跑时至少有 1 个托管 Worker 行可杀", len(rows) >= 1, json.dumps([[r["id"], r["status"]] for r in rows], ensure_ascii=False))

    job = make_job(api, ctx, "crash", source_path="/nas/cancel")
    ctx["crash_job"] = job["id"]
    # /nas/cancel 只有一个 ~60MB 存储型 zip（成员 big.log），解压+扫描给足"运行中"窗口，
    # runtime_flow 的"运行中取消"用的就是同一个夹具，窗口宽度已被验证过。
    running = wait_until(
        lambda: next((t for t in job_file_tasks(api, job["id"]) if t["status"] == "running"), None),
        label="/nas/cancel 出现 running 任务", timeout=300, interval=1,
    )
    task_id, owner = running["id"], running["worker_id"]
    ledger.check("running 任务带着归属 Worker 与心跳", bool(owner) and bool(running["heartbeat_at"]),
                 json.dumps({k: running[k] for k in ("id", "worker_id", "heartbeat_at", "attempts")}, ensure_ascii=False))
    owner_row = by_id(rows, owner) or {}
    ledger.check("归属 Worker 在管理面上是非离线（online/busy）",
                 owner_row.get("status") in ("online", "busy"), json.dumps(owner_row.get("status")))

    for name in (WORKER_1_NAME, WORKER_2_NAME):
        hard_kill(name)

    killed = managed_rows(api)
    ledger.check("SIGKILL 之后两行都还没 offline（自报退出的路被物理切断）",
                 all(r["status"] != "offline" for r in killed) and len(killed) == 2,
                 json.dumps([[r["id"], r["status"]] for r in killed], ensure_ascii=False))
    status_now = api.get("/system/status", expect_status=200)
    ledger.check("管理面此刻「以为」池子还健康（workers_online 仍计数）——这正是需要服务端兜底的原因",
                 status_now["workers_online"] >= 2, json.dumps(status_now, ensure_ascii=False)[:220])

    # 僵死阈值写死 120s（DEFAULT_STALE_SECONDS），没有环境变量可压；演练按生产口径等。
    sleep(CRASH_WAIT_SECONDS)
    still = next((t for t in job_file_tasks(api, job["id"]) if t["id"] == task_id), None)
    ledger.equal("宽限期内没有任何进程推进它，任务仍停在 running", still["status"], "running")

    # 全池无存活进程时新作业只能排队：证明"服务端收单"与"执行"是解耦的。
    # 注意"排队"在作业这一层看不见——POST /jobs 收单时同步扫描目录并把作业直接置成
    # running（没有 queued 这个作业状态），所以"没有 Worker 认领"只能由文件任务层证明：
    # 全部 pending 且 worker_id 为空，同时作业还没进终态。
    idle_job = make_job(api, ctx, "no-worker", source_path="/nas/clean")
    ctx["idle_job"] = idle_job["id"]
    sleep(30)
    idle_status = api.get(f"/jobs/{idle_job['id']}", expect_status=200)["status"]
    idle_tasks = job_file_tasks(api, idle_job["id"])
    ledger.check("空池 30s 后新作业仍未进终态（没人认领就没人收尾）",
                 idle_status not in TERMINAL_JOB_STATUSES, idle_status)
    ledger.check("它的文件任务全在排队、没有 running 与归属",
                 len(idle_tasks) > 0 and all(t["status"] == "pending" and t["worker_id"] in (None, "") for t in idle_tasks),
                 json.dumps([[t["id"], t["status"], t["worker_id"]] for t in idle_tasks], ensure_ascii=False))

    swept = api.post("/system/maintenance", expect_status=200)
    ledger.check("维护清扫回收了僵死任务（recovered>=1）", swept["recovered"] >= 1, json.dumps(swept, ensure_ascii=False))
    ledger.check("维护清扫把死 Worker 降级（workers_demoted>=2）", swept["workers_demoted"] >= 2, json.dumps(swept, ensure_ascii=False))
    ledger.check("审计里留下这次维护", bool(audit_actions(api, "system.maintenance")),
                 json.dumps(audit_actions(api, "system.maintenance")[:1], ensure_ascii=False)[:220])

    after = next((t for t in job_file_tasks(api, job["id"]) if t["id"] == task_id), None)
    ledger.equal("任务退回 pending（回收是重新排队，不是丢弃）", after["status"], "pending")
    ledger.check("attempts 保留（回收不回滚重试次数，上限照样生效）", after["attempts"] >= 1,
                 json.dumps({"attempts": after["attempts"], "max_attempts": after["max_attempts"]}))
    ledger.check("归属被清空，等待任意存活 Worker 重新认领", after["worker_id"] in (None, ""), json.dumps(after["worker_id"]))
    ledger.check("error 写清是心跳超时", "heartbeat" in (after["error"] or ""), json.dumps(after["error"]))
    ledger.equal("降级后的 Worker 行是 offline", (by_id(workers_of(api), owner) or {}).get("status"), "offline")

    history = api.get(f"/jobs/{job['id']}/files/{task_id}/attempts", expect_status=200)
    timed_out = [a for a in history if a["status"] == "timeout"]
    ledger.check("崩掉的那次尝试被标成 timeout 并写了结束时间",
                 len(timed_out) == 1 and bool(timed_out[0]["finished_at"]),
                 json.dumps([[a["attempt_no"], a["status"], a["worker_version"]] for a in history], ensure_ascii=False))

    # 维护入口可重复执行：没有待处理项时三个计数都应该是 0（幂等，不是"报错"）。
    again = api.post("/system/maintenance", expect_status=200)
    ledger.equal("再跑一次维护是幂等的（recovered 归零）", again["recovered"], 0)

    # 恢复池子：按行上残留的版本决定镜像，这样单跑 W4（没做过 W2/W3）也能收敛。
    for name in (WORKER_1_NAME, WORKER_2_NAME):
        row = by_id(workers_of(api), name) or {}
        version = row.get("version") or BASE_VERSION
        start_worker(name, WORKER_V2_IMAGE if version == V2_VERSION else WORKER_IMAGE, version)
    wait_until(lambda: online_count(api) == 2,
               label="两个托管 Worker 重新在线", timeout=180, interval=3)

    done = api.wait_job_terminal(job["id"], timeout=900)
    ledger.equal("崩溃作业由重排后的任务跑完（不是靠取消或人工兜底）", done["status"], "succeeded")
    ledger.equal("空池期排队的作业也在恢复后成功", api.wait_job_terminal(idle_job["id"], timeout=900)["status"], "succeeded")

    # 重试的幂等性：processor 写库前按 file_task 删旧行（DELETE_BY_TASK_SQL），
    # 所以"同一任务同一成员同一行号同一规则"不允许出现两次；同时页面上的任务计数
    # 必须与实际行数一致——这是崩溃恢复最容易留下脏数据的地方。
    match_rows = job_match_rows(api, job["id"])
    keys = [(m["file_task_id"], m["member_path"], m["line_no"], m["rule_id"]) for m in match_rows]
    ledger.check("重跑没有留下重复命中（按任务删除后重写）", len(keys) == len(set(keys)),
                 f"{len(keys)} 行 / {len(set(keys))} 唯一键")
    ledger.equal("任务级命中计数与实际行数一致", job_matched_rows(api, job["id"]), len(match_rows))
    clean_oracle = ctx["fixtures"]["clean"]
    ledger.equal("排队 30s 的那个小作业命中数仍等于 oracle",
                 job_matched_rows(api, idle_job["id"]), clean_oracle["expect_total_matches"])
    ctx["crash_recovered"] = True


# ============================================================================
# W5 Server 重启：旧 token 继续可用、数据没丢、启动期迁移幂等、心跳与作业照跑
# ============================================================================
def phase_restart(ledger: Ledger, api: ApiClient, ctx: dict[str, Any]) -> None:
    ledger.phase("W5 Server 重启：换进程不换数据")

    def startup_marker_count() -> int:
        # uvicorn 每次启动打印一次 "Application startup complete"。数它的增量，比在
        # 一大坨访问日志里找"最后一条"稳：重启前后的差值必须正好是 1。
        _, out = wslc_soft("logs", "--tail", "2000", SERVER_NAME, timeout=120)
        return out.count("Application startup complete")

    before_stats = api.get("/stats/jobs?days=90", expect_status=200)["totals"]
    before_version = pg_sql("SELECT version_num FROM public.alembic_version").strip()
    # 演练自己造的作业逐个记状态：别的流程正在同一个库里并发造数据，全局计数只能
    # 断"不会减少"，唯有按 id 逐个比才既精确又互不干扰。
    drill_jobs = {jid: api.get(f"/jobs/{jid}", expect_status=200)["status"] for jid in job_ids_of(api, ctx)}
    before_starts = startup_marker_count()
    ledger.check("重启前基线：alembic_version 在 head", before_version != "", before_version)

    wslc("restart", SERVER_NAME, timeout=180)

    # 用没有 token 的裸客户端探健康检查：nginx 在 server 不可用时会给 502/504，
    # 这条轮询序列本身就是"确实经历过不可用窗口"的证据。
    probe = ApiClient()
    codes: list[str] = []
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            codes.append(str(probe.call("GET", "/health", expect_status=None)["status"]))
        except Exception as exc:  # 连接被重置也算不可用窗口
            codes.append(type(exc).__name__)
        if codes[-1] == "200":
            break
        sleep(2)
    ledger.check("重启窗口内确实出现过不可用（不是瞬时假象）",
                 any(c != "200" for c in codes[:-1]) or len(codes) > 1, " ".join(codes[:20]))
    ledger.equal("server 恢复后 /api/health 经 nginx 回到 200", codes[-1], "200")
    ledger.check("恢复耗时在 180s 预算内", time.time() < deadline, f"探针轮次={len(codes)}")
    ledger.check("重启后新打了一次启动标记（+1）", startup_marker_count() > before_starts,
                 f"before={before_starts} after={startup_marker_count()}")

    ready = probe.call("GET", "/health/ready", expect_status=None)
    ledger.equal("/health/ready 通过（连接池与迁移都活着）", ready["status"], 200)

    # JWT 密钥来自同一份环境变量：重启不该让已签发的 token 失效。
    stale_token_ok = api.call("GET", "/jobs?limit=1&offset=0", expect_status=None)
    ledger.check("重启前签发的 token 重启后仍然可用（密钥没漂移，无需重新登录）",
                 stale_token_ok["status"] == 200, f"status={stale_token_ok['status']}")

    after_stats = api.get("/stats/jobs?days=90", expect_status=200)["totals"]
    ledger.check("全局作业数没有因为重启而减少", after_stats["jobs"] >= before_stats["jobs"],
                 json.dumps({"before": before_stats["jobs"], "after": after_stats["jobs"]}))
    changed = {jid: (before, api.get(f"/jobs/{jid}", expect_status=200)["status"])
               for jid, before in drill_jobs.items()}
    if changed:
        ledger.check("演练作业逐个状态不变（没有半途作业被写成 failed）",
                     all(a == b for b, a in changed.values()),
                     json.dumps([[k, v[0], v[1]] for k, v in changed.items() if v[0] != v[1]][:6], ensure_ascii=False))
    else:
        ledger.skip("演练作业逐个状态不变", "单独跑 --phase restart 时没有前序作业可比对")
    after_version = pg_sql("SELECT version_num FROM public.alembic_version").strip()
    ledger.equal("启动期自动迁移是幂等的（版本号没动、也没报错回滚）", after_version, before_version)

    # Worker 不受 server 重启影响：心跳在恢复后继续推进，说明队列面活着。
    def newest_age() -> float | None:
        ages = [heartbeat_age_seconds(r) for r in workers_of(api) if r["status"] != "offline"]
        return min((a for a in ages if a is not None), default=None)

    age_a = newest_age()
    sleep(20)
    age_b = newest_age()
    ledger.check("恢复后 Worker 心跳继续刷新（服务端重启不打断上报）",
                 age_a is not None and age_b is not None and age_b >= age_a and age_b < 60,
                 json.dumps({"age_before": round(age_a, 1) if age_a else None,
                             "age_after": round(age_b, 1) if age_b else None}))

    job = make_job(api, ctx, "post-restart", source_path="/nas/clean")
    done = api.wait_job_terminal(job["id"], timeout=600)
    ledger.equal("重启后新作业照样跑完", done["status"], "succeeded")
    oracle = ctx["fixtures"]["clean"]
    ledger.equal("重启后命中数仍等于 oracle", job_matched_rows(api, job["id"]), oracle["expect_total_matches"])
    frames = list(api.job_events(job["id"], max_events=50))
    ledger.check("重启后 SSE 进度流仍可订阅并带终态帧",
                 bool(frames) and frames[-1].get("status") in TERMINAL_JOB_STATUSES,
                 json.dumps(frames[-1], ensure_ascii=False)[:200] if frames else "无帧")
    ctx["post_restart_job"] = job["id"]


# ============================================================================
# W6 迁移：主库幂等 -> 全新库等价 -> downgrade 往返（一律避开主库）
# ============================================================================
CLAIM_INDEX = "ix_file_tasks_claim"
RECOVER_INDEX = "ix_file_tasks_recover"
IT_DB = "logharvest_e2e_it"  # pytest 集成测试库：夹具会 DROP SCHEMA public，同样禁止当靶子


def sql_scalar(sql: str, *, database: str | None = None) -> str:
    return pg_sql(sql, database=database).strip()


def sql_write_scalar(sql: str, *, database: str | None = None) -> str:
    """带 RETURNING 的写语句取值。

    `psql -At` 会在结果行之后再补一行命令标签（`INSERT 0 1`），直接 strip 整段会把
    标签粘进返回值——探针断言因此变成"值里带换行"的假 FAIL。这里取第一行非空结果，
    并显式剥掉尾部可能出现的命令标签，保证拿到的是纯标量。
    """
    lines = [line.strip() for line in pg_sql(sql, database=database).splitlines() if line.strip()]
    while lines and re.fullmatch(r"(INSERT|UPDATE|DELETE) \d+ \d+", lines[-1]):
        lines.pop()
    return lines[0] if lines else ""


def alembic_version_of(*, database: str | None = None) -> str:
    """库里实际落到的 revision（alembic_version 只有一行）。"""
    return sql_scalar("SELECT version_num FROM public.alembic_version", database=database)


def db_signature(*, database: str | None = None) -> dict[str, str]:
    """结构指纹：表名 -> 列清单，NOT NULL 且无默认值的列带 *。

    拿它比"全新库 upgrade head"和"就地升级到 head 的老库"是否逐字一致，是这次演练里
    最值钱的一条证据：客户直接升级后的库结构必须和新装一模一样，否则回滚承诺是空的。
    """
    raw = sql_scalar(
        "SELECT table_name||'|'||string_agg("
        "column_name||(CASE WHEN is_nullable='NO' AND column_default IS NULL THEN '*' ELSE '' END),"
        " ',' ORDER BY ordinal_position) "
        "FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name <> 'alembic_version' "
        "GROUP BY table_name ORDER BY table_name",
        database=database,
    )
    sig: dict[str, str] = {}
    for line in raw.splitlines():
        if "|" in line:
            table, cols = line.split("|", 1)
            sig[table.strip()] = cols
    return sig


def index_names(*, database: str | None = None) -> list[str]:
    raw = sql_scalar(
        "SELECT indexname FROM pg_indexes WHERE schemaname='public' AND tablename <> 'alembic_version' ORDER BY indexname",
        database=database,
    )
    return [line.strip() for line in raw.splitlines() if line.strip()]


def row_counts(*, database: str | None = None) -> dict[str, int]:
    """每张业务表的行数。用来钉住「回滚脚本只动索引、不动数据」这句承诺。"""
    tables = sorted(db_signature(database=database))
    sql = " UNION ALL ".join(f"SELECT '{t}', count(*) FROM public.\"{t}\"" for t in tables)
    raw = sql_scalar(sql, database=database)
    counts: dict[str, int] = {}
    for line in raw.splitlines():
        if "|" in line:
            table, num = line.rsplit("|", 1)
            counts[table.strip()] = int(num)
    return counts


def mig_guard(db_name: str) -> str:
    """一次性迁移库的连接串，外加三道硬闸。

    downgrade 会真删对象，所以这条路径宁可自己抛异常，也绝不把 URL 递错库：
    主库、集成测试库、以及任何"库名段不是一次性库"的串都当场拦下。
    """
    url = mig_url(db_name)
    if db_name == PG_DB:
        raise RuntimeError(f"迁移往返拒绝指向主库 {PG_DB}")
    if db_name == IT_DB:
        raise RuntimeError(f"迁移往返拒绝指向集成测试库 {IT_DB}")
    if url.rsplit("/", 1)[1] != db_name:
        raise RuntimeError(f"连接串库名段与 {db_name} 不符：{url}")
    return url


def phase_migrations(ledger: Ledger, api: ApiClient, ctx: dict[str, Any]) -> None:
    ledger.phase("W6 迁移：主库幂等 + 全新库等价 + downgrade 往返")

    head_before = alembic_version_of()
    sig_before = db_signature()
    idx_before = index_names()
    ledger.check("主库在 head", "(head)" in alembic("current"), alembic("current")[:160])

    # 幂等性：服务端每次启动都自动 upgrade head，重复执行必须是空操作。
    alembic("upgrade", "head")
    alembic("upgrade", "head")
    ledger.equal("主库连续两次 upgrade head 版本号不动", alembic_version_of(), head_before)
    ledger.equal("主库连续两次 upgrade head 结构不动", db_signature(), sig_before)

    url = mig_guard(MIG_DB)
    ledger.check("一次性库名与主库/测试库都不同（闸门生效）", MIG_DB != PG_DB and MIG_DB != IT_DB, MIG_DB)

    code, out = wslc_soft(
        "exec", PG_NAME, "psql", "-U", PG_USER, "-d", "postgres", "-At", "-c",
        f'CREATE DATABASE "{MIG_DB}"', timeout=240,
    )
    ledger.check("一次性迁移库已创建", code == 0, out[:200])

    # 「全新部署」= 空库直接 upgrade head。
    alembic("upgrade", "head", database_url=url)
    ledger.equal("全新库 upgrade head 落在同一 revision", alembic_version_of(database=MIG_DB), head_before)
    fresh_sig = db_signature(database=MIG_DB)
    ledger.equal("全新库与就地升级库的表集合一致", sorted(fresh_sig), sorted(sig_before))
    drift = {t: {"fresh": fresh_sig.get(t), "main": sig_before.get(t)} for t in set(fresh_sig) | set(sig_before)
             if fresh_sig.get(t) != sig_before.get(t)}
    ledger.check("逐表列清单（含 NOT NULL 标记）也一致", not drift, json.dumps(drift, ensure_ascii=False)[:400] or "无差异")
    ledger.equal("全新库索引集合与主库一致", sorted(index_names(database=MIG_DB)), sorted(idx_before))

    # 探针行：空库迁移完是没有业务数据的，行数比对会全 0 自证清白——那是假绿。
    probe_id = sql_write_scalar("INSERT INTO roles (code, name) VALUES ('e2e_mig_probe', '升级演练探针') RETURNING id",
                                database=MIG_DB)
    counts_before = row_counts(database=MIG_DB)
    ledger.check("探针行写入一次性库（让回滚的数据比对有意义）", probe_id.isdigit(), f"roles.id={probe_id}")

    # 已知不对称，这里如实记下来而不是绕开：0001 用 metadata.create_all 建基线，全新库在
    # 0001 就带上了 0006 才"补"的两条索引；于是 downgrade -1 会连同名字段一起删掉。
    had_claim = CLAIM_INDEX in index_names(database=MIG_DB)
    alembic("downgrade", "-1", database_url=url)
    ledger.equal("downgrade -1 退到 0005", alembic_version_of(database=MIG_DB), "0005")
    after_down = index_names(database=MIG_DB)
    ledger.check("0006 的两条部分索引确实被回滚脚本删掉",
                 CLAIM_INDEX not in after_down and RECOVER_INDEX not in after_down,
                 sorted(after_down)[:8])
    ledger.equal("回滚只动索引，业务表行数不变（数据没被连带删掉）", row_counts(database=MIG_DB), counts_before)

    # 0006 的 upgrade 先问再建（_has_index），所以老库补建、新库重建，两条路径收敛到同一结构。
    alembic("upgrade", "head", database_url=url)
    ledger.equal("再 upgrade head 回到 head", alembic_version_of(database=MIG_DB), head_before)
    ledger.equal("往返之后索引集合复原", sorted(index_names(database=MIG_DB)), sorted(idx_before))
    ledger.equal("往返之后结构仍与主库逐字一致", db_signature(database=MIG_DB), sig_before)
    probe_after = sql_scalar("SELECT count(*) FROM roles WHERE code='e2e_mig_probe' AND id=%s" % probe_id,
                             database=MIG_DB)
    ledger.equal("探针行活过了 downgrade -> upgrade 往返", probe_after, "1")

    # 上面所有动作都必须只打在一次性库上：主库的版本号、结构、索引三样都得原样。
    ledger.equal("主库版本号没被迁移往返碰到", alembic_version_of(), head_before)
    ledger.equal("主库结构没被迁移往返碰到", db_signature(), sig_before)
    ledger.equal("主库索引没被迁移往返碰到", sorted(index_names()), sorted(idx_before))

    code, out = wslc_soft(
        "exec", PG_NAME, "psql", "-U", PG_USER, "-d", "postgres", "-At", "-c",
        f'DROP DATABASE IF EXISTS "{MIG_DB}" WITH (FORCE)', timeout=240,
    )
    ledger.check("一次性库用完即删（不给环境留残库）", code == 0, out[:200])
    ledger.equal("残库确实不存在",
                 sql_scalar(f"SELECT count(*) FROM pg_database WHERE datname='{MIG_DB}'", database="postgres"), "0")
    ctx["mig_db_dropped"] = True
    ctx["mig_head"] = head_before
    ctx["mig_signature"] = sig_before


# ============================================================================
# W7 数据面对账：夹具重跑 + API 读数与 PostgreSQL 原始聚合逐项核对
# ============================================================================
def sql_int(sql: str, *, database: str | None = None) -> int:
    return int(sql_scalar(sql, database=database) or "0")


def sql_pairs(sql: str, *, database: str | None = None) -> dict[str, int]:
    raw = sql_scalar(sql, database=database)
    out: dict[str, int] = {}
    for line in raw.splitlines():
        if "|" in line:
            key, num = line.rsplit("|", 1)
            out[key.strip()] = int(num)
    return out


def job_ids_of(api: ApiClient, ctx: dict[str, Any]) -> list[int]:
    """本次演练自己造的作业 id。对账全部按这个集合做，避免和并发跑的其他流程互相干扰。"""
    return sorted({int(x) for x in ctx.get("drill_job_ids", []) if str(x).strip()})


def work_dir_entries(name: str) -> list[str]:
    """Worker 容器里 /local-tmp/logharvest/<worker_id> 的残留条目（任务级目录跑完就该被删）。"""
    code, out = wslc_soft("exec", name, "ls", "-1", f"{WORK_DIR_ROOT}/{name}", timeout=120)
    if code != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def phase_reconcile(ledger: Ledger, api: ApiClient, ctx: dict[str, Any]) -> None:
    ledger.phase("W7 数据面对账：夹具重跑 + API 与库内聚合逐项核对")

    fixtures = ctx["fixtures"]
    reconcile_jobs: list[int] = []
    # 这四组覆盖：普通归档+裸文件、6 种压缩格式、GBK/UTF-8 混合、zip-slip 防御。
    # 升级最怕的就是"版本换了，抓取结果悄悄变了"，所以每条都按规则级 oracle 比。
    for case in ("clean", "formats", "encoding", "traversal"):
        spec = fixtures[case]
        job = make_job(api, ctx, f"rc-{case}", source_path=spec["source"])
        reconcile_jobs.append(job["id"])
        done = api.wait_job_terminal(job["id"], timeout=900)
        ledger.equal(f"{case} 作业在升级后的池子上跑完", done["status"], "succeeded")
        got = matches_by_rule(api, job["id"])
        ledger.equal(f"{case} 命中按规则逐条对齐 oracle", got,
                     {k: int(v) for k, v in (spec["expect_matches_by_rule"] or {}).items()})
        ledger.equal(f"{case} 文件任务数等于 oracle", len(job_file_tasks(api, job["id"])), spec["expect_total_files"])
        ledger.equal(f"{case} 任务级命中计数 = /matches 行数",
                     job_matched_rows(api, job["id"]), sum(got.values()))

    ids = job_ids_of(api, ctx)
    id_list = ",".join(str(i) for i in ids)
    ledger.check("对账范围只取本次演练造出的作业（与并发流程互不干扰）", bool(ids), f"drill jobs={id_list[:200]}")

    # ---- API 读数 vs 库里的真实行数：命中不能多（重复入库）也不能少（漏写） ----
    api_vs_sql = [
        {"job_id": jid,
         "api_rows": len(list(iter_matches(api, page=500, job_id=jid))),
         "sql_rows": sql_int(f"SELECT count(*) FROM matches WHERE job_id={jid}")}
        for jid in ids
    ]
    mismatched = [r for r in api_vs_sql if r["api_rows"] != r["sql_rows"]]
    ledger.check("每个演练作业的 /matches 行数与 matches 表行数一致",
                 not mismatched, json.dumps(mismatched[:4], ensure_ascii=False) or f"共核对 {len(ids)} 个作业")

    # 重复入库是升级演练最典型的事故（回收重跑、attempt 重写）。唯一键取业务语义四元组，
    # 而不是 matches.id——id 天然不重复，拿它查重等于什么都没查。
    dup = sql_scalar(
        "SELECT string_agg(k, E'\\n' ORDER BY k) FROM ("
        "  SELECT file_task_id||'/'||member_path||'/'||line_no||'/'||rule_id AS k"
        "  FROM matches WHERE job_id IN (" + id_list + ")"
        "  GROUP BY 1 HAVING count(*) > 1) x"
    )
    ledger.check("演练作业没有重复命中（回收/重试按任务删除后重写）", dup == "", dup[:300])

    # ---- 状态机不变量：库里不该出现自相矛盾的行 ----
    invariants = {
        "pending 但没排队时间（领取索引会漏它）": sql_int(
            "SELECT count(*) FROM file_tasks WHERE status='pending' AND scheduled_at IS NULL"),
        "running 但没有 worker/心跳（僵死回收抓不到）": sql_int(
            "SELECT count(*) FROM file_tasks WHERE status='running' AND (worker_id IS NULL OR heartbeat_at IS NULL)"),
        "succeeded 但没有结束时间": sql_int(
            "SELECT count(*) FROM file_tasks WHERE status='succeeded' AND finished_at IS NULL"),
        "attempts>0 却没有尝试历史（审计断链）": sql_int(
            f"SELECT count(*) FROM file_tasks ft WHERE ft.job_id IN ({id_list}) AND ft.attempts > 0 "
            "AND NOT EXISTS (SELECT 1 FROM file_task_attempts ta WHERE ta.file_task_id = ft.id)"),
        "尝试行数与 attempts 计数不等": sql_int(
            f"SELECT count(*) FROM file_tasks ft WHERE ft.job_id IN ({id_list}) AND ft.attempts > 0 "
            "AND (SELECT count(*) FROM file_task_attempts ta WHERE ta.file_task_id = ft.id) <> ft.attempts"),
        "attempt_no 不连续（1..n 中间断了）": sql_int(
            "SELECT count(*) FROM (SELECT file_task_id, count(*) c, max(attempt_no) m FROM file_task_attempts "
            f"WHERE file_task_id IN (SELECT id FROM file_tasks WHERE job_id IN ({id_list})) "
            "GROUP BY file_task_id HAVING count(*) <> max(attempt_no)) x"),
    }
    ledger.check("文件任务状态机不变量全部成立", all(v == 0 for v in invariants.values()),
                 json.dumps({k: v for k, v in invariants.items() if v}, ensure_ascii=False) or "六项均为 0")

    # ---- 全局计数器：页面显示的数字必须等于一次 group by ----
    busy = sql_int("SELECT count(*) FROM file_tasks WHERE status IN ('pending','running')")
    status_api = api.get("/system/status", expect_status=200)
    status_sql = {k: v for k, v in sql_pairs("SELECT status, count(*) FROM file_tasks GROUP BY status").items() if v}
    if busy == 0:
        ledger.equal("system/status 的 file_tasks 分布 = 库内真实分布（静默期精确比对）", status_sql,
                     {k: int(v) for k, v in (status_api.get("file_tasks") or {}).items() if int(v)})
    else:
        ledger.check("system/status 的 file_tasks 分布与库内一致（非静默期，容许在跑任务造成的差）",
                     sum(status_sql.values()) == sum(int(v) for v in (status_api.get("file_tasks") or {}).values()),
                     json.dumps({"in_flight": busy, "api": status_api.get("file_tasks"), "sql": status_sql},
                                ensure_ascii=False)[:300])
    ledger.check("演练结束后没有本流程的滞留任务",
                 sql_int(f"SELECT count(*) FROM file_tasks WHERE job_id IN ({id_list}) "
                         "AND status IN ('pending','running')") == 0,
                 f"file_tasks pending/running 总数（含其他流程）={busy}")

    totals = api.get("/stats/jobs?days=90", expect_status=200)["totals"]
    window = "j.created_at >= now() - interval '90 days'"
    jobs_sql = sql_int(f"SELECT count(*) FROM jobs j WHERE {window}")
    files_sql = sql_int(f"SELECT count(*) FROM file_tasks ft JOIN jobs j ON j.id=ft.job_id WHERE {window}")
    matched_sql = sql_int(f"SELECT coalesce(sum(ft.matched_rows),0) FROM file_tasks ft JOIN jobs j ON j.id=ft.job_id WHERE {window}")
    # 统计窗口按 jobs.created_at 切，文件与命中都通过所属作业计入——这里就是照这个口径复算。
    ledger.check("stats/jobs 的作业数与库内 90 天窗口一致（并发写入容许 +2）",
                 abs(int(totals["jobs"]) - jobs_sql) <= 2, json.dumps({"api": totals["jobs"], "sql": jobs_sql}))
    ledger.check("stats/jobs 的文件数与库内一致（并发容许 ±5）",
                 abs(int(totals["files_total"]) - files_sql) <= 5, json.dumps({"api": totals["files_total"], "sql": files_sql}))
    ledger.check("stats/jobs 的命中总数与任务计数求和一致（并发容许 ±2%）",
                 abs(int(totals["matched_rows_total"]) - matched_sql) <= max(50, matched_sql // 50),
                 json.dumps({"api": totals["matched_rows_total"], "sql": matched_sql}))

    # ---- 本地临时盘：两类目录要分开核，不能笼统要求"零残留" ----
    # 正常跑完的任务由 processor 的 finally 删掉自己的任务目录；被 SIGKILL 打断的那次没有
    # finally 可跑，目录只能等下一次 Worker 启动时的孤儿清扫——而清扫只删 mtime 超过
    # ORPHAN_MAX_AGE_SECONDS=3600s 的条目（service/src/worker/main.py:305）。首轮实测就是
    # 这条笼统断言把 crash 作业留下的 185 号目录算成了"清理漏了"，其实它是设计内的残留。
    task_ids = {line.strip() for line in sql_scalar(
        f"SELECT id FROM file_tasks WHERE job_id IN ({id_list})").splitlines() if line.strip()}
    interrupted = {line.strip() for line in sql_scalar(
        f"SELECT ft.id FROM file_tasks ft WHERE ft.job_id IN ({id_list}) AND EXISTS ("
        f"SELECT 1 FROM file_task_attempts a WHERE a.file_task_id = ft.id AND a.status = 'timeout')"
    ).splitlines() if line.strip()}
    leftovers: dict[str, list[str]] = {}
    for name in (WORKER_1_NAME, WORKER_2_NAME):
        if not container_exists(name):
            continue
        hit = sorted(entry for entry in work_dir_entries(name) if entry in task_ids)
        if hit:
            leftovers[name] = hit
    flat = sorted({tid for hit in leftovers.values() for tid in hit})
    leaked = [tid for tid in flat if tid not in interrupted]
    ledger.check("跑完的任务不在临时盘留目录（finally 清理生效）",
                 not leaked, json.dumps({"leftovers": leftovers, "leaked": leaked,
                                         "checked_tasks": len(task_ids)}, ensure_ascii=False)[:300])
    ledger.check("残留只来自被强杀打断的任务（等启动孤儿清扫，年龄闸门 3600s 内不删是设计）",
                 all(tid in interrupted for tid in flat),
                 json.dumps({"leftovers": leftovers, "interrupted_tasks": sorted(interrupted)},
                            ensure_ascii=False)[:300])

    # 上面那条把残留归因给"年龄闸门还没到"，就得真验一次闸门本身：造一个 mtime 拨到两小时前
    # 的目录和一个刚建的目录，重启 Worker 后旧的必须被 startup 清扫删掉、新的必须还在。
    # 只删自己工作目录下的条目，所以拿托管 Worker-1 的目录做实验不影响别人。
    probe = WORKER_1_NAME
    old_dir = f"{WORK_DIR_ROOT}/{probe}/{ORPHAN_PROBE_OLD}"
    new_dir = f"{WORK_DIR_ROOT}/{probe}/{ORPHAN_PROBE_NEW}"
    wslc("exec", probe, "sh", "-c",
         f"mkdir -p '{old_dir}' '{new_dir}' && touch -m -d '2 hours ago' '{old_dir}'", timeout=120)
    row = by_id(workers_of(api), probe) or {}
    version = row.get("version") or BASE_VERSION
    image = WORKER_V2_IMAGE if version == V2_VERSION else WORKER_IMAGE
    graceful_stop(probe)
    start_worker(probe, image, version)
    back = wait_until(
        lambda: (lambda r: r if r and r["status"] in ("online", "busy") else None)(by_id(workers_of(api), probe)),
        label=f"{probe} 重启后重新在线", timeout=180, interval=3)
    ledger.equal("重启沿用同一个自报版本（清扫发生在同 id 的工作目录上）", back["version"], version)
    entries = work_dir_entries(probe)
    ledger.check("超过年龄闸门的孤儿目录被启动清扫删掉", ORPHAN_PROBE_OLD not in entries,
                 json.dumps(entries, ensure_ascii=False)[:300])
    ledger.check("没到闸门的近期目录不会被误删（清扫按 mtime 筛，不是一锅端）", ORPHAN_PROBE_NEW in entries,
                 json.dumps(entries, ensure_ascii=False)[:300])
    wslc_soft("exec", probe, "rm", "-rf", new_dir, timeout=120)
    ledger.check("验证用的目录已经回收（不给环境留残目录）",
                 ORPHAN_PROBE_NEW not in work_dir_entries(probe),
                 json.dumps(work_dir_entries(probe), ensure_ascii=False)[:300])

    ctx["reconcile_jobs"] = reconcile_jobs


# ============================================================================
# W8 回滚到基线：v2 -> 0.1.0 + 冒烟 + 环境复原校验
# ============================================================================
def restore_foreign_workers(ledger: Ledger, api: ApiClient, ctx: dict[str, Any]) -> None:
    """把 W0 停掉的非托管 Worker 原样 start 回去。

    W0 只 stop 不 rm 就是为了这一步：演练借用别人的机器只是要一个干净的计数口径，
    跑完必须把环境复原成别人离开时的样子，否则 limits 流程回来发现 worker-3 不见了。
    """
    names = ctx.get("foreign_stopped") or []
    if not names or ctx.get("foreign_restored"):
        return
    for name in names:
        wslc_soft("start", name, timeout=120)
    wait_until(lambda: all((by_id(workers_of(api), n) or {}).get("status") in ("online", "busy") for n in names),
               label=f"非托管 Worker 重新上线：{names}", timeout=180, interval=3)
    ctx["foreign_restored"] = True
    ledger.check("W0 停掉的非托管 Worker 容器已回到运行中",
                 all(container_is_running(n) for n in names),
                 json.dumps([[n, (by_id(workers_of(api), n) or {}).get("status")] for n in names], ensure_ascii=False))


def phase_restore(ledger: Ledger, api: ApiClient, ctx: dict[str, Any]) -> None:
    ledger.phase("W8 回滚基线：Worker 换回 0.1.0 并冒烟")

    for name in (WORKER_1_NAME, WORKER_2_NAME):
        graceful_stop(name)
    for name in (WORKER_1_NAME, WORKER_2_NAME):
        start_worker(name, WORKER_IMAGE, BASE_VERSION)

    rows = wait_until(lambda: managed_rows(api) if online_count(api) == 2 else None,
                      label="两条基线 Worker 回到 online", timeout=180, interval=3)
    live = [r for r in rows if r["status"] in ("online", "busy")]
    ledger.equal("回滚后托管池恰好两条在线", len(live), 2)
    ledger.equal("回滚后在线版本集只剩基线", sorted({r["version"] for r in live}), [BASE_VERSION])
    status = api.get("/system/status", expect_status=200)
    ledger.equal("system/status 的 workers_online 回到 2", status["workers_online"], 2)

    # 基线镜像不该带 v2 的构建期标记：读不到才证明容器真的换回去了，而不只是版本号被改。
    code, out = wslc_soft("exec", WORKER_1_NAME, "cat", "/etc/logharvest-release", timeout=120)
    ledger.check("基线容器里没有 v2 镜像的构建期标记", code != 0, f"code={code} out={out[:120]}")

    job = make_job(api, ctx, "restored-smoke", source_path="/nas/clean")
    done = api.wait_job_terminal(job["id"], timeout=600)
    ledger.equal("回滚后的池子照样跑完作业", done["status"], "succeeded")
    ledger.equal("回滚后命中数仍等于 oracle", job_matched_rows(api, job["id"]),
                 ctx["fixtures"]["clean"]["expect_total_matches"])

    # 整场演练动过镜像、动过容器、动过 alembic——但主库结构必须与 W6 开始时逐字一致。
    if ctx.get("mig_signature"):
        ledger.equal("主库结构与迁移前一致", db_signature(), ctx["mig_signature"])
        ledger.equal("主库 revision 仍在 head", alembic_version_of(), ctx.get("mig_head", ""))
    ledger.check("一次性迁移库没有残留",
                 sql_scalar(f"SELECT count(*) FROM pg_database WHERE datname='{MIG_DB}'", database="postgres") == "0",
                 MIG_DB)
    # 池子回基线后才把别人的机器放回来：顺序反了的话上面那句 workers_online==2 就自打脸。
    restore_foreign_workers(ledger, api, ctx)
    ctx["restored"] = True



# ============================================================================
# 编排
# ============================================================================
PHASES = ["pre", "drain", "rolling", "crash", "restart", "migrations", "reconcile", "restore"]
RUNNERS: dict[str, Callable[[Ledger, ApiClient, dict[str, Any]], None]] = {
    "pre": phase_pre,
    "drain": phase_drain,
    "rolling": phase_rolling,
    "crash": phase_crash,
    "restart": phase_restart,
    "migrations": phase_migrations,
    "reconcile": phase_reconcile,
    "restore": phase_restore,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="LogHarvest 升级演练（会替换 Worker 容器、重启 server；请在其他 E2E 流程空档跑）")
    parser.add_argument("--phase", default="all", help=f"逗号分隔：{','.join(PHASES)} 或 all")
    parser.add_argument("--keep-v2", action="store_true",
                        help="跑完不换回基线镜像（调试用）。默认恢复 0.1.0，让后续 UI/API 流程仍看到 e2e/up.sh 承诺的环境")
    args = parser.parse_args(argv)

    wanted = list(PHASES) if args.phase == "all" else [p.strip() for p in args.phase.split(",") if p.strip()]
    unknown = [p for p in wanted if p not in PHASES]
    if unknown:
        print(f"!! 未知阶段：{unknown}，可选 {PHASES}", flush=True)
        return 2

    # pre 是所有阶段的地基：它确认全栈健康、两套 Worker 镜像在位，并把 NAS 数据源、
    # canonical 规则集与夹具 oracle 放进 ctx——缺了它后面每条断言都是空中楼阁。
    pipeline = ["pre"] + [p for p in wanted if p != "pre"]
    if args.keep_v2 and "restore" in pipeline:
        pipeline.remove("restore")

    ledger = Ledger("Upgrade Flow")
    api = ApiClient(base=API_BASE)
    try:
        api.login(ADMIN_ENV_USERNAME, ADMIN_ENV_PASSWORD)
    except (ApiError, OSError) as exc:
        ledger.fail_phase("登录前置", exc)
        print(f"!! 登录失败，栈可能没起来：先 bash e2e/up.sh（{type(exc).__name__}: {exc}）", flush=True)
        ledger.summary()
        return 2

    ctx: dict[str, Any] = {}
    try:
        for name in pipeline:
            try:
                RUNNERS[name](ledger, api, ctx)
            except Exception as exc:  # noqa: BLE001 - 阶段崩了也要把已跑出的证据落盘
                ledger.fail_phase(f"{name} 阶段异常", exc)
                print(f"!! 阶段 {name} 中断：{type(exc).__name__}: {exc}", flush=True)
                if name == "pre":
                    break
    finally:
        # 只要动过容器（滚动替换或崩溃回收），无论跑到哪一步都得把池子换回基线：
        # 同一套环境上还跑着别人的 UI/API 流程，它们按 0.1.0 分组断言。
        touched = bool(ctx.get("upgraded") or ctx.get("crash_recovered"))
        if touched and not args.keep_v2 and "restore" not in pipeline:
            print("---- 兜底回滚基线（前序阶段异常中断）" + "-" * 30, flush=True)
            try:
                phase_restore(ledger, api, ctx)
            except Exception as exc:  # noqa: BLE001
                ledger.fail_phase("兜底回滚异常", exc)
        # 一次性迁移库不许留下：W6 自己会删，中途崩在这里也补一刀（IF EXISTS，幂等）。
        wslc_soft("exec", PG_NAME, "psql", "-U", PG_USER, "-d", "postgres", "-At", "-c",
                  f'DROP DATABASE IF EXISTS "{MIG_DB}" WITH (FORCE)', timeout=240)
        # W0 停别人的机器只是要一个干净的计数口径：无论跑到哪一步、是否 --keep-v2，
        # 都必须 start 回去，否则 limits/UI 流程回来会发现 worker-3 "消失"了。
        if ctx.get("foreign_stopped") and not ctx.get("foreign_restored"):
            print("---- 复原非托管 Worker（把 W0 停下的容器 start 回去）" + "-" * 24, flush=True)
            try:
                restore_foreign_workers(ledger, api, ctx)
            except Exception as exc:  # noqa: BLE001
                ledger.fail_phase("复原非托管 Worker 异常", exc)

    summary = ledger.summary()
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
