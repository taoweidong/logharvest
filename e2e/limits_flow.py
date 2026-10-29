"""R4 紧预算 Worker：解压预算越界 + 命中上限失败清场（真实容器，不是单测替身）。

为什么要单独一只 Worker：默认预算是 1GiB 单成员 / 4GiB 整包 / 10000 成员 / 压缩比 500 /
20 万命中（service/src/domain/archive/reader.py:35-38、processor.py:32），夹具的
``limits`` 与 ``flood`` 在这个预算下必然 succeeded —— api_flow 已经按这个口径对过账。
越界分支（规格 §11 压缩炸弹、§7.4 命中上限）在默认预算里永远不会触发，只能把预算压到
夹具尺寸附近才跑得出来。所以这里用 ``bash e2e/worker3.sh start`` 拉起第三只 Worker，
再把两只默认 Worker drain 掉，让它独占认领：

  L0 紧预算 Worker 上线，两只默认 Worker 进入 draining（版本与 id 先钉死归属）；
  L1 单成员越界（max_member_bytes）→ 任务 partial，坏成员被牺牲、其余成员照常落库；
  L2 成员数越界（max_members）    → 任务 partial，扫描在第 21 个成员处停止整包；
  L3 命中数越界（max_matches_per_task）→ 任务 failed，且**失败清场**：触顶前按 30 一批
     真实写进 matches 表的 90 行必须被删干净（用 PostgreSQL 序列号做"确实插入过"的证据）；
  L4 停掉紧预算 Worker、恢复默认 Worker 后人工重试同一任务 → succeeded、6500 行全量入库。

跑法（栈已由 bash e2e/up.sh 拉起，且 api_flow 已建好 e2e-nas 数据源）：
  service/.venv/Scripts/python.exe e2e/limits_flow.py
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from api_flow import create_job, iter_matches  # noqa: E402
from client import API_BASE, ApiClient, ApiError, Ledger, load_fixtures  # noqa: E402
from runtime_flow import (  # noqa: E402
    RUN_ID,
    TASK_FIELDS,
    bootstrap,
    counters,
    tasks_of,
    wait_until,
    workers_of,
)

E2E_DIR = Path(__file__).resolve().parent
WORKER3_SCRIPT = E2E_DIR / "worker3.sh"
BASH = shutil.which("bash") or "bash"

# 只读地取证用：紧预算用例要证明"失败清场"删掉的是真实落库的行，光看 API 的最终状态
# 分不清"根本没写"和"写了又删"。PG 容器里的序列号给得起这个证据（插入即推进，删除不回退）。
PG_CONTAINER = os.environ.get("PG_CONTAINER", "lh-e2e-pg")
PG_USER = os.environ.get("PG_USER", "logharvest")
PG_DB = os.environ.get("PG_DB", "logharvest_e2e")


def worker3(action: str) -> str:
    """调用 e2e/worker3.sh（start/stop/budget），失败就把它的 stderr 一起抛出。"""
    result = subprocess.run(
        [BASH, str(WORKER3_SCRIPT), action],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError(f"worker3.sh {action} 失败（exit {result.returncode}）：{result.stderr.strip()[:400]}")
    return result.stdout


def read_budget() -> dict[str, int]:
    """预算常量的唯一来源是 worker3.sh，这里读回来用，绝不在两个文件里各写一遍数字。"""
    raw = dict(
        line.split("=", 1)
        for line in worker3("budget").splitlines()
        if "=" in line and not line.startswith("-e")
    )
    keys = (
        "LOGHARVEST_ARCHIVE_MAX_MEMBER_BYTES",
        "LOGHARVEST_ARCHIVE_MAX_MEMBERS",
        "LOGHARVEST_MAX_MATCHES_PER_TASK",
        "LOGHARVEST_MATCHES_BATCH_SIZE",
    )
    missing = [key for key in keys if key not in raw]
    if missing:
        raise RuntimeError(f"worker3.sh budget 缺少常量 {missing}")
    return {key.split("_", 1)[1].lower(): int(raw[key]) for key in keys}


def psql_scalar(sql: str) -> str:
    result = subprocess.run(
        [
            "wslc", "exec", PG_CONTAINER,
            "psql", "-U", PG_USER, "-d", PG_DB, "-At", "-c", sql,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"psql 失败：{result.stderr.strip()[:300]}")
    return result.stdout.strip().replace("\r", "")


def psql_int(sql: str) -> int:
    text = psql_scalar(sql)
    if not text.lstrip("-").isdigit():
        raise RuntimeError(f"psql 结果不是整数：{text!r}（SQL={sql}）")
    return int(text)


def drain_ids(ledger: Ledger, api: ApiClient, ids: list[str]) -> None:
    """只排空指定 Worker：紧预算用例要让任务全部落到第三只上，其余的必须真的不认领。"""
    for worker_id in ids:
        body = api.post(f"/workers/{worker_id}/drain", expect_status=200)
        ledger.equal(f"drain {worker_id}", (body["id"], body["drain_requested"]), (worker_id, True))
    rows = wait_until(
        lambda: [w for w in workers_of(api) if w["id"] in ids and w["status"] == "draining"]
        if sum(1 for w in workers_of(api) if w["id"] in ids and w["status"] == "draining") == len(ids)
        else [],
        timeout=45.0,
        name=f"{ids} 全部自报 draining",
        interval=1.0,
    )
    ledger.check(f"{len(ids)} 只默认 Worker 全部 draining", len(rows) == len(ids), str([r["id"] for r in rows]))


def resume_ids(ledger: Ledger, api: ApiClient, ids: list[str]) -> None:
    for worker_id in ids:
        body = api.post(f"/workers/{worker_id}/resume", expect_status=200)
        ledger.equal(f"resume {worker_id}", (body["drain_requested"], body["status"]), (False, "online"))
    wait_until(
        lambda: all(not w["drain_requested"] for w in workers_of(api) if w["id"] in ids),
        timeout=30.0,
        name=f"{ids} 的 drain 标记清零",
        interval=1.0,
    )


def task_by_name(tasks: list[dict[str, Any]], name: str) -> dict[str, Any]:
    matches = [task for task in tasks if task["file_name"] == name]
    if len(matches) != 1:
        raise KeyError(f"任务里没有唯一的 {name}（找到 {len(matches)} 条：{[t['file_name'] for t in tasks]}）")
    return matches[0]


def phase_workers(ledger: Ledger, api: ApiClient, ctx: dict[str, Any], st: dict[str, Any]) -> None:
    """L0 紧预算 Worker 上线并独占认领。"""
    ledger.phase("L0 紧预算 Worker 上线，默认 Worker 排空")
    budget = read_budget()
    out = worker3("start")
    ledger.check("worker3.sh start 成功", "worker ok" in out, out.strip()[:200])
    name = "lh-e2e-worker-3"
    version = "0.1.0-budget"
    w3 = wait_until(
        lambda: next((w for w in workers_of(api) if w["id"] == name and w["status"] == "online"), None),
        timeout=60.0,
        name=f"{name} 注册为 online",
        interval=1.0,
    )
    ledger.equal(f"{name} 上报的 Worker 版本", w3["version"], version)
    ledger.check(f"{name} 未被打扰（无 drain 标记）", w3["drain_requested"] is False, str(w3["drain_requested"]))
    ledger.check(
        f"{name} 上报了临时盘余量（心跳字段齐全）",
        isinstance(w3["temp_disk_free_bytes"], int) and w3["temp_disk_free_bytes"] > 0,
        str(w3["temp_disk_free_bytes"]),
    )
    defaults = [w["id"] for w in workers_of(api) if w["id"] != name]
    ledger.check("默认 Worker 是两只", len(defaults) == 2, str(defaults))
    drain_ids(ledger, api, defaults)
    # 排空只该影响被点名的两只；把"顺带把第三只也停了"这类缺陷挡在认领之前。
    after = {w["id"]: w for w in workers_of(api)}
    ledger.equal(f"{name} 在两只默认 Worker draining 后仍 online", after[name]["status"], "online")
    ledger.equal("任务归属预检：池子里只有 1 只可认领", sum(1 for w in after.values() if w["status"] == "online"), 1)
    st["env"] = {"budget": budget, "defaults": defaults, "name": name, "version": version}


def phase_budget(ledger: Ledger, api: ApiClient, ctx: dict[str, Any], st: dict[str, Any]) -> None:
    """L1/L2 解压预算越界：单成员超限只牺牲该成员，成员数超限停止整包——两者都落 partial。

    期望全部从夹具 oracle 现算：被牺牲/被裁掉的成员名来自 ``fixtures.json`` 的成员顺序
    （zip 按 central directory 顺序遍历，与写入顺序一致），幸存命中数来自每个成员的
    ``matches``，一个都不手抄。
    """
    ledger.phase("L1/L2 解压预算越界（max_member_bytes / max_members）")
    env = st["env"]
    budget = env["budget"]
    worker_name = env["name"]
    spec = load_fixtures()["cases"]["limits"]

    big_members = spec["per_file"]["big_member.zip"]["members"]
    cap_member_bytes = budget["archive_max_member_bytes"]
    sacrificed = max(big_members, key=lambda name: big_members[name]["lines"])
    survivors = [name for name in big_members if name != sacrificed]
    if len(survivors) != 1:
        raise RuntimeError(f"big_member.zip 夹具预期 1 个幸存成员，实际 {survivors}")
    survivor = survivors[0]

    mm_members = spec["per_file"]["many_members.zip"]["members"]
    cap_members = budget["archive_max_members"]
    if not 0 < cap_members < len(mm_members):
        raise RuntimeError(f"max_members={cap_members} 落在 {len(mm_members)} 个成员之外，夹具白做了")
    order = list(mm_members)
    kept, cut = order[:cap_members], order[cap_members]

    job = create_job(
        api,
        name=f"e2e-budget-{RUN_ID}",
        data_source_id=ctx["ds_id"],
        source_path=spec["source"],
        rule_set_id=ctx["rule_set_id"],
        idem=f"e2e-limits-budget-{RUN_ID}",
    )
    final = api.wait_job_terminal(job["id"], timeout=420.0)
    ledger.equal("预算越界作业整体判 partial", final["status"], "partial")
    ledger.equal("partial 作业不写作业级 error", final["error"], None)
    ledger.equal("partial 作业没有取消标记", final["cancel_requested"], False)

    tasks = tasks_of(api, final["id"])
    ledger.equal("两个归档 = 两条任务", len(tasks), 2)
    ledger.equal("任务响应字段集 = FileTaskRead", sorted(tasks[0]), sorted(TASK_FIELDS))
    ledger.equal("任务状态分布", counters(tasks), {"partial": 2})
    ledger.check(
        "两条任务都归紧预算 Worker",
        all(task["worker_id"] == worker_name for task in tasks),
        str([task["worker_id"] for task in tasks]),
    )
    ledger.check("两条任务都有真实心跳（不是 pending 空转）", all(task["heartbeat_at"] for task in tasks), "")
    ledger.check("两条任务 attempts 恰为 1（内容失败不重试）", all(task["attempts"] == 1 for task in tasks), str([t["attempts"] for t in tasks]))
    ledger.check(
        "partial 任务的 max_attempts 仍是队列默认 5（预算失败不改动配额）",
        all(task["max_attempts"] == 5 for task in tasks),
        str([t["max_attempts"] for t in tasks]),
    )

    big_task = task_by_name(tasks, "big_member.zip")
    ledger.equal(big_task["file_name"] + " 单成员超限判 partial", big_task["status"], "partial")
    ledger.equal(
        "单成员超限的错误文案逐字命中（含预算值与成员名）",
        big_task["error"],
        f"1 member(s) failed: {sacrificed}: member {sacrificed!r} exceeded max_member_bytes={cap_member_bytes}",
    )
    ledger.equal(
        "超预算成员被牺牲，但同包其余成员照常产出命中",
        big_task["matched_rows"],
        big_members[survivor]["matches"],
    )
    ledger.check(
        "扫描确实在超限前跑了一段（不是整包没打开就失败）",
        (big_task["processed_lines"] or 0) > big_members[survivor]["lines"],
        f"processed={big_task['processed_lines']} survivor_lines={big_members[survivor]['lines']}",
    )
    ledger.check(
        "超限后停止读该成员（行数远小于默认预算下的全量）",
        (big_task["processed_lines"] or 0) < spec["per_file"]["big_member.zip"]["lines"],
        f"processed={big_task['processed_lines']} full={spec['per_file']['big_member.zip']['lines']}",
    )

    mm_task = task_by_name(tasks, "many_members.zip")
    ledger.equal("成员数超限判 partial", mm_task["status"], "partial")
    ledger.equal(
        "成员数超限的错误文案逐字命中（在第 21 个成员处停止整包）",
        mm_task["error"],
        f"1 member(s) failed: {cut}: archive exceeded max_members={cap_members} at {cut!r}",
    )
    ledger.equal(
        "成员数超限前的成员行数全部计入",
        mm_task["processed_lines"],
        sum(mm_members[name]["lines"] for name in kept),
    )
    ledger.equal(
        "成员数超限前的成员命中全部落库",
        mm_task["matched_rows"],
        sum(mm_members[name]["matches"] for name in kept),
    )

    rows = list(iter_matches(api, page=500, job_id=final["id"]))
    published = (big_task["matched_rows"] or 0) + (mm_task["matched_rows"] or 0)
    ledger.equal("命中表行数 = 两条任务的计数之和", len(rows), published)
    by_rule: dict[str, int] = {}
    for row in rows:
        by_rule[row["rule_id"]] = by_rule.get(row["rule_id"], 0) + 1
    ledger.equal("越界作业只命中 error_kw", by_rule, {"error_kw": published})
    ledger.check("命中的规则 id 属于本轮规则集", set(by_rule) <= set(ctx["rule_ids"]), str(sorted(by_rule)))
    seen_members = {row["member_path"] for row in rows}
    ledger.check(
        "被牺牲/被裁掉的成员没有留下半截命中",
        sacrificed not in seen_members and cut not in seen_members,
        f"seen={sorted(seen_members)}",
    )
    ledger.equal(
        "幸存成员与命中成员集合一致",
        sorted(seen_members),
        sorted([survivor, *kept]),
    )

    # partial ≠ failed：预算牺牲的成员不进人工重试队列，这条边界只能在这里钉住。
    body = api.call("POST", f"/jobs/{final['id']}/files/retry-failed")
    ledger.equal("partial 任务不在重试队列里", (body["status"], body["body"]), (200, {"retried": 0, "remaining_failed": 0}))
    ledger.equal("空重试后作业仍是 partial", api.get(f"/jobs/{final['id']}")["status"], "partial")
    st["budget_job"] = {"job": final, "tasks": tasks, "rows": len(rows)}


# ============================================================================
# L3 命中上限：失败清场
# ============================================================================
ATTEMPT_FIELDS = [
    "attempt_no", "error", "file_task_id", "finished_at", "matched_rows",
    "processed_lines", "started_at", "status", "worker_id", "worker_version",
]


class MatchObserver(threading.Thread):
    """尽力捕捉"触顶前确实按批落库过"的现场：轮询该任务在 matches 表里的行数峰值。

    已提交的 90 行只在两次批量写入之间存活几十毫秒，抓到是加分证据、抓不到也不能算
    失败，所以判定交给序列号（插入必然推进 ``last_value``，删除不会回退），这里只记录。
    """

    def __init__(self, task_id: int) -> None:
        # 注意：绝不能用 self._stop —— threading.Thread 内部就有 _stop() 方法，
        # join() 会调用它，覆盖成 Event 后 join 直接 TypeError（第一次跑就撞上了）。
        super().__init__(daemon=True)
        self._task_id = task_id
        self._done = threading.Event()
        self.peak = 0
        self.polls = 0
        self.errors = 0
        self.last_error = ""

    def run(self) -> None:
        sql = f"select count(*) from matches where file_task_id = {self._task_id}"
        while not self._done.is_set():
            try:
                self.peak = max(self.peak, psql_int(sql))
            except Exception as exc:  # noqa: BLE001 - 取证线程不能把主流程带崩
                self.errors += 1
                self.last_error = str(exc)
            self.polls += 1

    def stop(self) -> None:
        self._done.set()


def phase_flood(ledger: Ledger, api: ApiClient, ctx: dict[str, Any], st: dict[str, Any]) -> None:
    """L3 命中数越界：第 cap+1 条命中抛 MatchLimitExceeded，任务 failed 且命中被删干净。

    规格 §7.4 的"失败清场"要在真实 PostgreSQL 上才能证明两件事：触顶前那些行**确实提交
    过**（序列号推进），以及终态写回前它们**确实被删了**（按任务计数归零）。API 只报
    matched_rows=0，分不清"写了又删"和"根本没写"。
    """
    ledger.phase("L3 命中上限越界（max_matches_per_task）与失败清场")
    env = st["env"]
    budget = env["budget"]
    cap = budget["max_matches_per_task"]
    batch = budget["matches_batch_size"]
    # 缓冲区每凑满一批立刻提交；触顶那一行抛异常时剩下的半批还在内存里，从未落库。
    committed = (cap // batch) * batch
    if committed == 0:
        raise RuntimeError(f"matches_batch_size={batch} 大于上限 {cap}：触顶前一批都提交不了，清场无从证明")

    spec = load_fixtures()["cases"]["flood"]
    total_lines = spec["expect_total_lines"]
    full_matches = spec["expect_total_matches"]
    leading_noise = total_lines - full_matches  # build_flood：前 500 行不含任何关键字，之后每行一条命中
    if not 0 < committed < full_matches or cap >= full_matches:
        raise RuntimeError(f"cap={cap} / committed={committed} 与 flood 夹具（{full_matches} 命中）不匹配")

    seq_before = psql_int("select last_value from matches_id_seq")
    job = create_job(
        api,
        name=f"e2e-flood-{RUN_ID}",
        data_source_id=ctx["ds_id"],
        source_path=spec["source"],
        rule_set_id=ctx["rule_set_id"],
        idem=f"e2e-limits-flood-{RUN_ID}",
    )
    planned = tasks_of(api, job["id"])
    ledger.equal("flood 目录 1 个归档 = 1 条任务", len(planned), 1)
    task_id = planned[0]["id"]

    observer = MatchObserver(task_id)
    observer.start()
    try:
        final = api.wait_job_terminal(job["id"], timeout=420.0)
    finally:
        observer.stop()
        observer.join(timeout=60.0)

    ledger.equal("命中上限作业整体判 failed（单任务失败没有成功任务可稀释）", final["status"], "failed")
    ledger.equal("作业级 error 恒为 NULL：失败原因只落在任务上（FINISH_JOB_SQL 不写 error）", final["error"], None)

    tasks = tasks_of(api, final["id"])
    ledger.equal("任务响应字段集 = FileTaskRead", sorted(tasks[0]), sorted(TASK_FIELDS))
    task = tasks[0]
    ledger.equal("命中上限判 failed（不是 partial：整包结果已不可信）", task["status"], "failed")
    ledger.equal("上限失败的错误文案逐字命中", task["error"], f"match limit exceeded: max_matches_per_task={cap}")
    ledger.equal("任务归紧预算 Worker", task["worker_id"], env["name"])
    ledger.equal(
        "触顶停在第 cap+1 条命中所在行 = 前导噪声 + 上限 + 1",
        task["processed_lines"],
        leading_noise + cap + 1,
    )
    ledger.equal("失败清场后任务不再报告命中数", task["matched_rows"], 0)
    ledger.equal("上限失败是永久失败：attempts 恰为 1", task["attempts"], 1)
    ledger.equal("max_attempts 未被改动（队列默认 5）", task["max_attempts"], 5)

    in_db = psql_int(f"select count(*) from matches where file_task_id = {task_id}")
    ledger.equal("matches 表里该任务一行不剩（删的是真实行，不是从没写过）", in_db, 0)
    delta = psql_int("select last_value from matches_id_seq") - seq_before
    ledger.check(
        f"序列号至少推进 {committed}（触顶前确实批量提交过命中）",
        delta >= committed,
        f"delta={delta} committed={committed} batch={batch} cap={cap}",
    )
    ledger.check(
        "序列号远没到全量命中数（扫描确实在触顶处停了，没跑完 7000 行）",
        delta < full_matches,
        f"delta={delta} full={full_matches}",
    )
    if observer.peak > 0:
        ledger.check(
            "实时抓到了清场前的落库峰值",
            0 < observer.peak <= committed,
            f"peak={observer.peak} polls={observer.polls} committed={committed}",
        )
    else:
        ledger.skip(
            "实时抓到了清场前的落库峰值",
            f"{observer.polls} 次轮询都没抓到（{committed} 行只存活几十毫秒）；序列号证据已足够",
        )

    ledger.equal("命中查询接口对该作业返回空", list(iter_matches(api, page=500, job_id=final["id"])), [])

    attempts = api.get(f"/jobs/{final['id']}/files/{task_id}/attempts", expect_status=200)
    ledger.equal("第 1 次尝试已归档", len(attempts), 1)
    first = attempts[0]
    ledger.equal("尝试记录字段集 = FileTaskAttemptRead", sorted(first), sorted(ATTEMPT_FIELDS))
    ledger.equal(
        "尝试记录与任务终态一致（含清场后的 0 命中）",
        (first["attempt_no"], first["status"], first["matched_rows"], first["error"]),
        (1, "failed", 0, task["error"]),
    )
    ledger.equal("尝试记录写的是紧预算 Worker 的身份", (first["worker_id"], first["worker_version"]), (env["name"], env["version"]))
    ledger.check("尝试记录有真实的起止时间", bool(first["started_at"]) and bool(first["finished_at"]), str([first["started_at"], first["finished_at"]]))

    # retryable=False 的关键语义：不自动重排。给足 6 个 1s 轮询周期再回读。
    time.sleep(6.0)
    after_job = api.get(f"/jobs/{final['id']}")
    after_task = tasks_of(api, final["id"])[0]
    ledger.equal("永久失败不会被队列自动重排（作业仍是 failed）", after_job["status"], "failed")
    ledger.equal("永久失败不会被队列自动重排（任务仍是 failed、attempts=1）", (after_task["status"], after_task["attempts"]), ("failed", 1))
    open_tasks = psql_int(
        f"select count(*) from file_tasks where job_id = {final['id']} and status in ('pending','running')"
    )
    ledger.equal("该作业没有滞留的开放任务", open_tasks, 0)

    st["flood"] = {
        "job": final,
        "task": task,
        "task_id": task_id,
        "committed": committed,
        "seq_delta": delta,
        "observer_polls": observer.polls,
    }


# ============================================================================
# L4 撤掉紧预算 Worker，用默认预算人工重试
# ============================================================================
def phase_recover(ledger: Ledger, api: ApiClient, ctx: dict[str, Any], st: dict[str, Any]) -> None:
    """L4 换回默认预算的 Worker 重试同一任务：6500 行全量入库，两次尝试都留档。

    这一步同时结三个契约：``retry-failed`` 只搬 failed 行并保留 attempts、第 2 次尝试
    开头的 ``delete_for_task`` 让重试幂等（不会把两次的命中叠起来），以及 SIGTERM 退出的
    Worker 会自报 offline、离线行被保留而不是删除（管理面要能回看历史）。
    """
    ledger.phase("L4 撤掉紧预算 Worker，用默认预算人工重试同一任务")
    env = st["env"]
    flood = st["flood"]
    job_id = flood["job"]["id"]
    task_id = flood["task_id"]
    spec = load_fixtures()["cases"]["flood"]

    out = worker3("stop")
    ledger.check("worker3.sh stop 执行成功", "worker rm" in out, out.strip()[:160])
    offline = wait_until(
        lambda: next(
            (w for w in workers_of(api) if w["id"] == env["name"] and w["status"] == "offline"), None
        ),
        timeout=60.0,
        name=f"{env['name']} 自报 offline",
        interval=1.0,
    )
    ledger.equal("离线 Worker 行仍被保留，且版本号不变", (offline["id"], offline["version"]), (env["name"], env["version"]))
    ledger.check("离线行的当前任务已清空", offline["current_task_id"] is None, str(offline["current_task_id"]))

    resume_ids(ledger, api, env["defaults"])
    pool = {w["id"]: w for w in workers_of(api)}
    online = sorted(w["id"] for w in pool.values() if w["status"] == "online")
    ledger.equal("恢复后池子里只有两只默认 Worker 可认领", online, sorted(env["defaults"]))
    default_versions = {w["id"]: w["version"] for w in pool.values() if w["id"] in env["defaults"]}
    ledger.check(
        "默认 Worker 的预算与紧预算 Worker 不同（否则 L3/L4 的对比没有意义）",
        default_versions[online[0]] != env["version"],
        str(default_versions),
    )

    body = api.post(f"/jobs/{job_id}/files/retry-failed", expect_status=200)
    ledger.equal("人工重试把 1 条 failed 放回队列", body, {"retried": 1, "remaining_failed": 0})
    ledger.equal("重开后作业回到 running（终态必须清掉，否则概览与取消都看不见它）", api.get(f"/jobs/{job_id}")["status"], "running")

    final = api.wait_job_terminal(job_id, timeout=420.0)
    ledger.equal("默认预算下同一份 NAS 全量跑通", final["status"], "succeeded")
    task = tasks_of(api, job_id)[0]
    ledger.equal("第 2 次尝试成功", (task["status"], task["attempts"]), ("succeeded", 2))
    ledger.equal("全量行数入库", task["processed_lines"], spec["expect_total_lines"])
    ledger.equal("全量命中数入库", task["matched_rows"], spec["expect_total_matches"])
    ledger.equal("失败原因已被成功终态覆盖", task["error"], None)
    ledger.check("任务改由默认 Worker 认领", task["worker_id"] in env["defaults"], str(task["worker_id"]))

    rows = list(iter_matches(api, page=500, job_id=job_id))
    ledger.equal("命中接口拿到全部行", len(rows), spec["expect_total_matches"])
    by_rule: dict[str, int] = {}
    for row in rows:
        by_rule[row["rule_id"]] = by_rule.get(row["rule_id"], 0) + 1
    ledger.equal("按规则分布 = 夹具 oracle", by_rule, spec["expect_matches_by_rule"])
    ledger.equal(
        "命中只来自那个成员",
        sorted({row["member_path"] for row in rows}),
        sorted(spec["per_file"]["flood.zip"]["members"]),
    )
    ledger.check("每行恰好一条命中（1:1 的噪声/命中结构）", {row["line_no"] for row in rows} == set(range(501, 501 + spec["expect_total_matches"])), f"min={min(r['line_no'] for r in rows)} max={max(r['line_no'] for r in rows)}")

    in_db = psql_int(f"select count(*) from matches where file_task_id = {task_id}")
    ledger.equal("重试幂等：第 2 次尝试开头删过旧行，两次结果没有叠加", in_db, spec["expect_total_matches"])
    distinct = psql_int(f"select count(distinct line_no) from matches where file_task_id = {task_id}")
    ledger.equal("每条命中各占一个行号（没有重复写入）", distinct, spec["expect_total_matches"])

    attempts = api.get(f"/jobs/{job_id}/files/{task_id}/attempts", expect_status=200)
    ledger.equal("两次尝试都留档且按 attempt_no 升序", [a["attempt_no"] for a in attempts], [1, 2])
    ledger.equal(
        "两次尝试的结局与命中数",
        [(a["status"], a["matched_rows"]) for a in attempts],
        [("failed", 0), ("succeeded", spec["expect_total_matches"])],
    )
    ledger.equal(
        "两次尝试落在不同版本（更精确的 Worker 身份）",
        sorted({a["worker_version"] for a in attempts}),
        sorted([env["version"], default_versions[attempts[1]["worker_id"]]]),
    )
    ledger.equal(
        "attempt 历史里的 worker 与任务终态一致",
        (attempts[0]["worker_id"], attempts[1]["worker_id"]),
        (env["name"], task["worker_id"]),
    )
    st["recovered"] = {"job": final, "task": task, "rows": len(rows)}


def teardown(ledger: Ledger, api: ApiClient, st: dict[str, Any]) -> None:
    """无论成败都把栈还原成"两只默认 Worker 在线、紧预算容器不存在"，给后续流程留干净池子。"""
    env = st.get("env") or {}
    try:
        worker3("stop")
    except Exception as exc:  # noqa: BLE001 - 清理失败要记账，但不能盖住用例本身的结果
        ledger.fail_phase("清理紧预算 Worker 失败", exc)
    if not env.get("defaults"):
        return
    ledger.phase("收尾：恢复默认 Worker 池")
    try:
        rows = workers_of(api)
        need_resume = [w["id"] for w in rows if w["id"] in env["defaults"] and (w["drain_requested"] or w["status"] != "online")]
        if need_resume:
            resume_ids(ledger, api, need_resume)
        rows = workers_of(api)
        ledger.equal(
            "收尾后在线的正好是两只默认 Worker",
            sorted(w["id"] for w in rows if w["status"] == "online"),
            sorted(env["defaults"]),
        )
        ledger.check(
            "没有残留的 drain 标记（否则后面的作业会永远 pending）",
            not any(w["drain_requested"] for w in rows if w["id"] in env["defaults"]),
            str([(w["id"], w["drain_requested"]) for w in rows]),
        )
        ledger.check(
            "紧预算 Worker 容器已删除（离线行仍保留在 /workers 里）",
            "absent" in worker3("status"),
            worker3("status").strip()[:160],
        )
    except Exception as exc:  # noqa: BLE001
        ledger.fail_phase("恢复默认 Worker 池失败", exc)


PHASES = ("workers", "budget", "flood", "recover")
RUNNERS: dict[str, Callable[[Ledger, ApiClient, dict[str, Any], dict[str, Any]], None]] = {
    "workers": phase_workers,
    "budget": phase_budget,
    "flood": phase_flood,
    "recover": phase_recover,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LogHarvest 紧预算流程：解压预算 / 命中上限 / 失败清场 / 人工重试")
    parser.add_argument("--phase", default="all", help=f"逗号分隔：{','.join(PHASES)} 或 all")
    args = parser.parse_args(argv)
    wanted = set(PHASES) if args.phase == "all" else {p.strip() for p in args.phase.split(",")}
    unknown = wanted - set(PHASES)
    if unknown:
        print(f"!! 未知阶段：{sorted(unknown)}，可选 {list(PHASES)}", flush=True)
        return 2
    # 所有阶段都依赖"只有紧预算 Worker 能认领"，单独跑某一阶段时自动补上 L0；
    # L4 必须先有 L3 造出的触顶失败任务，否则重试对象不存在。
    if wanted - {"workers"}:
        wanted.add("workers")
    if "recover" in wanted and "flood" not in wanted:
        print("!! --phase recover 依赖 flood（要先造出触顶的失败任务），已自动补上", flush=True)
        wanted.add("flood")
    pipeline = [name for name in PHASES if name in wanted]

    ledger = Ledger("Limits Flow")
    api = ApiClient(base=API_BASE)
    st: dict[str, Any] = {}
    try:
        ctx = bootstrap(ledger, api)
    except (ApiError, TimeoutError, KeyError, OSError, RuntimeError) as exc:
        ledger.fail_phase("R0 前置阶段异常", exc)
        print(f"!! 前置中断：{type(exc).__name__}: {exc}", flush=True)
        return 1
    try:
        for name in pipeline:
            try:
                RUNNERS[name](ledger, api, ctx, st)
            except Exception as exc:  # noqa: BLE001 - 用例崩在这里也要把台账落盘，别丢掉已跑出的证据
                ledger.fail_phase(f"{name} 阶段异常", exc)
                print(f"!! 阶段 {name} 中断：{type(exc).__name__}: {exc}", flush=True)
    finally:
        teardown(ledger, api, st)
    summary = ledger.summary()
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())



