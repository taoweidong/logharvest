"""Create or reset the admin account. Idempotent, safe to re-run.

Usage: cd service && uv run --python 3.12 -m scripts.seed_admin
Password comes from LOGHARVEST_ADMIN_PASSWORD; without it the command prompts.
"""

import getpass
import os
import sys

from sqlmodel import Session

from src.application.services.auth import ensure_admin
from src.config.settings import settings
from src.infrastructure.database import engine
from src.infrastructure.migrations import run_migrations

MIN_PASSWORD_LENGTH = 8


def main() -> int:
    password = os.getenv("LOGHARVEST_ADMIN_PASSWORD") or getpass.getpass("admin password: ")
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        print("refusing to seed: password must be at least 8 characters", file=sys.stderr)
        return 1
    # 先补齐 schema：在全新库上直接建管理员会因为 users 表不存在而抛底层
    # OperationalError，让人以为是账号问题。迁移幂等，重复执行没有副作用。
    run_migrations(settings.database_url)
    with Session(engine) as session:
        user = ensure_admin(session, settings.admin_username, password)
        # 读取属性要在会话内完成：commit 后 SQLModel 会过期实例，会话关闭再取就是 DetachedInstance。
        username, user_id = user.username, user.id
    print(f"seeded admin user {username} (id={user_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
