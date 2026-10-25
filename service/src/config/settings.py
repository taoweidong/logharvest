from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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
