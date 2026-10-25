from datetime import UTC, datetime, timedelta

import pytest
from jose import jwt

from src.domain.services.tokens import TokenService

SECRET = "unit-test-secret-key-0123456789-abcdef"


def make_service(**overrides) -> TokenService:
    options = {"secret_key": SECRET, "access_minutes": 30, "refresh_days": 7}
    options.update(overrides)
    return TokenService(**options)


def test_access_token_round_trip() -> None:
    service = make_service()

    token, expires_at = service.create_access_token({"sub": "1", "username": "admin"})
    claims = service.decode(token, expected_type="access")

    assert claims == {"sub": "1", "username": "admin", "type": "access"}
    assert expires_at > datetime.now(UTC)


def test_refresh_token_is_not_an_access_token() -> None:
    service = make_service()

    token, _ = service.create_refresh_token({"sub": "1"})

    assert service.decode(token, expected_type="access") is None
    assert service.decode(token, expected_type="refresh") is not None


def test_wrong_secret_and_garbage_are_rejected() -> None:
    token, _ = make_service().create_access_token({"sub": "1"})

    assert (
        make_service(secret_key="other-secret-key-0123456789-abcdef").decode(
            token, expected_type="access"
        )
        is None
    )
    assert make_service().decode("not.a.jwt", expected_type="access") is None


def test_expired_token_is_rejected() -> None:
    service = make_service(access_minutes=-1)

    token, _ = service.create_access_token({"sub": "1"})

    assert service.decode(token, expected_type="access") is None


def test_signed_by_hand_is_still_expired_aware() -> None:
    # 直接签发一个已过期的 token，确认库层面的 exp 校验生效。
    expired = jwt.encode(
        {"sub": "1", "type": "access", "exp": datetime.now(UTC) - timedelta(seconds=1)},
        SECRET,
        algorithm="HS256",
    )

    assert make_service().decode(expired, expected_type="access") is None


def test_secret_key_shorter_than_32_chars_is_rejected() -> None:
    with pytest.raises(ValueError):
        make_service(secret_key="too-short")
