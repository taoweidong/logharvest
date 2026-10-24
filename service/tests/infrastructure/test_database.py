from src.infrastructure import database
from src.infrastructure.database import UTC_SESSION_OPTIONS, connect_args_for


def test_postgres_sessions_are_pinned_to_utc() -> None:
    for url in ("postgresql://u@h/db", "postgresql+psycopg://u@h/db"):
        assert connect_args_for(url) == {"options": UTC_SESSION_OPTIONS}


def test_other_databases_receive_no_session_arguments() -> None:
    assert connect_args_for("sqlite:///./logharvest.db") == {}


def test_engine_builder_passes_the_session_arguments(monkeypatch) -> None:
    captured = {}

    def fake_create_engine(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(database, "create_engine", fake_create_engine)

    database.create_engine_from_settings("postgresql+psycopg://u@h/db")

    assert captured["url"] == "postgresql+psycopg://u@h/db"
    assert captured["connect_args"] == {"options": UTC_SESSION_OPTIONS}


def test_engine_builder_keeps_working_without_the_option(monkeypatch) -> None:
    captured = {}

    def fake_create_engine(url, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(database, "create_engine", fake_create_engine)

    database.create_engine_from_settings("sqlite://")

    assert captured["connect_args"] == {}
