from sqlalchemy.engine import Engine

from src.infrastructure.task_queue import PostgresTaskRepository

DEFAULT_STALE_SECONDS = 120


class MaintenanceRequiresPostgres(RuntimeError):
    """The reclaim step is PostgreSQL-only SQL.

    在非 PostgreSQL 上做"半套清扫"会返回一个看起来成功的计数，却悄悄漏掉了
    僵死任务回收这一主要职责，所以这里直接拒绝，让调用方拿到可解释的失败。
    """


def sweep_workers_and_jobs(
    engine: Engine, stale_seconds: int = DEFAULT_STALE_SECONDS
) -> dict[str, int]:
    """Run the two portable sweeps: silent workers to offline, finished jobs closed.

    ``mark_dead_workers`` 与 ``finalize_finished_jobs`` 都是可移植写法，能在 SQLite 上
    真实执行；重复调用只会返回 0。
    """
    repo = PostgresTaskRepository(engine)
    return {
        "workers_demoted": repo.mark_dead_workers(stale_seconds),
        "jobs_finalized": repo.finalize_finished_jobs(),
    }


def run_maintenance(engine: Engine, stale_seconds: int = DEFAULT_STALE_SECONDS) -> dict[str, int]:
    """Server-side compensation the worker loop cannot cover alone.

    没有存活 Worker 时（全部退出、还在升级），僵死任务、离线状态与作业收尾都
    只能由服务端触发：Worker 的启动补偿与每 300 秒周期扫描都发生在进程内，进程
    不在了就没有人执行它们。调度交给外部（cron / compose job），这里只保证
    可重复执行——每个步骤都以幂等 SQL 收敛，多跑一次只会返回 0。

    ``recover_stale`` 是 PostgreSQL 专有语句（``SKIP LOCKED``、``make_interval``），
    因此整个入口要求 PostgreSQL；另外两步见 :func:`sweep_workers_and_jobs`。
    """
    if engine.dialect.name != "postgresql":
        raise MaintenanceRequiresPostgres(
            f"maintenance requires a PostgreSQL queue database, got {engine.dialect.name}"
        )
    repo = PostgresTaskRepository(engine)
    recovered = repo.recover_stale(stale_seconds)
    return {"recovered": recovered, **sweep_workers_and_jobs(engine, stale_seconds)}
