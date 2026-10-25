import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from src.domain.models.auth import ADMIN_ROLE_CODE, Role, User, UserRole
from src.domain.services.passwords import hash_password


def test_user_role_and_link_round_trip(engine: Session) -> None:
    with Session(engine) as session:
        user = User(username="admin", password_hash=hash_password("admin123"), nickname="管理员")
        role = Role(code=ADMIN_ROLE_CODE, name="管理员")
        session.add_all([user, role])
        session.commit()
        session.add(UserRole(user_id=user.id, role_id=role.id))
        session.commit()

        stored = session.exec(select(User).where(User.username == "admin")).one()
        links = session.exec(select(UserRole)).all()
        assert stored.is_active is True
        assert stored.created_at is not None
        assert [(link.user_id, link.role_id) for link in links] == [(stored.id, role.id)]


def test_username_is_unique(engine: Session) -> None:
    with Session(engine) as session:
        session.add(User(username="dup", password_hash="x"))
        session.commit()
        session.add(User(username="dup", password_hash="y"))
        with pytest.raises(IntegrityError):
            session.commit()
