"""业务接口的细粒度权限接线。

这些用例只关心“权限码是否真的挂在路由上”：
- viewer/operator 读取类接口放行；
- 没有对应写/操作权限时必须在业务逻辑之前返回 403；
- 有权限时不能因为旧的 AdminDep 残留而被拦成 403。
"""

from fastapi.testclient import TestClient

READ_ENDPOINTS = (
    "/api/jobs",
    "/api/workers",
    "/api/data-sources",
    "/api/rule-sets",
    # 命中接口强制要求 job_id 或时间范围，否则是 422；这里只验证权限层放行。
    "/api/matches?job_id=1",
    "/api/system/status",
    "/api/system/overview",
)

JOB_PAYLOAD = {
    "name": "permission-check",
    "data_source_id": 1,
    "source_path": "/nas",
    "rule_set_id": 1,
}


def _login(client: TestClient, username: str, password: str) -> dict[str, str]:
    response = client.post("/api/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['accessToken']}"}


def _no_role_headers(client: TestClient, auth_headers: dict[str, str]) -> dict[str, str]:
    username = "no-role-user"
    password = "No-Role-Pass-1"
    created = client.post(
        "/api/users",
        headers=auth_headers,
        json={"username": username, "password": password, "role_ids": []},
    )
    assert created.status_code == 201, created.text
    return _login(client, username, password)


def test_viewer_can_read_business_endpoints(client, viewer_headers) -> None:
    for path in READ_ENDPOINTS:
        response = client.get(path, headers=viewer_headers)
        assert response.status_code == 200, (path, response.text)


def test_viewer_cannot_write_or_operate(client, viewer_headers) -> None:
    cases = (
        ("post", "/api/data-sources", {"name": "x", "root_path": "/tmp"}),
        ("post", "/api/jobs", JOB_PAYLOAD),
        ("post", "/api/workers/drain-version?version=0.1.0", None),
        ("post", "/api/system/maintenance", None),
    )
    for method, path, body in cases:
        response = client.request(method, path, headers=viewer_headers, json=body)
        assert response.status_code == 403, (path, response.status_code, response.text)
        assert "permission" in response.json()["detail"]


def test_operator_has_module_permissions_but_not_data_source_write(
    client, operator_headers
) -> None:
    for path in READ_ENDPOINTS:
        assert client.get(path, headers=operator_headers).status_code == 200, path

    # worker 操作权限已授予；空库下仍返回结构化的 drained/flagged 计数。
    drained = client.post("/api/workers/drain-version?version=0.1.0", headers=operator_headers)
    assert drained.status_code == 200, drained.text
    assert drained.json() == {"drained": 0, "flagged": 0}

    # 维护权限已授予：测试库是 SQLite，端点应在权限之后返回 503 而不是 403。
    maintenance = client.post("/api/system/maintenance", headers=operator_headers)
    assert maintenance.status_code == 503, maintenance.text

    # operator 没有 datasources:write，必须在业务校验之前被权限层拦下。
    denied = client.post(
        "/api/data-sources",
        headers=operator_headers,
        json={"name": "x", "root_path": "/tmp"},
    )
    assert denied.status_code == 403
    assert "datasources:write" in denied.json()["detail"]

    # operator 有 jobs:write：不能出现 403；具体业务错误（数据源不存在）可以是 404/409。
    job = client.post("/api/jobs", headers=operator_headers, json=JOB_PAYLOAD)
    assert job.status_code != 403, job.text


def test_user_without_roles_gets_403_on_every_business_endpoint(client, auth_headers) -> None:
    headers = _no_role_headers(client, auth_headers)

    for path in READ_ENDPOINTS:
        assert client.get(path, headers=headers).status_code == 403, path
    assert client.post("/api/jobs", headers=headers, json=JOB_PAYLOAD).status_code == 403


def test_sse_permission_is_checked_before_job_lookup(client, viewer_headers, auth_headers) -> None:
    # viewer 有 jobs:read：权限通过后进入作业校验，不存在的作业返回 404。
    allowed = client.get("/api/jobs/999999/events?max_events=1", headers=viewer_headers)
    assert allowed.status_code == 404, allowed.text

    # 无角色用户在 SSE 依赖处就必须被拒绝，不能建立长连接。
    headers = _no_role_headers(client, auth_headers)
    denied = client.get("/api/jobs/999999/events?max_events=1", headers=headers)
    assert denied.status_code == 403, denied.text
    assert "jobs:read" in denied.json()["detail"]


def test_admin_can_use_read_and_write_endpoints(client, auth_headers) -> None:
    for path in READ_ENDPOINTS:
        assert client.get(path, headers=auth_headers).status_code == 200, path
    # admin 通过通配权限放行，写接口不会因 RBAC 被拦。
    assert (
        client.post(
            "/api/data-sources",
            headers=auth_headers,
            json={"name": "admin-source", "root_path": "/tmp"},
        ).status_code
        == 201
    )
