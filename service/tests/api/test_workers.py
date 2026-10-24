from src.domain.models.entities import Worker


def test_drain_and_resume_worker(client, auth_headers, session):
    session.add(Worker(id="w1", hostname="h1", version="0.1.0", status="online"))
    session.commit()
    drain = client.post("/api/workers/w1/drain", headers=auth_headers)
    assert drain.status_code == 200
    assert drain.json()["drain_requested"] is True
    resume = client.post("/api/workers/w1/resume", headers=auth_headers)
    assert resume.status_code == 200
    assert resume.json()["drain_requested"] is False


def test_drain_by_version(client, auth_headers, session):
    session.add(Worker(id="w1", hostname="h1", version="0.1.0", status="online"))
    session.add(Worker(id="w2", hostname="h2", version="0.2.0", status="online"))
    session.commit()
    response = client.post("/api/workers/drain-version?version=0.1.0", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["drained"] == 1


def test_drain_persists_draining_status(client, auth_headers, session):
    session.add(Worker(id="w1", hostname="h1", version="0.1.0", status="online"))
    session.commit()

    client.post("/api/workers/w1/drain", headers=auth_headers)

    session.expire_all()
    worker = session.get(Worker, "w1")
    assert worker is not None
    assert worker.drain_requested is True
    assert worker.status == "draining"


def test_resume_offline_worker_conflicts(client, auth_headers, session):
    session.add(Worker(id="w1", hostname="h1", version="0.1.0", status="offline"))
    session.commit()

    response = client.post("/api/workers/w1/resume", headers=auth_headers)

    assert response.status_code == 409
    session.expire_all()
    worker = session.get(Worker, "w1")
    assert worker is not None
    assert worker.status == "offline"


def test_drain_offline_worker_keeps_offline_status(client, auth_headers, session):
    session.add(Worker(id="w1", hostname="h1", version="0.1.0", status="offline"))
    session.commit()

    response = client.post("/api/workers/w1/drain", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {"id": "w1", "drain_requested": True, "status": "offline"}
    session.expire_all()
    worker = session.get(Worker, "w1")
    assert worker is not None
    assert worker.status == "offline"
    assert worker.drain_requested is True


def test_drain_version_skips_offline_workers_and_persists_flags(client, auth_headers, session):
    session.add_all(
        [
            Worker(id="live", hostname="h1", version="0.1.0", status="online"),
            Worker(id="busy", hostname="h2", version="0.1.0", status="busy"),
            Worker(id="dead", hostname="h3", version="0.1.0", status="offline"),
            Worker(id="newer", hostname="h4", version="0.2.0", status="online"),
        ]
    )
    session.commit()

    response = client.post("/api/workers/drain-version?version=0.1.0", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {"drained": 2}
    session.expire_all()
    live = session.get(Worker, "live")
    busy = session.get(Worker, "busy")
    dead = session.get(Worker, "dead")
    newer = session.get(Worker, "newer")
    assert live is not None and live.drain_requested is True and live.status == "draining"
    assert busy is not None and busy.drain_requested is True and busy.status == "draining"
    assert dead is not None and dead.drain_requested is False and dead.status == "offline"
    assert newer is not None and newer.drain_requested is False and newer.status == "online"


def test_unknown_worker_returns_404(client, auth_headers):
    assert client.post("/api/workers/nope/drain", headers=auth_headers).status_code == 404
    assert client.post("/api/workers/nope/resume", headers=auth_headers).status_code == 404


def test_drain_version_without_matches_reports_zero(client, auth_headers, session):
    session.add(Worker(id="w1", hostname="h1", version="0.1.0", status="online"))
    session.commit()

    response = client.post("/api/workers/drain-version?version=9.9.9", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {"drained": 0}


def test_worker_management_requires_admin(client):
    response = client.post("/api/workers/w1/drain")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
