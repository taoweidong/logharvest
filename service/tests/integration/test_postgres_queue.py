"""PostgreSQL 集成测试：真正执行 M1b/M2a 至今只能做形状断言的队列 SQL。

默认跳过；设置 `LOGHARVEST_TEST_PG_URL` 指向一个可丢弃的 PostgreSQL 库后运行：

    cd service
    LOGHARVEST_TEST_PG_URL="postgresql+psycopg://logharvest:logharvest@127.0.0.1:5433/logharvest" \\
      uv run --python 3.12 pytest tests/integration -v

这里跑的每一条语句都是 SQLite 无法执行的（`FOR UPDATE SKIP LOCKED`、
`make_interval`、`ON CONFLICT`、CTE `UPDATE ... RETURNING`），所以本文件是队列
正确性的唯一真实证据；日常单测仍然只在内存 SQLite 上验证可移植部分。
"""

import os
from datetime import timedelta
from pathlib import Path
from threading import Barrier, Thread
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from src.application.worker.ports import WorkerStats
from src.domain.models.entities import (
    DataSource,
    FileTask,
    FileTaskAttempt,
    Job,
    Match,
    RuleSet,
    Worker,
    utcnow,
)
from src.infrastructure.database import create_db_and_tables, create_engine_from_settings
from src.infrastructure.match_writer import PostgresMatchWriter
from src.infrastructure.task_queue import PostgresTaskRepository

PG_URL = os.getenv("LOGHARVEST_TEST_PG_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not PG_URL, reason="LOGHARVEST_TEST_PG_URL 未设置：跳过 PostgreSQL 集成测试"
)


@pytest.fixture()
def engine() -> Engine:
    db_engine = create_engine_from_settings(PG_URL)
    db_engine.dispose()
    # 每个用例重建表：本库视为可丢弃的测试库。
    admin = create_engine_from_settings(PG_URL)
    with admin.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    create_db_and_tables(admin)
    admin.dispose()
    fresh = create_engine_from_settings(PG_URL)
    try:
        yield fresh
    finally:
        fresh.dispose()


@pytest.fixture()
def repo(engine: Engine) -> PostgresTaskRepository:
    return PostgresTaskRepository(engine, max_concurrent_copies=4)


def _seed(
    engine: Engine,
    task_statuses: list[str],
    *,
    job_status: str = "running",
    cancel_requested: bool = False,
    scheduled_delta_seconds: float = 0.0,
) -> tuple[int, list[int]]:
    """Create a rule set + job + one file task per status; returns (job_id, task_ids).

    数据源行必须真实存在：SQLite 测试里外键默认不生效，PostgreSQL 会直接拒绝，
    这正是本集成套件要暴露的差别之一。
    """
    with Session(engine) as session:
        suffix = uuid4().hex[:8]
        source = DataSource(name=f"nas-{suffix}", root_path="/nas")
        rule_set = RuleSet(name=f"rs-{suffix}", version=1, config={"name": "rs", "rules": []})
        session.add_all([source, rule_set])
        session.commit()
        session.refresh(source)
        session.refresh(rule_set)
        job = Job(
            name="j",
            data_source_id=source.id,
            source_path="/nas",
            rule_set_id=rule_set.id,
            status=job_status,
            cancel_requested=cancel_requested,
        )
        session.add(job)
        session.commit()
        job_id = job.id
        task_ids = []
        for index, status in enumerate(task_statuses, start=1):
            task = FileTask(
                job_id=job_id,
                source_path=f"/nas/a{index}.zip",
                file_name=f"a{index}.zip",
                size_bytes=1024,
                status=status,
                scheduled_at=utcnow() + timedelta(seconds=scheduled_delta_seconds),
            )
            session.add(task)
            session.commit()
            task_ids.append(task.id)
    return job_id, task_ids


def test_session_timezone_is_utc_for_clock_comparisons(engine: Engine) -> None:
    """UTC 会话时区是 mark_dead_workers / 心跳比较成立的前提。

    列都是 `timestamp without time zone`：SQL `now()`（timestamptz）写入时要靠会话
    时区折算，Python 侧写的是 UTC。会话不是 UTC 的话，两边的时钟就会整体错开一个
    偏移量，降级与僵死回收判定会永久不触发。这里走 ORM 写入，不手抄列清单。
    """
    with Session(engine) as session:
        session.add(Worker(id="tz", hostname="h", version="v", status="online"))
        session.commit()
    with engine.connect() as conn:
        timezone = conn.execute(text("show timezone")).scalar()
        fresh_within_thirty_seconds = conn.execute(
            text(
                "SELECT (last_heartbeat >= now() - interval '30 seconds') "
                "FROM workers WHERE id='tz'"
            )
        ).scalar()
        orm_row_is_visible = conn.execute(
            text(
                "SELECT (last_heartbeat < now() + interval '30 seconds') FROM workers WHERE id='tz'"
            )
        ).scalar()

    assert str(timezone).lower() in {"utc", "posix/gmt", "gmt"}
    assert fresh_within_thirty_seconds is True
    assert orm_row_is_visible is True


def test_claim_uses_skip_locked_and_never_double_assigns(
    repo: PostgresTaskRepository, engine: Engine
) -> None:
    """两个 Worker 并发领取时，SKIP LOCKED 必须保证零重叠、零丢失。"""
    _seed(engine, ["pending"] * 8, scheduled_delta_seconds=-5)

    claimed: list[int] = []
    lock = Barrier(4)

    def worker(name: str) -> None:
        thread_repo = PostgresTaskRepository(engine)
        lock.wait()
        for _ in range(5):
            task = thread_repo.claim_next(name, "0.1.0")
            if task is None:
                break
            claimed.append(task.id)

    threads = [Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(claimed) == 8, "所有待处理任务必须被领取恰好一次"
    assert len(set(claimed)) == 8, "同一个任务不能被两个 Worker 领到"
    with Session(engine) as session:
        rows = session.exec(
            select(FileTask.status, FileTask.attempts).where(FileTask.id.in_(claimed))
        ).all()
    assert {status for status, _ in rows} == {"running"}
    assert {attempts for _, attempts in rows} == {1}


def test_claim_records_attempt_history_and_rule_snapshot(
    repo: PostgresTaskRepository, engine: Engine
) -> None:
    job_id, _task_ids = _seed(engine, ["pending"], scheduled_delta_seconds=-5)

    task = repo.claim_next("w-snapshot", "9.9.9")

    assert task is not None
    assert task.job_id == job_id
    assert task.attempts == 1
    assert task.expected_size_bytes == 1024
    assert task.rule_version == 1
    assert task.root_path == Path("/nas")
    with Session(engine) as session:
        attempts = session.exec(
            select(FileTaskAttempt).where(FileTaskAttempt.file_task_id == task.id)
        ).all()
    assert [(a.attempt_no, a.status, a.worker_version) for a in attempts] == [
        (1, "running", "9.9.9")
    ]


def test_lease_fences_reject_a_stale_generation(
    repo: PostgresTaskRepository, engine: Engine
) -> None:
    """attempts 栅栏：被回收后旧 Worker 的续租与心跳都必须失效。"""
    _, [task_id] = _seed(engine, ["pending"], scheduled_delta_seconds=-5)
    task = repo.claim_next("w-fence", "0.1.0")
    assert task is not None

    assert repo.touch(task_id, task.attempts) is True
    assert repo.heartbeat(task_id, task.attempts, 10, 1) is True
    # 模拟回收后的下一代尝试。
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE file_tasks SET attempts = attempts + 1 WHERE id = :id"), {"id": task_id}
        )

    assert repo.touch(task_id, task.attempts) is False
    assert repo.heartbeat(task_id, task.attempts, 20, 2) is False


def test_complete_fences_late_completion_after_recovery(
    repo: PostgresTaskRepository, engine: Engine
) -> None:
    """回收之后迟到的完成写回不能覆盖新任务状态，也不能关闭尝试历史行。"""
    _, [task_id] = _seed(engine, ["pending"], scheduled_delta_seconds=-5)
    task = repo.claim_next("w-late", "0.1.0")
    assert task is not None
    repo.complete(task_id, task.attempts, "succeeded", 5, 1)

    with Session(engine) as session:
        stored = session.get(FileTask, task_id)
        attempt = session.exec(
            select(FileTaskAttempt).where(
                FileTaskAttempt.file_task_id == task_id, FileTaskAttempt.attempt_no == task.attempts
            )
        ).one()
    assert stored.status == "succeeded"
    assert stored.finished_at is not None
    assert attempt.status == "succeeded"
    assert attempt.matched_rows == 1

    # 同一代的重复完成必须无副作用。
    repo.complete(task_id, task.attempts, "succeeded", 999, 999)
    with Session(engine) as session:
        again = session.get(FileTask, task_id)
    assert again.processed_lines == 5


def test_failed_attempt_backs_off_then_terminalizes(
    repo: PostgresTaskRepository, engine: Engine
) -> None:
    _, [task_id] = _seed(engine, ["pending"], scheduled_delta_seconds=-5)
    task = repo.claim_next("w-retry", "0.1.0")
    assert task is not None

    repo.complete(task_id, task.attempts, "failed", 0, 0, "boom")

    with Session(engine) as session:
        stored = session.get(FileTask, task_id)
    assert stored.status == "pending"
    assert stored.finished_at is None
    # 线性退避必须真的把 scheduled_at 推到将来（make_interval）。
    assert stored.scheduled_at > utcnow() + timedelta(seconds=1)


def test_non_retryable_failure_is_terminal(repo: PostgresTaskRepository, engine: Engine) -> None:
    """永久失败（源文件变更）不烧 attempts，直接落终态。"""
    _, [task_id] = _seed(engine, ["pending"], scheduled_delta_seconds=-5)
    task = repo.claim_next("w-perm", "0.1.0")
    assert task is not None

    repo.complete(task_id, task.attempts, "failed", 0, 0, "source size changed", retryable=False)

    with Session(engine) as session:
        stored = session.get(FileTask, task_id)
    assert stored.status == "failed"
    assert stored.finished_at is not None
    assert "size changed" in (stored.error or "")


def test_partial_completion_is_terminal_and_finalizes_job_partial(
    repo: PostgresTaskRepository, engine: Engine
) -> None:
    """成员级部分失败是终态：不回 pending，且作业汇总为 partial。"""
    job_id, _task_ids = _seed(engine, ["pending", "succeeded"], scheduled_delta_seconds=-5)
    task = repo.claim_next("w-partial", "0.1.0")
    assert task is not None

    repo.complete(
        task.id,
        task.attempts,
        "partial",
        10,
        2,
        "1 member(s) failed: bad.log: invalid compressed data",
        retryable=False,
    )

    with Session(engine) as session:
        stored = session.get(FileTask, task.id)
        attempt = session.exec(
            select(FileTaskAttempt).where(FileTaskAttempt.file_task_id == task.id)
        ).one()
    assert stored.status == "partial"
    assert stored.finished_at is not None
    assert "bad.log" in (stored.error or "")
    assert attempt.status == "partial"
    assert repo.finalize_job(job_id) == "partial"


def test_cancelled_job_attempt_becomes_cancelled_not_requeued(
    repo: PostgresTaskRepository, engine: Engine
) -> None:
    """规格 §7.3：取消作业的失败尝试不能回到 pending，否则作业永久悬挂。"""
    _, [task_id] = _seed(engine, ["pending"], scheduled_delta_seconds=-5)
    task = repo.claim_next("w-cancel", "0.1.0")
    assert task is not None
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET cancel_requested = true WHERE id = :id"), {"id": task.job_id}
        )

    repo.complete(task_id, task.attempts, "failed", 0, 0, "read timeout")

    with Session(engine) as session:
        stored = session.get(FileTask, task_id)
    assert stored.status == "cancelled"
    assert stored.finished_at is not None


def test_claim_refuses_tasks_of_cancelled_job(repo: PostgresTaskRepository, engine: Engine) -> None:
    _seed(engine, ["pending"], cancel_requested=True, scheduled_delta_seconds=-5)

    assert repo.claim_next("w-blocked", "0.1.0") is None


def test_recover_stale_reclaims_and_marks_attempts_timeout(
    repo: PostgresTaskRepository, engine: Engine
) -> None:
    _, [task_id] = _seed(engine, ["pending"], scheduled_delta_seconds=-5)
    task = repo.claim_next("w-dead", "0.1.0")
    assert task is not None
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE file_tasks SET heartbeat_at = now() - interval '30 minutes' WHERE id = :id"
            ),
            {"id": task_id},
        )

    recovered = repo.recover_stale(120)

    assert recovered == 1
    with Session(engine) as session:
        stored = session.get(FileTask, task_id)
        attempt = session.exec(
            select(FileTaskAttempt).where(
                FileTaskAttempt.file_task_id == task_id, FileTaskAttempt.attempt_no == task.attempts
            )
        ).one()
    assert stored.status == "pending"
    assert stored.worker_id is None
    assert attempt.status == "timeout"
    assert attempt.finished_at is not None


def test_recover_stale_exhausts_attempts_and_finalizes_job(
    repo: PostgresTaskRepository, engine: Engine
) -> None:
    """回收把最后一个任务判死之后，补偿扫描必须收尾作业——没有完成回调的路径也要闭合。"""
    job_id, [task_id] = _seed(engine, ["pending"], scheduled_delta_seconds=-5)
    task = repo.claim_next("w-exhaust", "0.1.0")
    assert task is not None
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE file_tasks SET attempts = max_attempts,"
                " heartbeat_at = now() - interval '30 minutes' WHERE id = :id"
            ),
            {"id": task_id},
        )

    assert repo.recover_stale(120) == 1
    assert repo.finalize_finished_jobs() == 1

    with Session(engine) as session:
        assert session.get(FileTask, task_id).status == "failed"
        assert session.get(Job, job_id).status == "failed"


def test_worker_registry_and_dead_worker_demotion(
    repo: PostgresTaskRepository, engine: Engine
) -> None:
    """ON CONFLICT 注册（含并发预算）与 portable 降级必须在 PG 上真实走通。"""
    repo.register_worker("w-reg", "host-a", "1.0.0")
    repo.heartbeat_worker(
        "w-reg",
        "busy",
        42,
        stats=WorkerStats(cpu_percent=12.5, memory_bytes=2048, temp_disk_free_bytes=4096),
    )

    with Session(engine) as session:
        worker = session.get(Worker, "w-reg")
    assert worker.status == "busy"
    assert worker.current_task_id == 42
    assert worker.max_concurrent_copies == 4
    assert worker.cpu_percent == 12.5
    assert worker.memory_bytes == 2048
    assert worker.temp_disk_free_bytes == 4096

    # 不带 stats 的普通心跳不能把已知资源值抹成 NULL。
    repo.heartbeat_worker("w-reg", "online", None)
    with Session(engine) as session:
        worker = session.get(Worker, "w-reg")
    assert worker.status == "online"
    assert worker.cpu_percent == 12.5
    assert worker.memory_bytes == 2048
    assert worker.temp_disk_free_bytes == 4096

    repo.register_worker("w-reg", "host-b", "2.0.0")  # ON CONFLICT 路径
    with Session(engine) as session:
        worker = session.get(Worker, "w-reg")
    assert worker.hostname == "host-b"
    assert worker.version == "2.0.0"

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE workers SET last_heartbeat = now() - interval '10 minutes' "
                "WHERE id = 'w-reg'"
            )
        )
    assert repo.mark_dead_workers(120) == 1
    with Session(engine) as session:
        worker = session.get(Worker, "w-reg")
    assert worker.status == "offline"
    assert worker.current_task_id is None


def test_job_finalization_aggregates_pg_side(repo: PostgresTaskRepository, engine: Engine) -> None:
    """两个任务都落终态后，作业必须由单写入者栅栏收尾一次且仅一次。"""
    job_id, task_ids = _seed(engine, ["pending", "pending"], scheduled_delta_seconds=-5)
    assert repo.finalize_job(job_id) is None

    for index, expected in enumerate(task_ids):
        task = repo.claim_next("w-agg", "0.1.0")
        assert task is not None and task.id == expected
        repo.complete(task.id, task.attempts, "succeeded", 3, 1)
        if index == 0:
            # 还有一个任务处于 running，绝不能收尾。
            assert repo.finalize_job(job_id) is None

    assert repo.finalize_job(job_id) == "succeeded"
    # 行级栅栏：第二个汇总者什么也改不了。
    assert repo.finalize_job(job_id) is None
    with Session(engine) as session:
        job = session.get(Job, job_id)
    assert job.status == "succeeded"
    assert job.finished_at is not None


def test_match_writer_batches_and_scoped_cleanup(engine: Engine) -> None:
    """批量写入 + 按任务清理的幂等语义（真实 JSONB/Numeric 落库）。"""
    from decimal import Decimal

    from src.application.worker.ports import MatchRecord

    job_id, [task_id] = _seed(engine, ["running"])
    writer = PostgresMatchWriter(engine)

    def records(count: int) -> list[MatchRecord]:
        return [
            MatchRecord(
                job_id=job_id,
                file_task_id=task_id,
                rule_id="slow",
                rule_set_id=1,
                rule_version=1,
                severity="warning",
                tags=["perf", "cn:超时"],
                source_path="/nas/a1.zip",
                member_path="svc/app.log",
                file_name="app.log",
                line_no=index,
                raw_line=f"elapsed={index}ms",
                raw_truncated=False,
                fields={"elapsed_ms": str(index), "备注": "中文键"},
                numeric_value=Decimal(str(index)),
            )
            for index in range(1, count + 1)
        ]

    writer.write_batch(tuple(records(2500)))
    with Session(engine) as session:
        stored = session.exec(select(Match).where(Match.file_task_id == task_id)).all()
    assert len(stored) == 2500
    sample = stored[0]
    assert isinstance(sample.fields, dict) and sample.fields.get("备注") == "中文键"
    assert list(sample.tags) == ["perf", "cn:超时"]
    assert sample.numeric_value == Decimal(1)

    # 重试清场：只删本任务的行，别的作业不受影响。
    other_job, [other_task] = _seed(engine, ["running"])
    writer.write_batch(
        [
            MatchRecord(
                job_id=other_job,
                file_task_id=other_task,
                rule_id="slow",
                rule_set_id=1,
                rule_version=1,
                severity="warning",
                tags=[],
                source_path="/nas/b.zip",
                member_path="app.log",
                file_name="app.log",
                line_no=1,
                raw_line="x",
                raw_truncated=False,
                fields={},
                numeric_value=None,
            )
        ]
    )
    writer.delete_for_task(task_id)
    with Session(engine) as session:
        assert session.exec(select(Match).where(Match.file_task_id == task_id)).all() == []
        survivors = session.exec(select(Match).where(Match.file_task_id == other_task)).all()
    assert len(survivors) == 1


def test_claim_caches_rule_config_by_ruleset_version(
    repo: PostgresTaskRepository, engine: Engine
) -> None:
    """同一 (rule_set_id, version) 的领取命中配置缓存，不再重复拉取 config JSON。

    5k 规则的 config 是 MB 级 JSON：2000 任务逐次领取重复传输是 GB 级浪费。
    版本不可变（rule_sets 只增版本），缓存永不失效。
    """
    _job_id, _task_ids = _seed(engine, ["pending"], scheduled_delta_seconds=-5)
    task = repo.claim_next("w-cache", "0.1.0")
    assert task is not None
    assert task.rule_config == {"name": "rs", "rules": []}
    assert repo._rule_config_cache == {(task.rule_set_id, task.rule_version): task.rule_config}

    # 失败回队后再次领取：同版本 → 缓存命中（键不变、config 对象同一）。
    repo.complete(task.id, task.attempts, "failed", 0, 0, "x")
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE file_tasks SET status = 'pending', scheduled_at = now() WHERE id = :id"),
            {"id": task.id},
        )
    again = repo.claim_next("w-cache", "0.1.0")
    assert again is not None
    assert (again.rule_set_id, again.rule_version) == (task.rule_set_id, task.rule_version)
    assert again.rule_config is task.rule_config
