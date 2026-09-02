import pytest
from pydantic import SecretStr, ValidationError

from payrecover.config import Settings


def test_persistence_requires_database_url():
    with pytest.raises(ValidationError, match="DATABASE_URL is required"):
        Settings(_env_file=None, persistence_enabled=True, database_url=None)


def test_database_url_must_use_psycopg():
    with pytest.raises(ValidationError, match=r"postgresql\+psycopg"):
        Settings(
            _env_file=None,
            persistence_enabled=True,
            database_url=SecretStr("sqlite:///payrecover.db"),
        )


def test_database_password_is_not_exposed_in_settings_repr():
    settings = Settings(
        _env_file=None,
        database_url=SecretStr("postgresql+psycopg://user:very-secret@db/payrecover"),
    )
    assert "very-secret" not in repr(settings)
    assert settings.require_database_url().endswith("@db/payrecover")


def test_persistence_can_be_explicitly_disabled_without_a_url():
    settings = Settings(_env_file=None, persistence_enabled=False, database_url=None)
    assert settings.persistence_enabled is False
