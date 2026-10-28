"""路由层的共享小工具：客户端 IP 与时区归一。

历史上 ``_client_ip`` 在六个路由文件里各存一份、``_as_utc`` 存两份，
内联的 ``request.client.host if request.client else None`` 还有六处；
口径改动（例如支持可信代理清单）时不可能逐处同步，所以收拢到这里。
"""

import ipaddress
from datetime import UTC, datetime
from functools import lru_cache

from fastapi import Request

from src.config.settings import settings

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


@lru_cache(maxsize=16)
def _trusted_networks(raw: str) -> tuple[IPNetwork, ...]:
    return tuple(
        ipaddress.ip_network(token.strip(), strict=False)
        for token in raw.split(",")
        if token.strip()
    )


def _is_trusted_proxy(peer: str | None, raw: str) -> bool:
    if not peer or not raw:
        return False
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(
        address.version == network.version and address in network
        for network in _trusted_networks(raw)
    )


def _parse_forwarded_ip(value: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def _forwarded_candidates(request: Request) -> list[IPAddress]:
    # X-Forwarded-For 是完整链路，优先级高于单值的 X-Real-IP；倒序遍历时
    # 右侧（更接近本进程）的代理先被跳过。
    candidates: list[IPAddress] = []
    real_ip = _parse_forwarded_ip(request.headers.get("x-real-ip", ""))
    if real_ip is not None:
        candidates.append(real_ip)
    for part in request.headers.get("x-forwarded-for", "").split(","):
        address = _parse_forwarded_ip(part)
        if address is not None:
            candidates.append(address)
    return candidates


def client_ip(request: Request) -> str | None:
    """Resolve the client address, trusting forwarding headers only from proxies.

    A direct client can spoof ``X-Forwarded-For``; therefore the headers are
    ignored unless the TCP peer is in ``LOGHARVEST_TRUSTED_PROXY_CIDRS``. With a
    trusted proxy the chain is walked right-to-left and the first untrusted
    address wins, which is the closest address the proxy actually observed.
    """
    peer = request.client.host if request.client else None
    trusted = settings.trusted_proxy_cidrs
    if not _is_trusted_proxy(peer, trusted):
        return peer
    for address in reversed(_forwarded_candidates(request)):
        if not _is_trusted_proxy(str(address), trusted):
            return str(address)
    return peer


def as_utc(value: datetime) -> datetime:
    """把请求传入的时间归一到 UTC：naive 按 UTC 墙上时间，aware 转 UTC。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
