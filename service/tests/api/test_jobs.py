import zipfile

from src.domain.models.entities import FileTask, Worker


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

    files = client.get(f"/api/jobs/{job['id']}/files", headers=auth_headers)
    assert files.status_code == 200
    assert files.json()[0]["file_name"] == "a.zip"


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
