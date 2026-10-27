"""应用 lifespan 与系统维护端点的接线测试。

lifespan 分支（run_migrations + ensure_rbac_seed）此前覆盖为 0：autouse 的
"跳过启动迁移" fixture 保护了开发库，但也让接线代码从未在测试里真实跑过。
这里在独立临时 SQLite 上打开一次 TestClient，验证启动迁移 + 种子补齐真的发生。
"""

import pytest
from sqlmodel import Session, create_engine, select

from src.config.settings import settings
from src.domain.models.rbac import Menu
from src.infrastructure import database


@pytest.fixture()
def migrated_app_environment(tmp_path, monkeypatch):
    """临时 SQLite + 启用自动迁移；database.engine 也指向同一文件。"""
    db_file = tmp_path / "lifespan.db"
    url = f"sqlite:///{db_file}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setattr(settings, "auto_create_tables", True)
    monkeypatch.setattr(database, "engine", engine)
    return engine, db_file


def test_lifespan_runs_migrations_and_seeds(migrated_app_environment) -> None:
    from fastapi.testclient import TestClient

    from src.main import create_app

    engine, db_file = migrated_app_environment
    assert not db_file.exists()

    app = create_app()
    with TestClient(app):
        # lifespan 已执行：迁移建表 + RBAC 种子。
        with Session(engine) as session:
            menus = session.exec(select(Menu)).all()
            assert {menu.name for menu in menus} >= {"LogHarvest", "Jobs"}
        assert db_file.exists()

    engine.dispose()


def test_lifespan_skips_seed_when_disabled(migrated_app_environment, monkeypatch) -> None:
    """auto_create_tables=False（测试默认）：lifespan 不碰迁移也不碰种子。"""
    from fastapi.testclient import TestClient

    from src.main import create_app

    _engine, db_file = migrated_app_environment
    monkeypatch.setattr(settings, "auto_create_tables", False)

    with TestClient(create_app()):
        assert not db_file.exists()
