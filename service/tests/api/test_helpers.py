from datetime import UTC, datetime, timedelta, timezone

from starlette.requests import Request

from src.api.v1 import helpers


def make_request(
    peer: str | None = "203.0.113.10", headers: dict[str, str] | None = None
) -> Request:
    raw_headers = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": raw_headers,
        "client": (peer, 1234) if peer is not None else None,
    }
    return Request(scope)


def test_untrusted_peer_ignores_forwarded_headers(monkeypatch) -> None:
    monkeypatch.setattr(helpers.settings, "trusted_proxy_cidrs", "10.0.0.0/8")
    request = make_request(
        peer="203.0.113.10",
        headers={"x-forwarded-for": "1.2.3.4", "x-real-ip": "5.6.7.8"},
    )

    assert helpers.client_ip(request) == "203.0.113.10"


def test_trusted_proxy_uses_forwarded_for(monkeypatch) -> None:
    monkeypatch.setattr(helpers.settings, "trusted_proxy_cidrs", "10.0.0.0/8")
    request = make_request(
        peer="10.0.0.5",
        headers={"x-forwarded-for": "203.0.113.9", "x-real-ip": "198.51.100.7"},
    )

    assert helpers.client_ip(request) == "203.0.113.9"


def test_trusted_proxy_walks_the_chain_from_the_right(monkeypatch) -> None:
    monkeypatch.setattr(helpers.settings, "trusted_proxy_cidrs", "10.0.0.0/8,203.0.113.0/24")
    request = make_request(
        peer="10.0.0.5",
        headers={"x-forwarded-for": "198.51.100.7, 203.0.113.9, 10.0.0.6"},
    )

    # 10.0.0.6 与 203.0.113.9 都是可信代理；最右侧第一个不可信地址才是客户端。
    assert helpers.client_ip(request) == "198.51.100.7"


def test_trusted_proxy_falls_back_to_x_real_ip(monkeypatch) -> None:
    monkeypatch.setattr(helpers.settings, "trusted_proxy_cidrs", "10.0.0.0/8")
    request = make_request(peer="10.0.0.5", headers={"x-real-ip": "203.0.113.9"})

    assert helpers.client_ip(request) == "203.0.113.9"


def test_malformed_forwarded_headers_fall_back_to_peer(monkeypatch) -> None:
    monkeypatch.setattr(helpers.settings, "trusted_proxy_cidrs", "10.0.0.0/8")
    request = make_request(
        peer="10.0.0.5",
        headers={"x-forwarded-for": "not-an-ip", "x-real-ip": "also-bad"},
    )

    assert helpers.client_ip(request) == "10.0.0.5"


def test_ipv6_trusted_proxy_is_supported(monkeypatch) -> None:
    monkeypatch.setattr(helpers.settings, "trusted_proxy_cidrs", "2001:db8::/32")
    request = make_request(peer="2001:db8::1", headers={"x-forwarded-for": "2001:db9::5"})

    assert helpers.client_ip(request) == "2001:db9::5"


def test_missing_client_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(helpers.settings, "trusted_proxy_cidrs", "10.0.0.0/8")

    assert helpers.client_ip(make_request(peer=None)) is None


def test_as_utc_treats_naive_values_as_utc_wall_time() -> None:
    naive = datetime(2026, 10, 29, 8, 0, 0)

    assert helpers.as_utc(naive) == naive.replace(tzinfo=UTC)


def test_as_utc_converts_aware_values() -> None:
    aware = datetime(2026, 10, 29, 8, 0, 0, tzinfo=timezone(timedelta(hours=2)))

    assert helpers.as_utc(aware) == datetime(2026, 10, 29, 6, 0, 0, tzinfo=UTC)
