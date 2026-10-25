from sqlalchemy.engine import Engine

from src.infrastructure.task_queue import PostgresTaskRepository

DEFAULT_STALE_SECONDS = 120


def run_maintenance(engine: Engine, stale_seconds: int = DEFAULT_STALE_SECONDS) -> dict[str, int]:
    """Server-side compensation the worker loop cannot cover alone.

    没有存活 Worker 时（全部退出、还在升级），僵死任务、离线状态与作业收尾都
    只能由服务端触发：Worker 的启动补偿与每 300 秒周期扫描都发生在进程内，进程
    不在了就没有人执行它们。调度交给外部（cron / compose job），这里只保证
    可重复执行——每个步骤都以幂等 SQL 收敛，多跑一次只会返回 0。

    ``recover_stale`` 是 PostgreSQL 专有语句（``SKIP LOCKED``、``make_interval``）；
    另外两个清理是可移植写法。
    """
    repo = PostgresTaskRepository(engine)
    return {
        "recovered": repo.recover_stale(stale_seconds),
        "workers_demoted": repo.mark_dead_workers(stale_seconds),
        "jobs_finalized": repo.finalize_finished_jobs(),
    }
