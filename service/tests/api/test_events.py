import asyncio
import json
from typing import get_type_hints

import pytest
from sqlalchemy import delete
from sqlmodel import Session, create_engine, select

from src.api.dependencies import get_engine, get_session
from src.api.v1.routes_events import _event_stream, job_events
from src.domain.models.entities import FileTask, Job
from src.infrastructure.task_queue import PostgresTaskRepository


def seed_job(session, status: str = "running") -> int:
    job = Job(
        name="j",
        data_source_id=1,
        source_path="/nas",
        rule_set_id=1,
        status=status,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    session.add_all(
        [
            FileTask(
                job_id=job.id,
                source_path="/nas/a.zip",
                file_name="a.zip",
                size_bytes=10,
                status="succeeded",
                processed_lines=10,
                matched_rows=2,
            ),
            FileTask(
                job_id=job.id,
                source_path="/nas/b.zip",
                file_name="b.zip",
                size_bytes=20,
                status="pending",
            ),
        ]
    )
    session.commit()
    assert job.id is not None
    return job.id


def parse_events(body: str) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def _dependency_calls(endpoint) -> set:
    """Dependency callables declared on the endpoint signature.

    Uses only public typing/FastAPI surface, so it survives a FastAPI upgrade.
    """
    calls = set()
    for hint in get_type_hints(endpoint, include_extras=True).values():
        for metadata in getattr(hint, "__metadata__", ()):
            dependency = getattr(metadata, "dependency", None)
            if callable(dependency):
                calls.add(dependency)
    return calls


def test_job_events_sse_returns_snapshot(client, auth_headers, session):
    job_id = seed_job(session)

    response = client.get(f"/api/jobs/{job_id}/events?max_events=1", headers=auth_headers)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: progress" in response.text
    events = parse_events(response.text)
    assert events == [
        {
            "job_id": job_id,
            "status": "running",
            "total": 2,
            "by_status": {"succeeded": 1, "pending": 1},
            "processed_lines": 10,
            "matched_rows": 2,
        }
    ]


def test_job_events_route_does_not_inject_request_session():
    calls = _dependency_calls(job_events)

    assert get_session not in calls
    assert get_engine in calls


def test_job_events_returns_404_before_streaming_for_unknown_job(client, auth_headers):
    response = client.get("/api/jobs/999/events?max_events=1", headers=auth_headers)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


def test_job_events_validates_max_events_bounds(client, auth_headers, session):
    job_id = seed_job(session)

    assert (
        client.get(f"/api/jobs/{job_id}/events?max_events=-1", headers=auth_headers).status_code
        == 422
    )
    assert (
        client.get(f"/api/jobs/{job_id}/events?max_events=101", headers=auth_headers).status_code
        == 422
    )


def test_job_events_requires_admin(client, session):
    job_id = seed_job(session)

    assert client.get(f"/api/jobs/{job_id}/events?max_events=1").status_code == 401


def test_event_stream_zero_max_events_is_unbounded(engine, session):
    job_id = seed_job(session)

    async def collect_two() -> tuple[str, str]:
        stream = _event_stream(engine, job_id, 0, poll_seconds=0)
        try:
            return await anext(stream), await anext(stream)
        finally:
            await stream.aclose()

    first, second = asyncio.run(collect_two())

    assert first.startswith("event: progress\ndata: ")
    assert second.startswith("event: progress\ndata: ")
    assert parse_events(first)[0]["job_id"] == job_id


def test_event_stream_stops_after_terminal_snapshot(engine, session):
    job_id = seed_job(session, status="succeeded")

    async def collect() -> list[str]:
        stream = _event_stream(engine, job_id, 0, poll_seconds=0)
        return [event async for event in stream]

    events = asyncio.run(asyncio.wait_for(collect(), timeout=2))

    assert len(events) == 1
    payload = parse_events(events[0])[0]
    assert payload["job_id"] == job_id
    assert payload["status"] == "succeeded"


def test_event_stream_ends_cleanly_when_job_is_deleted(engine, session):
    job_id = seed_job(session)

    async def run() -> str:
        stream = _event_stream(engine, job_id, 0, poll_seconds=0)
        try:
            first = await anext(stream)
            with Session(engine) as cleanup:
                cleanup.execute(delete(FileTask).where(FileTask.job_id == job_id))
                cleanup.execute(delete(Job).where(Job.id == job_id))
                cleanup.commit()
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
            return first
        finally:
            await stream.aclose()

    first = asyncio.run(asyncio.wait_for(run(), timeout=2))

    assert parse_events(first)[0]["job_id"] == job_id


def test_event_stream_stops_when_the_worker_finalizes_the_job(engine, session):
    """End-to-end: closing the job is what ends a live progress stream."""
    job_id = seed_job(session)

    def finish_tasks() -> None:
        with Session(engine) as worker_session:
            for task in worker_session.exec(
                select(FileTask).where(FileTask.job_id == job_id)
            ).all():
                task.status = "succeeded"
                worker_session.add(task)
            worker_session.commit()
        assert PostgresTaskRepository(engine).finalize_job(job_id) == "succeeded"

    async def collect() -> list[str]:
        stream = _event_stream(engine, job_id, 0, poll_seconds=0)
        try:
            events = [await anext(stream)]
            # The last file task lands between two polls.
            finish_tasks()
            events.extend([event async for event in stream])
            return events
        finally:
            await stream.aclose()

    events = asyncio.run(asyncio.wait_for(collect(), timeout=2))

    assert len(events) == 2
    assert parse_events(events[0])[0]["status"] == "running"
    assert parse_events(events[1])[0]["status"] == "succeeded"


def test_sse_route_leaves_the_connection_pool_free(tmp_path) -> None:
    """Regression: M1b made SSE use short sessions; the JWT dependency must not re-pin one.

    A pool of exactly one connection: if the auth dependency holds a request-scoped
    session for the whole stream, the endpoint's own validation session cannot be
    checked out and the request fails instead of streaming.
    """
    from fastapi.testclient import TestClient
    from sqlalchemy.pool import QueuePool

    from src.api.dependencies import get_session
    from src.application.services.auth import ensure_admin
    from src.config.settings import settings
    from src.domain.services.tokens import TokenService
    from src.infrastructure.database import create_db_and_tables
    from src.main import create_app

    pool_engine = create_engine(
        f"sqlite:///{(tmp_path / 'pool.db').as_posix()}",
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=1,
        connect_args={"check_same_thread": False},
    )
    create_db_and_tables(pool_engine)
    with Session(pool_engine) as session:
        user = ensure_admin(session, "admin", "Pool-Password-1")
        job = Job(name="j", data_source_id=1, source_path="/nas", rule_set_id=1, status="running")
        session.add(job)
        session.commit()
        job_id = job.id
        token, _ = TokenService(secret_key=settings.jwt_secret_key).create_access_token(
            {"sub": str(user.id), "username": user.username}
        )

    def _session():
        with Session(pool_engine) as db:
            yield db

    app = create_app()
    app.dependency_overrides[get_engine] = lambda: pool_engine
    app.dependency_overrides[get_session] = _session
    try:
        with TestClient(app) as client:
            response = client.get(
                f"/api/jobs/{job_id}/events?max_events=1",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200, response.text
            assert parse_events(response.text)[0]["job_id"] == job_id
            # 连接已归还：同一个单连接池还能服务普通请求。
            assert client.get("/api/health").status_code == 200
    finally:
        app.dependency_overrides.clear()
        pool_engine.dispose()
