from sqlalchemy.engine import Engine
from sqlmodel import SQLModel, create_engine

import src.domain.models  # noqa: F401  (register model tables on SQLModel.metadata)
from src.config.settings import settings

UTC_SESSION_OPTIONS = "-c timezone=UTC"
# 事务泄漏防护：任何连接不允许停留在"空闲但开着事务"的状态超过 5 分钟。
# 队列语句与 API 查询都在毫秒~秒级完成；一旦代码路径忘记提交/回滚（连接归还前
# 挂在事务里），持久的 idle-in-transaction 会长期持有行锁、阻塞 SKIP LOCKED
# 之外的 VACUUM 推进。5 分钟对合法长事务（迁移 DDL、手工 psql 会话不经此参数）
# 足够宽裕，又能让泄漏在下一个周期被服务端自动终止。
IDLE_IN_TRANSACTION_TIMEOUT_MS = "300000"


def connect_args_for(database_url: str) -> dict[str, str | bool]:
    """Force a UTC session timezone on PostgreSQL connections.

    The timestamp columns are ``without time zone``: SQL ``now()`` stores
    wall-clock time in the session timezone, while Python writes UTC-aware
    values. Without pinning the session to UTC the two clocks disagree, and a
    non-UTC server would make every heartbeat comparison
    (``file_tasks.heartbeat_at``, stale recovery, dead-worker demotion,
    ``jobs.finished_at``) shift by the offset.
    """
    if database_url.startswith("postgresql"):
        return {
            "options": f"{UTC_SESSION_OPTIONS} "
            f"-c idle_in_transaction_session_timeout={IDLE_IN_TRANSACTION_TIMEOUT_MS}"
        }
    return {}


def engine_kwargs_for(database_url: str) -> dict:
    """Dialect-specific engine tuning.

    PostgreSQL（生产与 e2e 的固定数据库）：
    - ``pool_pre_ping``：连接归还池后可能被服务端/网络切断（PG 重启、备份窗口、
      空闲超时），借出前 ping 一次，坏连接当场重建，Worker 领取循环不会因一次
      ``OperationalError`` 整轮失败。
    - ``pool_size``/``max_overflow``：server（FastAPI 线程池 + SSE）与多 Worker
      共用一套连接画像；10+20 覆盖「API 峰值 + 单 Worker 并发领取/心跳/写入」，
      超出的请求在池上短暂等待而不是无限建连压垮 PG。
    - ``pool_recycle``：低于任何常见 NAT/防火墙空闲回收阈值，长期空闲的池连接
      主动轮换，避免拿到已被静默丢弃的半开连接。

    SQLite（本地开发与单测的默认库）保持 SQLAlchemy 默认池行为，不加任何参数：
    单文件库没有服务端断线问题，pre_ping 反而给每条语句多一次往返。
    """
    if database_url.startswith("postgresql"):
        return {
            "pool_pre_ping": True,
            "pool_size": 10,
            "max_overflow": 20,
            "pool_recycle": 1800,
        }
    return {}


def create_engine_from_settings(database_url: str | None = None) -> Engine:
    """Build the application database engine from settings."""
    url = database_url or settings.database_url
    return create_engine(
        url,
        echo=False,
        connect_args=connect_args_for(url),
        **engine_kwargs_for(url),
    )


engine = create_engine_from_settings()


def create_db_and_tables(db_engine: Engine | None = None) -> None:
    """Create all core tables, using the public module-level engine by default.

    The module-level ``engine`` is looked up at call time so reassigning it
    (for example in tests) is honoured.
    """
    SQLModel.metadata.create_all(db_engine if db_engine is not None else engine)
