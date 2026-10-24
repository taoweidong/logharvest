import secrets
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.engine import Engine
from sqlmodel import Session

from src.config.settings import settings
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


def require_admin(
    authorization: str | None = Header(default=None),
) -> str:
    header = authorization or ""
    token = header[len("Bearer ") :] if header.startswith("Bearer ") else ""
    expected = settings.admin_token
    if (
        not token
        or not expected
        or not secrets.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid admin token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return "admin"


AdminDep = Annotated[str, Depends(require_admin)]
