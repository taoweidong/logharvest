import zipfile

from sqlmodel import select

from src.api.v1.schemas import JobCreate
from src.application.services import jobs as jobs_service
from src.domain.archive.models import ScanLimitError
from src.domain.models.entities import FileTask, Job, Worker


def make_nas(tmp_path):
    root = tmp_path / "nas"
    root.mkdir()
    batch = root / "batch"
    batch.mkdir()
    with zipfile.ZipFile(batch / "a.zip", "w") as archive:
        archive.writestr("app.log", "ERROR one\n")
    return root, batch


def add_zip(batch, name: str) -> None:
    with zipfile.ZipFile(batch / name, "w") as archive:
        archive.writestr("app.log", "ERROR one\n")


def seed_source_and_rules(client, auth_headers, root):
    source = client.post(
        "/api/data-sources",
        headers=auth_headers,
        json={"name": "nas", "root_path": str(root)},
    ).json()
    rules = client.post(
        "/api/rule-sets",
        headers=auth_headers,
        json={
            "name": "baseline",
            "config": {
                "name": "baseline",
                "file_glob": "**/*.log",
                "rules": [{"id": "e", "match": {"type": "keyword", "any": ["ERROR"]}}],
            },
        },
    ).json()
    return source, rules


def create_job(client, auth_headers, source, rules, source_path, **overrides):
    body = {
        "name": "batch-1",
        "data_source_id": source["id"],
        "source_path": str(source_path),
        "rule_set_id": rules["id"],
        "file_filter": {"recursive": True, "max_depth": 5},
    }
    body.update(overrides)
    return client.post("/api/jobs", headers=auth_headers, json=body)


def test_create_job_creates_file_tasks(client, auth_headers, tmp_path) -> None:
    root, batch = make_nas(tmp_path)
    source, rules = seed_source_and_rules(client, auth_headers, root)
    response = client.post(
        "/api/jobs",
        headers={**auth_headers, "Idempotency-Key": "job-1"},
        json={
            "name": "batch-1",
            "data_source_id": source["id"],
            "source_path": str(batch),
            "rule_set_id": rules["id"],
            "file_filter": {"recursive": True, "max_depth": 5},
        },
    )
    assert response.status_code == 201
    job = response.json()
    assert job["status"] == "running"
    assert job["total_files"] == 1
    assert job["created_by"] == "admin"
    assert job["started_at"] is not None
    assert job["finished_at"] is None

    files = client.get(f"/api/jobs/{job['id']}/files", headers=auth_headers)
    assert files.status_code == 200
    assert files.json()[0]["file_name"] == "a.zip"
    assert files.json()[0]["max_attempts"] == 5
    assert files.json()[0]["worker_id"] is None
    assert files.json()[0]["scheduled_at"] is not None


def test_same_idempotency_key_returns_same_job(client, auth_headers, tmp_path) -> None:
    root, batch = make_nas(tmp_path)
    source, rules = seed_source_and_rules(client, auth_headers, root)
    body = {
        "name": "batch-1",
        "data_source_id": source["id"],
        "source_path": str(batch),
        "rule_set_id": rules["id"],
        "file_filter": {"recursive": True, "max_depth": 5},
    }
    headers = {**auth_headers, "Idempotency-Key": "same-key"}
    first = client.post("/api/jobs", headers=headers, json=body)
    second = client.post("/api/jobs", headers=headers, json=body)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


def test_source_path_outside_root_is_rejected(client, auth_headers, tmp_path) -> None:
    root, _batch = make_nas(tmp_path)
    source, rules = seed_source_and_rules(client, auth_headers, root)
    response = client.post(
        "/api/jobs",
        headers=auth_headers,
        json={
            "name": "bad",
            "data_source_id": source["id"],
            "source_path": str(tmp_path),
            "rule_set_id": rules["id"],
        },
    )
    assert response.status_code == 400


def test_empty_directory_succeeds(client, auth_headers, tmp_path) -> None:
    root, _batch = make_nas(tmp_path)
    empty = root / "empty"
    empty.mkdir()
    source, rules = seed_source_and_rules(client, auth_headers, root)
    response = client.post(
        "/api/jobs",
        headers=auth_headers,
        json={
            "name": "empty",
            "data_source_id": source["id"],
            "source_path": str(empty),
            "rule_set_id": rules["id"],
        },
    )
    assert response.status_code == 201
    assert response.json()["status"] == "succeeded"
    assert response.json()["total_files"] == 0


def test_missing_data_source_or_rule_set_returns_404(client, auth_headers, tmp_path) -> None:
    root, batch = make_nas(tmp_path)
    source, rules = seed_source_and_rules(client, auth_headers, root)

    missing_source = create_job(
        client,
        auth_headers,
        {"id": 9999},
        rules,
        batch,
    )
    assert missing_source.status_code == 404

    missing_rules = create_job(
        client,
        auth_headers,
        source,
        {"id": 9999},
        batch,
    )
    assert missing_rules.status_code == 404


def test_list_get_and_require_admin(client, auth_headers, tmp_path) -> None:
    assert client.get("/api/jobs").status_code == 401

    root, batch = make_nas(tmp_path)
    source, rules = seed_source_and_rules(client, auth_headers, root)
    job = create_job(client, auth_headers, source, rules, batch).json()

    listed = client.get("/api/jobs", headers=auth_headers)
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [job["id"]]

    detail = client.get(f"/api/jobs/{job['id']}", headers=auth_headers)
    assert detail.status_code == 200
    assert detail.json()["name"] == "batch-1"

    missing = client.get("/api/jobs/9999", headers=auth_headers)
    assert missing.status_code == 404


def test_job_files_support_pagination(client, auth_headers, tmp_path) -> None:
    root, batch = make_nas(tmp_path)
    add_zip(batch, "b.zip")
    add_zip(batch, "c.zip")
    source, rules = seed_source_and_rules(client, auth_headers, root)
    job = create_job(client, auth_headers, source, rules, batch).json()
    assert job["total_files"] == 3

    page = client.get(
        f"/api/jobs/{job['id']}/files?limit=1&offset=1",
        headers=auth_headers,
    )
    assert page.status_code == 200
    assert [item["file_name"] for item in page.json()] == ["b.zip"]

    missing = client.get("/api/jobs/9999/files", headers=auth_headers)
    assert missing.status_code == 404


def test_cancel_job_cancels_pending_tasks(client, auth_headers, tmp_path) -> None:
    root, batch = make_nas(tmp_path)
    source, rules = seed_source_and_rules(client, auth_headers, root)
    job = create_job(client, auth_headers, source, rules, batch).json()

    response = client.post(f"/api/jobs/{job['id']}/cancel", headers=auth_headers)
    assert response.status_code == 200
    cancelled = response.json()
    assert cancelled["cancel_requested"] is True
    assert cancelled["status"] == "cancelled"

    files = client.get(f"/api/jobs/{job['id']}/files", headers=auth_headers).json()
    assert [item["status"] for item in files] == ["cancelled"]

    missing = client.post("/api/jobs/9999/cancel", headers=auth_headers)
    assert missing.status_code == 404


def test_cancel_running_job_waits_for_worker(client, auth_headers, session, tmp_path) -> None:
    root, batch = make_nas(tmp_path)
    source, rules = seed_source_and_rules(client, auth_headers, root)
    job = create_job(client, auth_headers, source, rules, batch).json()
    files = client.get(f"/api/jobs/{job['id']}/files", headers=auth_headers).json()

    task = session.get(FileTask, files[0]["id"])
    assert task is not None
    task.status = "running"
    session.add(task)
    session.commit()

    response = client.post(f"/api/jobs/{job['id']}/cancel", headers=auth_headers)
    assert response.status_code == 200
    cancelled = response.json()
    assert cancelled["cancel_requested"] is True
    assert cancelled["status"] == "running"


def test_workers_and_system_status(client, auth_headers, session, tmp_path) -> None:
    root, batch = make_nas(tmp_path)
    source, rules = seed_source_and_rules(client, auth_headers, root)
    create_job(client, auth_headers, source, rules, batch)

    session.add(
        Worker(
            id="w-online",
            hostname="host-1",
            version="0.1.0",
            status="online",
        )
    )
    session.add(
        Worker(
            id="w-busy",
            hostname="host-2",
            version="0.1.0",
            status="busy",
        )
    )
    session.add(
        Worker(
            id="w-offline",
            hostname="host-3",
            version="0.1.0",
            status="offline",
        )
    )
    session.commit()

    status = client.get("/api/system/status", headers=auth_headers)
    assert status.status_code == 200
    body = status.json()
    assert body["file_tasks"] == {"pending": 1}
    assert body["workers_online"] == 2
    assert body["pending_oldest_seconds"] is not None
    assert body["pending_oldest_seconds"] >= 0

    workers = client.get("/api/workers", headers=auth_headers)
    assert workers.status_code == 200
    assert {item["id"] for item in workers.json()} == {"w-online", "w-busy", "w-offline"}
    assert {"id", "status", "last_heartbeat"} <= set(workers.json()[0])

    page = client.get("/api/workers?limit=1&offset=1", headers=auth_headers)
    assert page.status_code == 200
    assert [item["id"] for item in page.json()] == ["w-offline"]

    too_large = client.get("/api/workers?limit=201", headers=auth_headers)
    assert too_large.status_code == 422


def test_file_filter_max_files_cap(client, auth_headers, tmp_path) -> None:
    root, batch = make_nas(tmp_path)
    add_zip(batch, "b.zip")
    source, rules = seed_source_and_rules(client, auth_headers, root)

    over_limit = create_job(
        client,
        auth_headers,
        source,
        rules,
        batch,
        file_filter={"recursive": True, "max_depth": 5, "max_files": 1},
    )
    assert over_limit.status_code == 400
    assert "too many files" in over_limit.json()["detail"]
    assert client.get("/api/jobs", headers=auth_headers).json() == []

    above_schema_cap = create_job(
        client,
        auth_headers,
        source,
        rules,
        batch,
        file_filter={"recursive": True, "max_depth": 5, "max_files": 20_001},
    )
    assert above_schema_cap.status_code == 422


def test_cancel_terminal_job_is_noop(client, auth_headers, tmp_path) -> None:
    root, _batch = make_nas(tmp_path)
    empty = root / "empty"
    empty.mkdir()
    source, rules = seed_source_and_rules(client, auth_headers, root)
    job = create_job(client, auth_headers, source, rules, empty).json()
    assert job["status"] == "succeeded"

    response = client.post(f"/api/jobs/{job['id']}/cancel", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == job["id"]
    assert body["status"] == "succeeded"
    assert body["cancel_requested"] is False


def test_blank_idempotency_key_is_rejected(client, auth_headers, tmp_path) -> None:
    root, batch = make_nas(tmp_path)
    source, rules = seed_source_and_rules(client, auth_headers, root)
    body = {
        "name": "batch-1",
        "data_source_id": source["id"],
        "source_path": str(batch),
        "rule_set_id": rules["id"],
    }

    for key in ("", "   "):
        response = client.post(
            "/api/jobs",
            headers={**auth_headers, "Idempotency-Key": key},
            json=body,
        )
        assert response.status_code == 400
        assert "Idempotency-Key" in response.json()["detail"]

    assert client.get("/api/jobs", headers=auth_headers).json() == []


def test_idempotency_integrity_error_returns_existing_job(
    client, auth_headers, session, tmp_path, monkeypatch
) -> None:
    root, batch = make_nas(tmp_path)
    source, rules = seed_source_and_rules(client, auth_headers, root)

    winner = Job(
        name="winner",
        data_source_id=source["id"],
        source_path=str(batch),
        rule_set_id=rules["id"],
        status="succeeded",
        created_by="admin",
        idempotency_key="race-key",
    )
    session.add(winner)
    session.commit()
    session.refresh(winner)
    winner_id = winner.id

    original_find = jobs_service._find_job_by_idempotency_key
    calls = 0

    def find_after_race(session_, created_by, key):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return original_find(session_, created_by, key)

    monkeypatch.setattr(jobs_service, "_find_job_by_idempotency_key", find_after_race)

    payload = JobCreate(
        name="winner",
        data_source_id=source["id"],
        source_path=str(batch),
        rule_set_id=rules["id"],
    )
    job, created = jobs_service.create_job(session, payload, "admin", "race-key")

    assert created is False
    assert job.id == winner_id
    assert calls == 2


def test_idempotent_replay_body_mismatch_returns_409(client, auth_headers, tmp_path) -> None:
    root, batch = make_nas(tmp_path)
    source, rules = seed_source_and_rules(client, auth_headers, root)
    body = {
        "name": "batch-1",
        "data_source_id": source["id"],
        "source_path": str(batch),
        "rule_set_id": rules["id"],
        "file_filter": {"recursive": True, "max_depth": 5},
    }
    headers = {**auth_headers, "Idempotency-Key": "mismatch-key"}
    first = client.post("/api/jobs", headers=headers, json=body)
    assert first.status_code == 201

    renamed = client.post("/api/jobs", headers=headers, json={**body, "name": "other"})
    assert renamed.status_code == 409
    assert "different request body" in renamed.json()["detail"]

    refiltered = client.post(
        "/api/jobs",
        headers=headers,
        json={**body, "file_filter": {"recursive": True, "max_depth": 4}},
    )
    assert refiltered.status_code == 409

    assert len(client.get("/api/jobs", headers=auth_headers).json()) == 1


def test_idempotency_key_is_trimmed(client, auth_headers, tmp_path) -> None:
    root, batch = make_nas(tmp_path)
    source, rules = seed_source_and_rules(client, auth_headers, root)
    body = {
        "name": "batch-1",
        "data_source_id": source["id"],
        "source_path": str(batch),
        "rule_set_id": rules["id"],
    }

    first = client.post(
        "/api/jobs",
        headers={**auth_headers, "Idempotency-Key": "  trim-key  "},
        json=body,
    )
    assert first.status_code == 201

    second = client.post(
        "/api/jobs",
        headers={**auth_headers, "Idempotency-Key": "trim-key"},
        json=body,
    )
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


def test_suffixes_must_overlap_compressed_archives(client, auth_headers, tmp_path) -> None:
    root, batch = make_nas(tmp_path)
    (batch / "raw.log").write_text("ERROR raw\n", encoding="utf-8")
    source, rules = seed_source_and_rules(client, auth_headers, root)

    rejected = create_job(
        client,
        auth_headers,
        source,
        rules,
        batch,
        file_filter={"recursive": True, "max_depth": 5, "suffixes": [".log"]},
    )
    assert rejected.status_code == 400
    assert "compressed archive" in rejected.json()["detail"]
    assert client.get("/api/jobs", headers=auth_headers).json() == []

    accepted = create_job(
        client,
        auth_headers,
        source,
        rules,
        batch,
        file_filter={
            "recursive": True,
            "max_depth": 5,
            "suffixes": [".LOG", ".zip"],
        },
    )
    assert accepted.status_code == 201
    assert accepted.json()["total_files"] == 1


def test_scan_deadline_returns_503(client, auth_headers, tmp_path, monkeypatch) -> None:
    root, batch = make_nas(tmp_path)
    source, rules = seed_source_and_rules(client, auth_headers, root)

    def raise_deadline(*args, **kwargs):
        raise ScanLimitError("archive scan deadline exceeded", reason="deadline")

    monkeypatch.setattr(jobs_service, "discover_archives", raise_deadline)

    response = create_job(client, auth_headers, source, rules, batch)
    assert response.status_code == 503
    assert "timed out" in response.json()["detail"]
    assert client.get("/api/jobs", headers=auth_headers).json() == []


def test_job_scan_releases_transaction_and_passes_limits(
    client, auth_headers, session, tmp_path, monkeypatch
) -> None:
    root, batch = make_nas(tmp_path)
    source, rules = seed_source_and_rules(client, auth_headers, root)
    payload = JobCreate(
        name="scan",
        data_source_id=source["id"],
        source_path=str(batch),
        rule_set_id=rules["id"],
    )
    seen: dict = {}

    def fake_discover(path, **kwargs):
        seen["in_transaction"] = session.in_transaction()
        seen["path"] = path
        seen.update(kwargs)
        return []

    monkeypatch.setattr(jobs_service, "discover_archives", fake_discover)

    job, created = jobs_service.create_job(session, payload, "admin", " scan-key ")

    assert created is True
    assert seen["in_transaction"] is False
    assert seen["max_files"] == 20_000
    assert seen["deadline_seconds"] == 10.0
    assert seen["suffixes"] is None
    assert job.idempotency_key == "scan-key"


def _seed_job_with_tasks(session, specs, *, job_status="running", cancel_requested=False) -> int:
    """Create a job with one FileTask per (status, name) tuple, returned as its id."""
    job = Job(
        name="seeded",
        data_source_id=1,
        source_path="/nas",
        rule_set_id=1,
        status=job_status,
        cancel_requested=cancel_requested,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    for status, name in specs:
        session.add(
            FileTask(
                job_id=job.id,
                source_path=f"/nas/{name}",
                file_name=name,
                size_bytes=1,
                status=status,
            )
        )
    session.commit()
    return job.id


def test_retry_failed_returns_terminal_tasks_to_queue(client, auth_headers, session) -> None:
    job_id = _seed_job_with_tasks(session, [("failed", "ERROR"), ("succeeded", "OK")])

    response = client.post(f"/api/jobs/{job_id}/files/retry-failed", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {"retried": 1, "remaining_failed": 0}
    session.expire_all()
    statuses = [
        t.status for t in session.exec(select(FileTask).where(FileTask.job_id == job_id)).all()
    ]
    assert sorted(statuses) == ["pending", "succeeded"]


def test_retry_failed_requires_admin(client, session) -> None:
    job_id = _seed_job_with_tasks(session, [("failed", "ERROR")])

    assert client.post(f"/api/jobs/{job_id}/files/retry-failed").status_code == 401


def test_job_files_can_be_filtered_by_status(client, auth_headers, session) -> None:
    job_id = _seed_job_with_tasks(session, [("pending", "a"), ("succeeded", "b"), ("failed", "c")])

    pending = client.get(f"/api/jobs/{job_id}/files?status=pending", headers=auth_headers)

    assert pending.status_code == 200
    assert [item["status"] for item in pending.json()] == ["pending"]
    assert [item["file_name"] for item in pending.json()] == ["a"]

    every = client.get(f"/api/jobs/{job_id}/files", headers=auth_headers)
    assert [item["file_name"] for item in every.json()] == ["a", "b", "c"]
    assert {"status", "attempts"} <= set(every.json()[0])
    assert [item["attempts"] for item in every.json()] == [0, 0, 0]


def test_job_files_status_filter_combines_with_pagination(client, auth_headers, session) -> None:
    job_id = _seed_job_with_tasks(
        session, [("succeeded", "a"), ("succeeded", "b"), ("pending", "c")]
    )

    page = client.get(
        f"/api/jobs/{job_id}/files?status=succeeded&limit=1&offset=1",
        headers=auth_headers,
    )

    assert page.status_code == 200
    assert [item["file_name"] for item in page.json()] == ["b"]


def test_job_files_rejects_unknown_status_filter(client, auth_headers, session) -> None:
    job_id = _seed_job_with_tasks(session, [("pending", "a")])

    unknown = client.get(f"/api/jobs/{job_id}/files?status=nope", headers=auth_headers)
    missing_job = client.get("/api/jobs/9999/files?status=pending", headers=auth_headers)

    assert unknown.status_code == 422
    assert missing_job.status_code == 404


def test_retry_failed_reopens_a_failed_job(client, auth_headers, session) -> None:
    """规格 §7.3 的"重试失败文件"正是面向 failed 作业；作业必须重新变为 running，
    否则回收与终态汇总都看不到它。"""
    job_id = _seed_job_with_tasks(
        session,
        [("failed", "e1"), ("failed", "e2"), ("succeeded", "ok")],
        job_status="failed",
    )

    response = client.post(f"/api/jobs/{job_id}/files/retry-failed", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {"retried": 2, "remaining_failed": 0}
    session.expire_all()
    job = session.get(Job, job_id)
    assert job is not None and job.status == "running" and job.finished_at is None


def test_retry_failed_rejects_cancelled_jobs(client, auth_headers, session) -> None:
    """取消作业的任务由 CLAIM_SQL 排除，重新置 pending 只会永远卡在队列里。"""
    flagged = _seed_job_with_tasks(session, [("failed", "e1")], cancel_requested=True)
    done = _seed_job_with_tasks(session, [("failed", "e2")], job_status="cancelled")

    assert (
        client.post(f"/api/jobs/{flagged}/files/retry-failed", headers=auth_headers).status_code
        == 409
    )
    assert (
        client.post(f"/api/jobs/{done}/files/retry-failed", headers=auth_headers).status_code == 409
    )

    session.expire_all()
    statuses = [t.status for t in session.exec(select(FileTask)).all()]
    assert statuses == ["failed", "failed"]


def test_retry_failed_reports_tasks_left_beyond_the_batch(client, auth_headers, session) -> None:
    """limit 会截断单次重试，剩余数量必须可见，不能静默丢人。"""
    job_id = _seed_job_with_tasks(session, [("failed", f"e{i}") for i in range(201)])

    response = client.post(f"/api/jobs/{job_id}/files/retry-failed", headers=auth_headers)

    assert response.json() == {"retried": 200, "remaining_failed": 1}
