from sqlmodel import Session

from src.application.services.auth import ensure_admin
from src.domain.models.auth import ADMIN_ROLE_CODE, User
from src.domain.services.tokens import TokenService

UNIT_SECRET = "unit-secret-key-0123456789-abcdef"


def test_login_returns_envelope_with_tokens_and_roles(client, session) -> None:
    ensure_admin(session, "admin", "Sup3r-Secret")

    response = client.post("/api/login", json={"username": "admin", "password": "Sup3r-Secret"})

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["message"] == "ok"
    data = body["data"]
    assert data["username"] == "admin"
    assert data["roles"] == [ADMIN_ROLE_CODE]
    assert data["permissions"] == []
    assert data["accessToken"] and data["refreshToken"]
    assert isinstance(data["expires"], int)


def test_login_rejects_bad_password_and_unknown_user(client, session) -> None:
    ensure_admin(session, "admin", "Sup3r-Secret")

    bad = client.post("/api/login", json={"username": "admin", "password": "nope"})
    unknown = client.post("/api/login", json={"username": "ghost", "password": "x"})
    assert bad.status_code == 401
    assert unknown.status_code == 401


def test_login_on_empty_database_returns_401(client) -> None:
    # 库里没有任何用户时，登录只做校验：返回 401 而不是 500。
    response = client.post("/api/login", json={"username": "admin", "password": "whatever"})
    assert response.status_code == 401


def test_business_route_requires_valid_access_token(client, session) -> None:
    user = ensure_admin(session, "admin", "Sup3r-Secret")
    token, _ = TokenService(secret_key=UNIT_SECRET).create_access_token(
        {"sub": str(user.id), "username": user.username}
    )

    # 未认证 -> 401；错误签名 -> 401；正确 token -> 200。
    missing = client.get("/api/data-sources")
    bogus = client.get("/api/data-sources", headers={"Authorization": "Bearer bogus"})
    valid = client.get("/api/data-sources", headers={"Authorization": f"Bearer {token}"})

    assert missing.status_code == 401
    assert bogus.status_code == 401
    assert valid.status_code == 200


def test_refresh_token_cannot_be_used_as_access_token(client, session) -> None:
    user = ensure_admin(session, "admin", "Sup3r-Secret")
    refresh, _ = TokenService(secret_key=UNIT_SECRET).create_refresh_token(
        {"sub": str(user.id), "username": user.username}
    )

    response = client.get("/api/data-sources", headers={"Authorization": f"Bearer {refresh}"})
    assert response.status_code == 401


def test_refresh_returns_new_access_token(client, session) -> None:
    user = ensure_admin(session, "admin", "Sup3r-Secret")
    service = TokenService(secret_key=UNIT_SECRET)
    refresh, _ = service.create_refresh_token({"sub": str(user.id), "username": user.username})

    response = client.post("/api/refresh-token", json={"refreshToken": refresh})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["accessToken"]
    assert isinstance(data["expires"], int)


def test_refresh_rejects_access_token(client, session) -> None:
    user = ensure_admin(session, "admin", "Sup3r-Secret")
    access, _ = TokenService(secret_key=UNIT_SECRET).create_access_token(
        {"sub": str(user.id), "username": user.username}
    )

    response = client.post("/api/refresh-token", json={"refreshToken": access})
    assert response.status_code == 401


def test_inactive_user_token_is_rejected(client, session) -> None:
    user = ensure_admin(session, "admin", "Sup3r-Secret")
    token, _ = TokenService(secret_key=UNIT_SECRET).create_access_token(
        {"sub": str(user.id), "username": user.username}
    )

    stored = session.get(User, user.id)
    assert stored is not None
    stored.is_active = False
    session.add(stored)
    session.commit()

    response = client.get("/api/data-sources", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_auth_routes_and_logout_require_login(client, session) -> None:
    assert client.get("/api/auth/routes").status_code == 401
    assert client.post("/api/logout").status_code == 401

    user = ensure_admin(session, "admin", "Sup3r-Secret")
    token, _ = TokenService(secret_key=UNIT_SECRET).create_access_token(
        {"sub": str(user.id), "username": user.username}
    )
    headers = {"Authorization": f"Bearer {token}"}

    routes = client.get("/api/auth/routes", headers=headers)
    assert routes.status_code == 200
    assert routes.json() == {"code": 0, "message": "ok", "data": []}

    logout = client.post("/api/logout", headers=headers)
    assert logout.status_code == 200
    assert logout.json() == {"code": 0, "message": "ok", "data": None}


def test_health_does_not_require_auth(client) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "logharvest"}


def test_unauthorized_response_includes_www_authenticate(client) -> None:
    response = client.get("/api/jobs")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_login_uses_injected_session_not_module_engine(client, engine) -> None:
    # 认证写库必须走请求级 session（测试覆盖到内存库），不碰模块级默认引擎。
    with Session(engine) as session:
        user = ensure_admin(session, "admin", "Sup3r-Secret")
        token, _ = TokenService(secret_key=UNIT_SECRET).create_access_token(
            {"sub": str(user.id), "username": user.username}
        )
    assert client.get("/api/jobs", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_unknown_user_still_pays_a_bcrypt_verify(client, monkeypatch) -> None:
    """登录耗时不能泄露用户名是否存在（timing oracle）。"""
    from src.api.v1 import routes_auth

    seen: list[str] = []
    monkeypatch.setattr(
        routes_auth, "verify_password", lambda password, hashed: seen.append(hashed) or False
    )

    response = client.post("/api/login", json={"username": "ghost", "password": "whatever"})

    assert response.status_code == 401
    assert seen == [routes_auth.UNKNOWN_USER_HASH]
    # 哑哈希必须与真实哈希同型，否则成本差异依旧存在。
    assert seen[0].startswith("$2b$")


def test_existing_user_verifies_against_stored_hash(client, session, monkeypatch) -> None:
    from src.api.v1 import routes_auth

    user = ensure_admin(session, "timing", "Ombud-Pass-123")
    seen: list[str] = []
    monkeypatch.setattr(
        routes_auth, "verify_password", lambda password, hashed: seen.append(hashed) or False
    )

    client.post("/api/login", json={"username": "timing", "password": "anything"})

    assert seen == [user.password_hash]


def test_disabled_user_with_correct_password_gets_403(client, session) -> None:
    user = ensure_admin(session, "suspended", "Suspend-Pass-1")
    user.is_active = False
    session.add(user)
    session.commit()

    response = client.post("/api/login", json={"username": "suspended", "password": "Suspend-Pass-1"})

    assert response.status_code == 403
    assert response.json()["detail"] == "user is disabled"


def test_token_service_follows_the_current_secret(monkeypatch) -> None:
    """get_token_service 不能缓存密钥：轮换后旧对象会让新 token 验不过。"""
    from src.api.dependencies import get_token_service
    from src.config import settings as settings_module

    old = settings_module.settings.jwt_secret_key
    monkeypatch.setattr(settings_module.settings, "jwt_secret_key", "rotated-secret-key-0123456789-abcdef")
    try:
        token, _ = get_token_service().create_access_token({"sub": "1"})
        assert get_token_service().decode(token, expected_type="access") == {
            "sub": "1",
            "type": "access",
        }
        assert old != settings_module.settings.jwt_secret_key
    finally:
        monkeypatch.setattr(settings_module.settings, "jwt_secret_key", old)

    stale = get_token_service()
    assert stale.decode(token, expected_type="access") is None
