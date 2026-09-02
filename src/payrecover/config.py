from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    """Runtime settings loaded lazily by application dependencies."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    persistence_enabled: bool = True
    database_url: SecretStr | None = Field(default=None, repr=False)
    razorpay_webhook_secret: SecretStr | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def validate_database_url(self) -> "Settings":
        if not self.persistence_enabled:
            return self
        if self.database_url is None:
            raise ValueError("DATABASE_URL is required when persistence is enabled")

        raw_url = self.database_url.get_secret_value()
        try:
            url = make_url(raw_url)
        except ValueError as exc:
            raise ValueError("DATABASE_URL must be a valid SQLAlchemy URL") from exc
        if url.get_backend_name() != "postgresql" or url.get_driver_name() != "psycopg":
            raise ValueError("DATABASE_URL must use postgresql+psycopg")
        return self

    def require_database_url(self) -> str:
        if not self.persistence_enabled or self.database_url is None:
            raise RuntimeError("PostgreSQL persistence is not configured")
        return self.database_url.get_secret_value()

    def require_razorpay_webhook_secret(self) -> str:
        if self.razorpay_webhook_secret is None:
            raise RuntimeError("Razorpay webhook ingestion is not configured")
        secret = self.razorpay_webhook_secret.get_secret_value()
        if not secret:
            raise RuntimeError("Razorpay webhook ingestion is not configured")
        return secret


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    return Settings()
