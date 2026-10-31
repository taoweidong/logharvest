from src.infrastructure import database
from src.infrastructure.database import UTC_SESSION_OPTIONS, connect_args_for, engine_kwargs_for


def test_postgres_sessions_are_pinned_to_utc() -> None:
    for url in ("postgresql://u@h/db", "postgresql+psycopg://u@h/db"):
        options = connect_args_for(url)["options"]
        # 时区钉扎在前，事务泄漏防护跟在同一个 options 里
        assert options.startswith(UTC_SESSION_OPTIONS)
        assert "idle_in_transaction_session_timeout=" in options


def test_other_databases_receive_no_session_arguments() -> None:
    assert connect_args_for("sqlite:///./logharvest.db") == {}


def test_postgres_engine_gets_pool_hardening_and_sqlite_keeps_defaults(monkeypatch) -> None:
    captured = {}

    def fake_create_engine(url, **kwargs):
        captured[url] = dict(kwargs)
        return object()

    monkeypatch.setattr(database, "create_engine", fake_create_engine)

    database.create_engine_from_settings("postgresql+psycopg://u@h/db")

    pg_kwargs = captured["postgresql+psycopg://u@h/db"]
    assert pg_kwargs["connect_args"] == {
        "options": f"{UTC_SESSION_OPTIONS} "
        "-c idle_in_transaction_session_timeout=300000"
    }
    # 断线自愈 + 池容量 + 长期空闲轮换，只作用于 PostgreSQL
    assert pg_kwargs["pool_pre_ping"] is True
    assert pg_kwargs["pool_size"] == 10
    assert pg_kwargs["max_overflow"] == 20
    assert pg_kwargs["pool_recycle"] == 1800

    database.create_engine_from_settings("sqlite://")

    # SQLite（本地开发/单测默认库）不附加任何池参数，保持 SQLAlchemy 默认行为
    assert captured["sqlite://"] == {"echo": False, "connect_args": {}}
