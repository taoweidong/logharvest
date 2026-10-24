from src.domain.models.entities import Match


def _add_match(
    session,
    line_no: int,
    rule_id: str = "error",
    file_name: str = "app.log",
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
            severity="error",
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
    assert response.json()[0]["line_no"] == 3
    assert response.json()[0]["raw_line"] == "ERROR one"


def test_matches_cursor_limit_and_filters(client, auth_headers, session) -> None:
    _add_match(session, 1)
    _add_match(session, 2)
    _add_match(session, 3)
    _add_match(session, 4, rule_id="warn", file_name="other.log")
    session.commit()

    first_page = client.get("/api/matches?job_id=7&limit=2", headers=auth_headers)
    assert first_page.status_code == 200
    assert [item["line_no"] for item in first_page.json()] == [4, 3]

    second_page = client.get("/api/matches?job_id=7&cursor=3", headers=auth_headers)
    assert [item["line_no"] for item in second_page.json()] == [2, 1]

    by_rule = client.get("/api/matches?job_id=7&rule_id=warn", headers=auth_headers)
    assert [item["line_no"] for item in by_rule.json()] == [4]

    by_file = client.get(
        "/api/matches?job_id=7&file_name=app.log&limit=1",
        headers=auth_headers,
    )
    assert [item["line_no"] for item in by_file.json()] == [3]
