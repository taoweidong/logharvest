from pydantic_settings import BaseSettings, SettingsConfigDict


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


settings = Settings()
