from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.engine import Engine
from sqlmodel import Session

from src.application.services.auth import permission_codes
from src.config.settings import settings
from src.domain.models.auth import User
from src.domain.permissions import ALL_PERMISSION_CODE
from src.domain.services.rate_limit import SlidingWindowLimiter
from src.domain.services.tokens import TokenService
from src.infrastructure.database import engine


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def get_engine() -> Engine:
    """Return the shared engine without checking out a connection.

    Streaming endpoints depend on this instead of ``SessionDep``: they open and
    close a short-lived session per poll instead of pinning a request-scoped
    connection for the whole stream.
    """
    return engine


EngineDep = Annotated[Engine, Depends(get_engine)]

_bearer = HTTPBearer(auto_error=False)


def get_token_service() -> TokenService:
    """Build the token service from current settings.

    不做进程级缓存：缓存会把旧的 ``jwt_secret_key`` 一直留住，测试或运维改密钥后
    新签发的 token 反而验不过。构造成本相对一次 JWT 验签可以忽略。
    """
    return TokenService(
        secret_key=settings.jwt_secret_key,
        access_minutes=settings.access_token_expire_minutes,
        refresh_days=settings.refresh_token_expire_days,
    )


_login_limiter: SlidingWindowLimiter | None = None


def get_login_limiter() -> SlidingWindowLimiter:
    """登录限流器；与 :func:`get_token_service` 的"不缓存"正好相反。

    限流状态本来就必须跨请求存活，缓存它就是目的。阈值只在首次调用时从 settings
    读一次：限流不是密钥，没有轮换需求，而每次登录都重建一个限流器会让限流失效。
    测试需要干净状态时请重置 ``_login_limiter``（conftest 已 autouse 处理）。
    """
    global _login_limiter
    if _login_limiter is None:
        _login_limiter = SlidingWindowLimiter(
            max_attempts=settings.login_max_attempts,
            window_seconds=settings.login_window_seconds,
        )
    return _login_limiter


LoginRateLimiterDep = Annotated[SlidingWindowLimiter, Depends(get_login_limiter)]


def _unauthorized(reason: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=reason,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _access_claims(
    credentials: HTTPAuthorizationCredentials | None,
    service: TokenService,
) -> int:
    """Validate the bearer access token and return the user id it names."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized("missing bearer token")
    claims = service.decode(credentials.credentials, expected_type="access")
    if claims is None:
        raise _unauthorized("invalid or expired token")
    user_id = claims.get("sub")
    if not isinstance(user_id, str) or not user_id.isdigit():
        raise _unauthorized("invalid token payload")
    return int(user_id)


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: SessionDep,
    service: Annotated[TokenService, Depends(get_token_service)],
) -> User:
    user = session.get(User, _access_claims(credentials, service))
    if user is None or not user.is_active:
        raise _unauthorized("user is not available")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_permission(code: str):
    """Build a dependency that requires one permission code.

    Admin short-circuits through the wildcard returned by ``permission_codes``;
    every other user must have the code granted via an active role. Missing /
    invalid tokens and disabled users keep the usual 401 contract.
    """

    def dependency(user: CurrentUser, session: SessionDep) -> User:
        codes = permission_codes(session, user)
        if ALL_PERMISSION_CODE not in codes and code not in codes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"permission '{code}' required",
            )
        return user

    return dependency


def require_permission_streaming(code: str):
    """Permission check for long-lived responses (SSE), without a request session.

    与 :func:`require_permission` 唯一的区别是会话来源：这里按 ``EngineDep``
    自开自闭一条短会话，连接在响应开始前就归还，不会在整个 SSE 期间占住连接。
    """

    def dependency(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
        engine: EngineDep,
        service: Annotated[TokenService, Depends(get_token_service)],
    ) -> User:
        user_id = _access_claims(credentials, service)
        with Session(engine) as session:
            user = session.get(User, user_id)
            codes = permission_codes(session, user) if user is not None else []
        if user is None or not user.is_active:
            raise _unauthorized("user is not available")
        if ALL_PERMISSION_CODE not in codes and code not in codes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"permission '{code}' required",
            )
        return user

    return dependency
