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


def test_verify_password_rejects_empty_inputs() -> None:
    hashed = hash_password("Correct-Horse-1")
    # 任一侧为空都不该进 bcrypt（空口令在 hash 阶段已被拒，防御性兜底在 verify 侧）。
    assert verify_password("", hashed) is False
    assert verify_password("Correct-Horse-1", "") is False


def test_verify_password_survives_malformed_hash() -> None:
    # 畸形 hash（非 bcrypt 格式）会让 checkpw 抛 ValueError，必须按"不匹配"处理，
    # 而不是把异常冒泡成 500——这正是 UNKNOWN_USER_HASH 时序防护的兜底语义。
    assert verify_password("anything", "not-a-bcrypt-hash") is False
