import asyncio
import json

from src.api.v1.routes_events import _event_stream
from src.domain.models.entities import FileTask, Job


def seed_job(session) -> int:
    job = Job(
        name="j",
        data_source_id=1,
        source_path="/nas",
        rule_set_id=1,
        status="running",
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
