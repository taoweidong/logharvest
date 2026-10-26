from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlmodel import select

from src.api.dependencies import (
    CurrentUser,
    LoginRateLimiterDep,
    SessionDep,
    get_token_service,
)
from src.application.services.auth import permission_codes, role_codes
from src.application.services.navigation import build_user_routes
from src.domain.models.auth import User
from src.domain.services.passwords import verify_password
from src.domain.services.rate_limit import attempt_key
from src.domain.services.tokens import TokenService

router = APIRouter(tags=["auth"])
_login_logger = structlog.get_logger(__name__)


def ok(data: Any) -> dict[str, Any]:
    """Baseline frontend envelope: the Pure Admin store gates on ``data.code === 0``."""
    return {"code": 0, "message": "ok", "data": data}


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=1, max_length=256)


class RefreshRequest(BaseModel):
    refreshToken: str = Field(min_length=1)  # camelCase matches the frontend login contract

    @field_validator("refreshToken")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("refreshToken must not be blank")
        return value


# 用户名不存在时也要付一次 bcrypt 校验的成本。否则"未知用户"分支直接短路返回，
# 响应时间差会把用户名是否存在泄露出来（实测 ~450ms 对 ~5ms）。这是预先算好的
# bcrypt 摘要，对应一个无用的占位口令，不属于任何真实账号。
UNKNOWN_USER_HASH = "$2b$12$XWjXDfmfNHDTb63JG0jtJOXDqa6agq99SX/8aaxacUWU.1dB4lm7C"


@router.post("/login")
def login(
    request: Request,
    payload: LoginRequest,
    session: SessionDep,
    service: Annotated[TokenService, Depends(get_token_service)],
    limiter: LoginRateLimiterDep,
):
    # 限流排在所有工作之前：被限流的请求连 bcrypt 都不该跑。放在后面就只能压低爆破
    # 速度，挡不住它最该挡的东西——一次登录约 100ms 的 CPU 占用本身。
    key = attempt_key(payload.username, request.client.host if request.client else None)
    if not limiter.check(key):
        retry_after = limiter.retry_after(key)
        _login_logger.warning(
            "login_rate_limited", username=payload.username, retry_after=retry_after
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many login attempts; try again later",
            headers={"Retry-After": str(retry_after)},
        )

    user = session.exec(select(User).where(User.username == payload.username)).first()
    password_ok = verify_password(
        payload.password, user.password_hash if user is not None else UNKNOWN_USER_HASH
    )
    if user is None or not password_ok:
        limiter.record_failure(key)
        raise HTTPException(status_code=401, detail="invalid credentials")
    if not user.is_active:
        # 口令是对的，只是账号停用了：这不算一次凭据失败。计进去等于给"停用账号"
        # 配一把反向的锁——任何人拿错口令都能把这个账号在限流里按住。
        raise HTTPException(status_code=403, detail="user is disabled")
    # 登录成功即清空该来源的失败记录，免得用户被自己早先的输错挡在门外。
    limiter.reset(key)

    access_token, expires_at = service.create_access_token(
        {"sub": str(user.id), "username": user.username}
    )
    refresh_token, _ = service.create_refresh_token(
        {"sub": str(user.id), "username": user.username}
    )
    return ok(
        {
            "avatar": None,
            "username": user.username,
            "nickname": user.nickname,
            "roles": role_codes(session, user),
            "permissions": permission_codes(session, user),
            "accessToken": access_token,
            "refreshToken": refresh_token,
            "expires": int(expires_at.timestamp() * 1000),
        }
    )


@router.post("/refresh-token")
def refresh(
    payload: RefreshRequest,
    session: SessionDep,
    service: Annotated[TokenService, Depends(get_token_service)],
):
    claims = service.decode(payload.refreshToken, expected_type="refresh")
    if claims is None:
        raise HTTPException(status_code=401, detail="invalid refresh token")
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.isdigit():
        raise HTTPException(status_code=401, detail="invalid refresh token")
    user = session.get(User, int(subject))
    if user is None or not user.is_active or user.username != claims.get("username"):
        raise HTTPException(status_code=401, detail="invalid refresh token")

    access_token, expires_at = service.create_access_token(
        {"sub": str(user.id), "username": user.username}
    )
    new_refresh, _ = service.create_refresh_token({"sub": str(user.id), "username": user.username})
    return ok(
        {
            "accessToken": access_token,
            "refreshToken": new_refresh,
            "expires": int(expires_at.timestamp() * 1000),
        }
    )


@router.get("/auth/routes")
def auth_routes(user: CurrentUser, session: SessionDep):
    # 菜单即权限：路由树由 menus 表动态生成，前端不再维护静态业务菜单。
    return ok(build_user_routes(session, user))


@router.post("/logout")
def logout(_: CurrentUser):
    # 无服务端会话；JWT 自然过期。
    return ok(None)
