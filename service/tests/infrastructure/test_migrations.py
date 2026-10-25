from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine

from src.infrastructure.migrations import BUSINESS_TABLES, run_migrations


def sqlite_url(tmp_path) -> str:
    return f"sqlite:///{(tmp_path / 'lh.db').as_posix()}"


def test_upgrade_creates_every_table_once(tmp_path) -> None:
    url = sqlite_url(tmp_path)

    run_migrations(url)
    first = set(inspect(create_engine(url)).get_table_names())
    run_migrations(url)  # 幂等：第二次不应报错或重复建表
    second = set(inspect(create_engine(url)).get_table_names())

    assert set(BUSINESS_TABLES) <= first
    assert {"users", "roles", "user_roles"} <= first
    assert "alembic_version" in first
    assert first == second


def test_migrations_match_model_metadata(tmp_path) -> None:
    url = sqlite_url(tmp_path)
    run_migrations(url)
    engine: Engine = create_engine(url)
    migrated = inspect(engine)

    from sqlmodel import SQLModel

    import src.domain.models  # noqa: F401

    for table_name in SQLModel.metadata.tables:
        assert migrated.has_table(table_name), table_name
        declared = {c.name for c in SQLModel.metadata.tables[table_name].columns}
        actual = {c["name"] for c in migrated.get_columns(table_name)}
        assert declared == actual, (table_name, declared ^ actual)
        # 索引同样要对齐：千万级命中的表上，少一条索引就是一次全表扫描。
        declared_indexes = {i.name for i in SQLModel.metadata.tables[table_name].indexes}
        actual_indexes = {
            i["name"] for i in migrated.get_indexes(table_name) if i["name"] is not None
        }
        assert declared_indexes == actual_indexes, (
            table_name,
            declared_indexes ^ actual_indexes,
        )


def test_only_postgres_takes_the_startup_lock() -> None:
    from src.infrastructure.migrations import needs_advisory_lock

    assert needs_advisory_lock("postgresql://u@h/db") is True
    assert needs_advisory_lock("postgresql+psycopg://u@h/db") is True
    assert needs_advisory_lock("sqlite:///./x.db") is False


def test_sqlite_migration_never_builds_a_lock_engine(monkeypatch, tmp_path) -> None:
    """锁引擎只在 PostgreSQL 路径出现；SQLite 分支若误建引擎会在这里暴露。"""
    from src.infrastructure import migrations

    def boom(url):
        raise AssertionError(f"lock engine must not be created for {url}")

    monkeypatch.setattr(migrations, "create_engine_from_settings", boom)
    migrations.run_migrations(f"sqlite:///{(tmp_path / 'nolock.db').as_posix()}")


def test_postgres_migration_takes_and_releases_the_lock(monkeypatch) -> None:
    from sqlalchemy import create_engine

    from src.infrastructure import migrations

    statements: list[str] = []
    engine = create_engine("sqlite://")

    monkeypatch.setattr(migrations, "create_engine_from_settings", lambda url: engine)
    monkeypatch.setattr(migrations, "text", lambda sql: statements.append(sql) or object())
    monkeypatch.setattr(
        migrations.command,
        "upgrade",
        lambda cfg, rev: statements.append("upgrade"),
    )

    # 用 SQLite 引擎替身跑 PostgreSQL 分支：只需看清语句顺序。
    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *_a, **_kw):
            return None

        def commit(self) -> None:
            statements.append("commit")

    monkeypatch.setattr(engine, "connect", lambda: _Conn())

    migrations.run_migrations("postgresql+psycopg://u@h/db")

    assert statements[0] == "SELECT pg_advisory_lock(:key)"
    assert statements[1] == "upgrade"
    assert statements[2] == "SELECT pg_advisory_unlock(:key)"
