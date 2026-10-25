from sqlmodel import Session, select

from src.domain.models.auth import ADMIN_ROLE_CODE, Role, User, UserRole
from src.domain.services.passwords import hash_password


def ensure_admin(session: Session, username: str, password: str) -> User:
    """Idempotently create the admin user plus its role link."""
    user = session.exec(select(User).where(User.username == username)).first()
    if user is None:
        user = User(username=username, password_hash=hash_password(password), nickname=username)
        session.add(user)
        session.commit()
        session.refresh(user)

    role = session.exec(select(Role).where(Role.code == ADMIN_ROLE_CODE)).first()
    if role is None:
        role = Role(code=ADMIN_ROLE_CODE, name="管理员")
        session.add(role)
        session.commit()
        session.refresh(role)

    link = session.exec(
        select(UserRole).where(UserRole.user_id == user.id, UserRole.role_id == role.id)
    ).first()
    if link is None:
        session.add(UserRole(user_id=user.id, role_id=role.id))
        session.commit()
    return user


def role_codes(session: Session, user: User) -> list[str]:
    rows = session.exec(
        select(Role.code)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user.id)
    ).all()
    return [str(code) for code in rows]
