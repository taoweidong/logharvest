from datetime import timedelta

from sqlalchemy.engine import Engine
from sqlmodel import Session

from src.domain.models.entities import FileTask, Job, Match, Worker, utcnow


def _add_match(session: Session, created_at) -> None:
    session.add(
        Match(
            job_id=1,
            file_task_id=1,
            rule_id="error",
            rule_set_id=1,
            rule_version=1,
            source_path="/nas/a.zip",
            member_path="app.log",
            file_name="app.log",
            line_no=1,
            raw_line="ERROR boom",
            created_at=created_at,
        )
    )


def test_overview_requires_admin(client) -> None:
    assert client.get("/api/system/overview").status_code == 401


def test_overview_counts_jobs_tasks_and_workers(client, auth_headers, engine: Engine) -> None:
    with Session(engine) as session:
        session.add_all(
            [
                Job(
                    name="a",
                    data_source_id=1,
                    source_path="/nas",
                    rule_set_id=1,
                    status="running",
                ),
                Job(
                    name="b",
                    data_source_id=1,
                    source_path="/nas",
                    rule_set_id=1,
                    status="succeeded",
                ),
                Worker(id="w1", hostname="h", version="0.1.0", status="online"),
                Worker(id="w2", hostname="h", version="0.1.0", status="offline"),
                Worker(id="w3", hostname="h", version="0.2.0", status="busy"),
            ]
        )
        session.commit()
        session.add(
            FileTask(
                job_id=1,
                source_path="/nas/a.zip",
                file_name="a.zip",
                size_bytes=1,
                status="succeeded",
            )
        )
        session.commit()

    response = client.get("/api/system/overview", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["jobs"] == {"running": 1, "succeeded": 1}
    assert body["file_tasks"] == {"succeeded": 1}
    assert body["matches_24h"] == 0
    # ``busy`` is an alive worker; ``offline`` is not counted, but still shows
    # up in the version distribution so upgrades can be tracked.
    assert body["workers"]["online"] == 2
    assert body["workers"]["by_version"] == {"0.1.0": 2, "0.2.0": 1}
    assert body["oldest_pending_seconds"] is None


def test_overview_reports_matches_and_oldest_pending_wait(
    client, auth_headers, engine: Engine, session: Session
) -> None:
    now = utcnow()
    _add_match(session, created_at=now - timedelta(hours=1))
    _add_match(session, created_at=now - timedelta(hours=25))
    session.add(
        Job(
            name="pending-job",
            data_source_id=1,
            source_path="/nas",
            rule_set_id=1,
            status="running",
        )
    )
    session.commit()
    session.add(
        FileTask(
            job_id=1,
            source_path="/nas/b.zip",
            file_name="b.zip",
            size_bytes=1,
            status="pending",
            scheduled_at=now - timedelta(seconds=900),
        )
    )
    session.commit()

    body = client.get("/api/system/overview", headers=auth_headers).json()

    assert body["file_tasks"] == {"pending": 1}
    assert body["matches_24h"] == 1
    assert 890 <= body["oldest_pending_seconds"] <= 1200


def test_maintenance_endpoint_reports_unavailable_without_postgresql(client, auth_headers) -> None:
    """API 测试跑在 SQLite 上：这里必须给出可解释的 503，而不是 500 堆栈。

    两步可移植清扫的真实行为由 tests/application/test_maintenance.py 直接覆盖。
    """
    response = client.post("/api/system/maintenance", headers=auth_headers)

    assert response.status_code == 503
    assert "postgresql" in response.json()["detail"].lower()


def test_maintenance_requires_admin(client) -> None:
    assert client.post("/api/system/maintenance").status_code == 401
