from datetime import UTC, datetime

from src.domain.models.entities import Match

SAME_TIMESTAMP = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _add_match(
    session,
    line_no: int,
    rule_id: str = "error",
    file_name: str = "app.log",
    severity: str = "error",
    created_at: datetime | None = None,
) -> None:
    session.add(
        Match(
            job_id=7,
            file_task_id=9,
            rule_id=rule_id,
            rule_set_id=1,
            rule_version=1,
            source_path="/nas/a.zip",
            member_path=file_name,
            file_name=file_name,
            line_no=line_no,
            raw_line=f"line {line_no}",
            fields={"level": "ERROR"},
            severity=severity,
            tags=["tag"] if severity == "error" else [],
            created_at=created_at or SAME_TIMESTAMP,
        )
    )


def test_query_matches_by_job(client, auth_headers, session) -> None:
    session.add(
        Match(
            job_id=7,
            file_task_id=9,
            rule_id="error",
            rule_set_id=1,
            rule_version=1,
            source_path="/nas/a.zip",
            member_path="app.log",
            file_name="app.log",
            line_no=3,
            raw_line="ERROR one",
            fields={"level": "ERROR"},
            severity="error",
        )
    )
    session.commit()

    response = client.get("/api/matches?job_id=7", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()[0]
    assert body["line_no"] == 3
    assert body["raw_line"] == "ERROR one"
    assert body["file_task_id"] == 9
    assert body["tags"] == []
    assert body["raw_truncated"] is False


def test_matches_require_job_or_time_range(client, auth_headers) -> None:
    assert client.get("/api/matches", headers=auth_headers).status_code == 422
    assert (
        client.get(
            "/api/matches?created_from=2026-01-01T00:00:00Z",
            headers=auth_headers,
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/matches?created_from=2026-01-02T00:00:00Z&created_to=2026-01-01T00:00:00Z",
            headers=auth_headers,
        ).status_code
        == 422
    )


def test_matches_filter_by_time_range_and_severity(client, auth_headers, session) -> None:
    _add_match(session, 1, created_at=datetime(2025, 12, 31, tzinfo=UTC))
    _add_match(session, 2, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    _add_match(
        session,
        3,
        severity="warning",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    session.commit()

    ranged = client.get(
        "/api/matches?created_from=2026-01-01T00:00:00Z&created_to=2026-01-02T23:59:59Z",
        headers=auth_headers,
    )
    assert ranged.status_code == 200
    assert [item["line_no"] for item in ranged.json()] == [3, 2]

    by_severity = client.get(
        "/api/matches?job_id=7&severity=warning",
        headers=auth_headers,
    )
    assert [item["line_no"] for item in by_severity.json()] == [3]


def test_matches_cursor_limit_and_filters(client, auth_headers, session) -> None:
    _add_match(session, 1)
    _add_match(session, 2)
    _add_match(session, 3)
    _add_match(session, 4, rule_id="warn", file_name="other.log")
    session.commit()

    first_page = client.get("/api/matches?job_id=7&limit=2", headers=auth_headers)
    assert first_page.status_code == 200
    assert [item["line_no"] for item in first_page.json()] == [4, 3]

    last = first_page.json()[-1]
    second_page = client.get(
        "/api/matches",
        headers=auth_headers,
        params={
            "job_id": 7,
            "created_before": last["created_at"],
            "id_before": last["id"],
        },
    )
    assert second_page.status_code == 200
    assert [item["line_no"] for item in second_page.json()] == [2, 1]

    by_rule = client.get("/api/matches?job_id=7&rule_id=warn", headers=auth_headers)
    assert [item["line_no"] for item in by_rule.json()] == [4]

    by_file = client.get(
        "/api/matches?job_id=7&file_name=app.log&limit=1",
        headers=auth_headers,
    )
    assert [item["line_no"] for item in by_file.json()] == [3]


def test_matches_cursor_with_equal_timestamps(client, auth_headers, session) -> None:
    for line_no in range(1, 6):
        _add_match(session, line_no, created_at=SAME_TIMESTAMP)
    session.commit()

    seen: list[int] = []
    params: dict[str, object] = {"job_id": 7, "limit": 2}
    while True:
        response = client.get("/api/matches", headers=auth_headers, params=params)
        assert response.status_code == 200
        page = response.json()
        if not page:
            break
        seen.extend(item["line_no"] for item in page)
        last = page[-1]
        params = {
            "job_id": 7,
            "limit": 2,
            "created_before": last["created_at"],
            "id_before": last["id"],
        }

    assert seen == [5, 4, 3, 2, 1]


def test_matches_cursor_requires_both_parts(client, auth_headers) -> None:
    response = client.get(
        "/api/matches?job_id=7&created_before=2026-01-01T00:00:00Z",
        headers=auth_headers,
    )
    assert response.status_code == 422
