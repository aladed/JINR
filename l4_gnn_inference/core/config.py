"""Application configuration for the L4 inference microservice."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration settings loaded from environment variables.

    Attributes:
        kafka_bootstrap_servers: Kafka broker addresses.
        kafka_input_topic: Kafka input topic from L3 layer.
        kafka_output_topic: Kafka output topic to L5 layer.
        redis_host: Redis host name or IP.
        redis_port: Redis TCP port.
        hidden_channels: Hidden channel size for the GNN model.
        num_heads: Number of attention heads in GATv2 layers.
        threshold_window: Rolling window size for dynamic thresholding.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    kafka_bootstrap_servers: str = Field(
        default="localhost:9092",
        description="Comma-separated list of Kafka brokers.",
    )
    kafka_input_topic: str = Field(
        default="L3_IN",
        description="Input topic consumed by L4 inference.",
    )
    kafka_output_topic: str = Field(
        default="L5_OUT",
        description="Output topic for L5 alerting pipeline.",
    )

    redis_host: str = Field(
        default="localhost",
        description="Redis host used for deduplication keys.",
    )
    redis_port: int = Field(
        default=6379,
        description="Redis port used for deduplication keys.",
    )

    hidden_channels: int = Field(
        default=64,
        ge=1,
        description="Hidden representation size of the model.",
    )
    num_heads: int = Field(
        default=4,
        ge=1,
        description="Number of attention heads in GATv2 layers.",
    )
    threshold_window: int = Field(
        default=200,
        ge=10,
        description="Window size for dynamic threshold computation.",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached settings instance.

    Returns:
        Settings: Validated application settings object.
    """

    return Settings()
