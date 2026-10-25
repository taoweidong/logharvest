from src.domain.services.passwords import hash_password, verify_password


def test_same_password_hashes_differ_and_verifies() -> None:
    first = hash_password("Correct-Horse-1")
    second = hash_password("Correct-Horse-1")

    assert first != second
    assert verify_password("Correct-Horse-1", first) is True
    assert verify_password("wrong", first) is False


def test_long_password_is_not_silently_truncated() -> None:
    # bcrypt 只取前 72 字节，长口令必须预哈希，否则尾 differences 会被忽略。
    base = "a" * 80
    variant = "a" * 79 + "b"
    hashed = hash_password(base)

    assert verify_password(base, hashed) is True
    assert verify_password(variant, hashed) is False


def test_empty_password_rejected() -> None:
    try:
        hash_password("")
    except ValueError:
        return
    raise AssertionError("empty password must be rejected")
