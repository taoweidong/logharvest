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
