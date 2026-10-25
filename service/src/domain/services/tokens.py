from datetime import UTC, datetime, timedelta
from typing import Any

from jose import JWTError, jwt

ACCESS_TYPE = "access"
REFRESH_TYPE = "refresh"
MIN_SECRET_LENGTH = 32


class TokenService:
    """HS256 access/refresh tokens with a ``type`` claim so the two are not interchangeable."""

    def __init__(
        self,
        secret_key: str,
        algorithm: str = "HS256",
        access_minutes: int = 30,
        refresh_days: int = 7,
    ) -> None:
        if len(secret_key) < MIN_SECRET_LENGTH:
            raise ValueError(f"jwt secret must be at least {MIN_SECRET_LENGTH} characters")
        self._secret_key = secret_key
        self._algorithm = algorithm
        self._access_delta = timedelta(minutes=access_minutes)
        self._refresh_delta = timedelta(days=refresh_days)

    def _create(
        self, claims: dict[str, Any], token_type: str, delta: timedelta
    ) -> tuple[str, datetime]:
        expires_at = datetime.now(UTC) + delta
        payload = {**claims, "type": token_type, "exp": expires_at}
        return jwt.encode(payload, self._secret_key, algorithm=self._algorithm), expires_at

    def create_access_token(self, claims: dict[str, Any]) -> tuple[str, datetime]:
        return self._create(claims, ACCESS_TYPE, self._access_delta)

    def create_refresh_token(self, claims: dict[str, Any]) -> tuple[str, datetime]:
        return self._create(claims, REFRESH_TYPE, self._refresh_delta)

    def decode(self, token: str, *, expected_type: str) -> dict[str, Any] | None:
        """Return verified claims (sans ``exp``), or None if invalid, expired, or wrong type."""
        try:
            payload = jwt.decode(token, self._secret_key, algorithms=[self._algorithm])
        except JWTError:
            return None
        if payload.get("type") != expected_type:
            return None
        return {k: v for k, v in payload.items() if k != "exp"} or None
