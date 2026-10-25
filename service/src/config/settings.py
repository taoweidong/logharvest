from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.domain.archive.reader import (
    DEFAULT_MAX_MEMBER_BYTES,
    DEFAULT_MAX_MEMBERS,
    DEFAULT_MAX_RATIO,
    DEFAULT_MAX_TOTAL_BYTES,
)
from src.domain.source_guard import POLICY_ENFORCE, READ_ONLY_POLICIES

PROTECTED_ENVIRONMENTS = frozenset({"dev", "test"})
MIN_SECRET_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="LOGHARVEST_",
        extra="ignore",
    )

    app_name: str = "LogHarvest"
    environment: str = "dev"
    database_url: str = "sqlite:///./logharvest.db"
    auto_create_tables: bool = True
    worker_poll_seconds: float = 1.0
    worker_version: str = "0.1.0"
    max_concurrent_copies: int = 4
    matches_batch_size: int = 1000
    max_matches_per_task: int = 200_000
    jwt_secret_key: str = ""
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7
    admin_username: str = "admin"
    admin_password: str = ""
    nas_read_only_policy: str = POLICY_ENFORCE
    # 解压预算（规格 §11 压缩炸弹）：单成员 / 整包累计 / 成员数 / 膨胀比，
    # 四个维度都按"解压后"的字节算，超限终止扫描而不是让 Worker 被 OOM 杀掉。
    archive_max_member_bytes: int = Field(default=DEFAULT_MAX_MEMBER_BYTES, gt=0)
    archive_max_total_bytes: int = Field(default=DEFAULT_MAX_TOTAL_BYTES, gt=0)
    archive_max_members: int = Field(default=DEFAULT_MAX_MEMBERS, gt=0)
    archive_max_ratio: float = Field(default=DEFAULT_MAX_RATIO, gt=0)
    # 登录限流：/api/login 是唯一匿名可达、且每次都要跑一次 bcrypt 的接口，
    # 按"用户名 + 来源 IP"限制失败次数（语义见 src/domain/services/rate_limit.py）。
    login_max_attempts: int = Field(default=10, ge=0)
    login_window_seconds: int = Field(default=300, gt=0)

    @model_validator(mode="after")
    def validate_auth_settings(self) -> "Settings":
        secret = self.jwt_secret_key.strip()
        # 每个环境都必须显式提供签名密钥。此前 dev 有一个代码内的回退常量，
        # 而 environment 默认就是 dev —— 忘记配置 LOGHARVEST_ENVIRONMENT 的部署
        # 会接受用这个公开常量伪造的管理员 token。
        if not secret:
            raise ValueError("jwt_secret_key is required (set LOGHARVEST_JWT_SECRET_KEY)")
        if len(secret) < MIN_SECRET_LENGTH:
            raise ValueError(f"jwt_secret_key must be at least {MIN_SECRET_LENGTH} characters")
        self.jwt_secret_key = secret
        if (
            self.environment.strip().lower() not in PROTECTED_ENVIRONMENTS
            and not self.admin_password.strip()
        ):
            raise ValueError("admin_password is required outside dev and test")
        return self

    @model_validator(mode="after")
    def validate_nas_read_only_policy(self) -> "Settings":
        # 只读守卫的策略取值必须是已知的三个词之一：一个拼错的值如果静默降级成
        # "不拦截"，就等于把规格 §7.2 的保护悄悄关掉了。
        policy = self.nas_read_only_policy.strip().lower()
        if policy not in READ_ONLY_POLICIES:
            choices = ", ".join(f'"{name}"' for name in sorted(READ_ONLY_POLICIES))
            raise ValueError(f"nas_read_only_policy must be one of {choices}")
        self.nas_read_only_policy = policy
        return self


settings = Settings()
