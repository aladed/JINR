"""Application configuration for the L5 RAG + LLM orchestrator."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated settings loaded from environment variables.

    Attributes:
        kafka_bootstrap_servers: Kafka broker addresses.
        kafka_l4_topic: Input Kafka topic for alerts from L4.
        kafka_l6_topic: Output Kafka topic for playbooks to L6.
        redis_host: Redis host for metadata cache.
        redis_port: Redis port for metadata cache.
        llm_api_key: API key for LLM provider.
        llm_base_url: OpenAI-compatible base URL.
        llm_model_name: Model identifier used for generation.
        llm_request_timeout_seconds: Timeout for each LLM request.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    kafka_bootstrap_servers: str = Field(
        default="localhost:9092",
        description="Comma-separated Kafka broker list.",
    )
    kafka_l4_topic: str = Field(
        default="L5_IN",
        description="Input topic that receives alerts from L4.",
    )
    kafka_l6_topic: str = Field(
        default="L6_OUT",
        description="Output topic that sends playbooks to L6.",
    )

    redis_host: str = Field(
        default="localhost",
        description="Redis host for metadata caching.",
    )
    redis_port: int = Field(
        default=6379,
        description="Redis TCP port for metadata caching.",
    )

    llm_api_key: str = Field(
        default="changeme",
        description="API key for OpenAI-compatible API.",
    )
    llm_base_url: str = Field(
        default="http://localhost:11434/v1",
        description="Base URL for OpenAI-compatible endpoint (vLLM/Ollama).",
    )
    llm_model_name: str = Field(
        default="qwen2.5:14b-instruct",
        description="Model name for playbook generation.",
    )
    llm_request_timeout_seconds: float = Field(
        default=20.0,
        gt=0.0,
        description="Timeout in seconds for one LLM API call.",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings instance.

    Returns:
        Settings: Loaded and validated application configuration.
    """

    return Settings()
