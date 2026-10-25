import hashlib

import bcrypt

# bcrypt 只看前 72 字节，超长的口令必须先摘要，否则尾部差异被静默忽略。
_BCRYPT_MAX_BYTES = 72


def _digest(password: str) -> bytes:
    encoded = password.encode("utf-8")
    if len(encoded) > _BCRYPT_MAX_BYTES:
        return hashlib.sha256(encoded).hexdigest().encode("utf-8")
    return encoded


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("password must not be empty")
    return bcrypt.hashpw(_digest(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    if not password or not hashed:
        return False
    try:
        return bcrypt.checkpw(_digest(password), hashed.encode("utf-8"))
    except ValueError:
        return False
