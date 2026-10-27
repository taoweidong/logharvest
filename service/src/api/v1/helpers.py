"""路由层的共享小工具：客户端 IP 与时区归一。

历史上 ``_client_ip`` 在六个路由文件里各存一份、``_as_utc`` 存两份，
内联的 ``request.client.host if request.client else None`` 还有六处；
口径改动（例如未来要支持可信代理清单）时不可能逐处同步，所以收拢到这里。
"""

from datetime import UTC, datetime

from fastapi import Request


def client_ip(request: Request) -> str | None:
    """TCP 对端地址；反向代理场景下即代理地址（不信任 X-Forwarded-For，见登录限流）。"""
    return request.client.host if request.client else None


def as_utc(value: datetime) -> datetime:
    """把请求传入的时间归一到 UTC：naive 按 UTC 墙上时间，aware 转 UTC。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
