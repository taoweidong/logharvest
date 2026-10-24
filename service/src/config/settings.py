from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_ADMIN_TOKEN = "dev-admin-token"
PROTECTED_ENVIRONMENTS = frozenset({"dev", "test"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="LOGHARVEST_",
        extra="ignore",
    )

    app_name: str = "LogHarvest"
    environment: str = "dev"
    admin_token: str = DEFAULT_ADMIN_TOKEN
    database_url: str = "sqlite:///./logharvest.db"
    auto_create_tables: bool = True
    worker_poll_seconds: float = 1.0
    worker_version: str = "0.1.0"
    max_concurrent_copies: int = 4
    matches_batch_size: int = 1000
    max_matches_per_task: int = 200_000

    @model_validator(mode="after")
    def validate_admin_token(self) -> "Settings":
        token = self.admin_token.strip()
        if not token:
            raise ValueError("admin_token must not be empty")
        if (
            self.environment.strip().lower() not in PROTECTED_ENVIRONMENTS
            and token == DEFAULT_ADMIN_TOKEN
        ):
            raise ValueError(
                "the default admin token is only allowed when environment is dev or test"
            )
        self.admin_token = token
        return self


settings = Settings()
