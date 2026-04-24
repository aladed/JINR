"""Configuration module for the L6 visualization and MLOps backend."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    Attributes:
        kafka_bootstrap_servers: Comma-separated list of Kafka brokers.
        kafka_l6_topic: Topic where L5 publishes final playbooks.
        database_url: SQLAlchemy connection URL for replay buffer storage.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    kafka_bootstrap_servers: str = Field(
        default="localhost:9092",
        description="Kafka bootstrap servers.",
    )
    kafka_l6_topic: str = Field(
        default="L6_OUT",
        description="Kafka topic consumed by the L6 backend.",
    )
    database_url: str = Field(
        default="sqlite:///./experience_replay.db",
        description="SQLAlchemy database URL for incident history storage.",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings.

    Returns:
        Settings: Validated settings object.
    """

    return Settings()
