from sqlmodel import Session, select

from src.application.services.auth import ensure_admin, role_codes
from src.domain.models.auth import ADMIN_ROLE_CODE, User
from src.domain.services.passwords import verify_password


def test_ensure_admin_is_idempotent(engine) -> None:
    with Session(engine) as session:
        first = ensure_admin(session, "admin", "First-Pass-1")
        second = ensure_admin(session, "admin", "Second-Pass-2")

        assert first.id == second.id
        # 幂等只保证存在，不覆盖已存在的口令。
        assert verify_password("Second-Pass-2", second.password_hash) is False
        assert verify_password("First-Pass-1", second.password_hash) is True
        assert role_codes(session, second) == [ADMIN_ROLE_CODE]
        assert len(session.exec(select(User)).all()) == 1


def _isolate_migrations(monkeypatch) -> None:
    # main() 里的 run_migrations 读的是 settings.database_url，不是被 monkeypatch
    # 换掉的 engine。开发者本地把 .env 指向 PostgreSQL 时，这一步会真的去连库并
    # 在容器没起时超时。夹具已经建好表，这里只需要把迁移这一步跳过。
    monkeypatch.setattr("scripts.seed_admin.run_migrations", lambda url: None)


def test_seed_cli_refuses_weak_password(engine, monkeypatch) -> None:
    monkeypatch.setattr("scripts.seed_admin.engine", engine)
    monkeypatch.setenv("LOGHARVEST_ADMIN_PASSWORD", "short")
    from scripts.seed_admin import main

    assert main() == 1


def test_seed_cli_creates_admin(engine, monkeypatch) -> None:
    _isolate_migrations(monkeypatch)
    monkeypatch.setattr("scripts.seed_admin.engine", engine)
    monkeypatch.setenv("LOGHARVEST_ADMIN_PASSWORD", "Strong-Pass-1")
    from scripts.seed_admin import main

    assert main() == 0
    with Session(engine) as session:
        user = session.exec(select(User)).one()
        assert role_codes(session, user) == [ADMIN_ROLE_CODE]
