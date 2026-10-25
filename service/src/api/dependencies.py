from collections.abc import Iterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.engine import Engine
from sqlmodel import Session

from src.application.services.auth import role_codes
from src.config.settings import settings
from src.domain.models.auth import ADMIN_ROLE_CODE, User
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


@lru_cache(maxsize=1)
def get_token_service() -> TokenService:
    # settings 已强制 jwt_secret_key 非空且足够长，这里不再有代码内的回退密钥。
    return TokenService(
        secret_key=settings.jwt_secret_key,
        access_minutes=settings.access_token_expire_minutes,
        refresh_days=settings.refresh_token_expire_days,
    )


def _unauthorized(reason: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=reason,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: SessionDep,
    service: Annotated[TokenService, Depends(get_token_service)],
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized("missing bearer token")
    claims = service.decode(credentials.credentials, expected_type="access")
    if claims is None:
        raise _unauthorized("invalid or expired token")
    user_id = claims.get("sub")
    if not isinstance(user_id, str) or not user_id.isdigit():
        raise _unauthorized("invalid token payload")
    user = session.get(User, int(user_id))
    if user is None or not user.is_active:
        raise _unauthorized("user is not available")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin_user(user: CurrentUser, session: SessionDep) -> User:
    if ADMIN_ROLE_CODE not in role_codes(session, user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required")
    return user


AdminDep = Annotated[User, Depends(require_admin_user)]
