from datetime import datetime

from sqlmodel import Field, SQLModel

from src.domain.models.entities import utcnow

ADMIN_ROLE_CODE = "admin"


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    nickname: str = ""
    is_active: bool = True
    created_at: datetime = Field(default_factory=utcnow)


class Role(SQLModel, table=True):
    __tablename__ = "roles"

    id: int | None = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)
    name: str


class UserRole(SQLModel, table=True):
    __tablename__ = "user_roles"

    user_id: int = Field(foreign_key="users.id", primary_key=True)
    role_id: int = Field(foreign_key="roles.id", primary_key=True)
