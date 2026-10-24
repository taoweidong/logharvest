from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlmodel import Session

from src.config.settings import settings
from src.infrastructure.database import engine


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def require_admin(
    authorization: str | None = Header(default=None),
) -> str:
    expected = f"Bearer {settings.admin_token}"
    if authorization != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid admin token",
        )
    return "admin"


AdminDep = Annotated[str, Depends(require_admin)]
