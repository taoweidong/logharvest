from typing import get_args

import pytest
from pydantic import ValidationError

from src.application.schemas import DataSourceTestResult
from src.application.services import data_sources as data_sources_service
from src.domain.source_guard import READ_ONLY, UNKNOWN, WRITABLE


def test_create_and_list_data_source(client, auth_headers, tmp_path) -> None:
    root = tmp_path / "nas"
    root.mkdir()
    created = client.post(
        "/api/data-sources",
        headers=auth_headers,
        json={"name": "nas", "root_path": str(root)},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "nas"
    assert body["read_only"] is True

    listed = client.get("/api/data-sources", headers=auth_headers)
    assert listed.status_code == 200
    assert [item["name"] for item in listed.json()] == ["nas"]


def test_data_source_path_test(client, auth_headers, tmp_path) -> None:
    root = tmp_path / "nas"
    root.mkdir()
    source = client.post(
        "/api/data-sources",
        headers=auth_headers,
        json={"name": "nas", "root_path": str(root)},
    ).json()
    response = client.post(
        f"/api/data-sources/{source['id']}/test",
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["exists"] is True
    assert body["is_dir"] is True
    assert body["readable"] is True
    # 本机临时目录既读不到挂载表，也带着写位：判定只能是 unknown，
    # 绝不能把它渲染成"已确认只读"。
    assert body["writability"] == "unknown"
    assert body["read_only_confirmed"] is False


def test_duplicate_data_source_name_returns_conflict(client, auth_headers, tmp_path) -> None:
    root = tmp_path / "nas"
    root.mkdir()
    payload = {"name": "nas", "root_path": str(root)}
    first = client.post("/api/data-sources", headers=auth_headers, json=payload)
    assert first.status_code == 201

    duplicate = client.post("/api/data-sources", headers=auth_headers, json=payload)
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "data source name already exists"

    listed = client.get("/api/data-sources", headers=auth_headers)
    assert [item["name"] for item in listed.json()] == ["nas"]


def test_missing_data_source_path_test_returns_404(client, auth_headers) -> None:
    response = client.post("/api/data-sources/9999/test", headers=auth_headers)
    assert response.status_code == 404
    assert response.json()["detail"] == "data source not found"


def test_data_source_requires_admin(client, tmp_path) -> None:
    response = client.post(
        "/api/data-sources",
        json={"name": "nas", "root_path": str(tmp_path)},
    )
    assert response.status_code == 401


def test_read_only_false_is_rejected(client, auth_headers, tmp_path) -> None:
    root = tmp_path / "nas"
    root.mkdir()
    response = client.post(
        "/api/data-sources",
        headers=auth_headers,
        json={"name": "nas", "root_path": str(root), "read_only": False},
    )
    assert response.status_code == 422


def test_data_source_list_pagination(client, auth_headers, tmp_path) -> None:
    root = tmp_path / "nas"
    root.mkdir()
    for name in ("alpha", "beta", "gamma"):
        response = client.post(
            "/api/data-sources",
            headers=auth_headers,
            json={"name": name, "root_path": str(root)},
        )
        assert response.status_code == 201

    page = client.get("/api/data-sources?limit=2&offset=1", headers=auth_headers)
    assert page.status_code == 200
    assert [item["name"] for item in page.json()] == ["beta", "gamma"]

    assert client.get("/api/data-sources?limit=501", headers=auth_headers).status_code == 422
    assert client.get("/api/data-sources?offset=-1", headers=auth_headers).status_code == 422


def test_data_source_test_reports_unknown_without_claiming_read_only(
    client, auth_headers, tmp_path, monkeypatch
) -> None:
    """无法确认只读时，接口必须说 unknown/false，不能给前端一个假的绿勾。"""
    root = tmp_path / "nas"
    root.mkdir()
    monkeypatch.setattr(
        data_sources_service, "detect_writability", lambda path: "unknown", raising=True
    )
    source = client.post(
        "/api/data-sources",
        headers=auth_headers,
        json={"name": "nas", "root_path": str(root)},
    ).json()

    body = client.post(f"/api/data-sources/{source['id']}/test", headers=auth_headers).json()

    assert body["writability"] == "unknown"
    assert body["read_only_confirmed"] is False
    assert body["exists"] is True


def test_data_source_test_reports_writable_mount(
    client, auth_headers, tmp_path, monkeypatch
) -> None:
    root = tmp_path / "nas"
    root.mkdir()
    monkeypatch.setattr(
        data_sources_service, "detect_writability", lambda path: "writable", raising=True
    )
    source = client.post(
        "/api/data-sources",
        headers=auth_headers,
        json={"name": "nas", "root_path": str(root)},
    ).json()

    body = client.post(f"/api/data-sources/{source['id']}/test", headers=auth_headers).json()

    assert body["writability"] == "writable"
    assert body["read_only_confirmed"] is False


def test_data_source_test_reports_confirmed_read_only(
    client, auth_headers, tmp_path, monkeypatch
) -> None:
    root = tmp_path / "nas"
    root.mkdir()
    monkeypatch.setattr(
        data_sources_service, "detect_writability", lambda path: "read_only", raising=True
    )
    source = client.post(
        "/api/data-sources",
        headers=auth_headers,
        json={"name": "nas", "root_path": str(root)},
    ).json()

    body = client.post(f"/api/data-sources/{source['id']}/test", headers=auth_headers).json()

    assert body["writability"] == "read_only"
    assert body["read_only_confirmed"] is True


def test_data_source_test_model_rejects_a_false_green_check() -> None:
    """响应模型自己也得挡住"verdict=unknown 但 confirmed=True"这种自相矛盾的绿勾。"""
    with pytest.raises(ValidationError):
        DataSourceTestResult(
            exists=True,
            is_dir=True,
            readable=True,
            writability="unknown",
            read_only_confirmed=True,
        )


def test_test_result_verdicts_stay_in_sync_with_the_domain_guard() -> None:
    """接口枚举与领域判定常量必须同名，否则前端会收到一个它认不出来的值。"""
    annotation = DataSourceTestResult.model_fields["writability"].annotation
    assert set(get_args(annotation)) == {READ_ONLY, WRITABLE, UNKNOWN}
