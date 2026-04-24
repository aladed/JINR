"""Entry point for the L5 RAG + LLM orchestrator microservice."""

from __future__ import annotations

import signal
import sys
import time
from types import FrameType

import redis

from l5_rag_llm.core.config import get_settings
from l5_rag_llm.core.logger import get_logger, setup_logger
from l5_rag_llm.llm.generator import PlaybookGenerator
from l5_rag_llm.pipeline.consumer import AlertConsumer
from l5_rag_llm.pipeline.orchestrator import L5Orchestrator
from l5_rag_llm.pipeline.producer import PlaybookProducer
from l5_rag_llm.rag.metadata_client import MetadataRetriever

is_running = True


def _handle_shutdown_signal(signum: int, frame: FrameType | None) -> None:
    """Handle shutdown signals by stopping the processing loop.

    Args:
        signum: Signal number.
        frame: Current stack frame from signal handler.
    """

    del frame
    global is_running
    is_running = False
    logger = get_logger(__name__)
    logger.info("Shutdown signal received: %s. Stopping L5 loop.", signum)


def main() -> int:
    """Start and run the L5 orchestrator service.

    Returns:
        int: Process exit code.
    """

    setup_logger()
    logger = get_logger(__name__)
    logger.info("Starting L5 RAG + LLM orchestrator service.")

    settings = get_settings()
    logger.info("Configuration loaded.")

    logger.info("Connecting to Redis...")
    redis_client = redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        decode_responses=True,
    )
    try:
        redis_client.ping()
        logger.info("Redis ping successful.")
    except Exception as exc:
        logger.critical(
            "Redis connection check failed. Continuing with fallback behavior: %s",
            exc,
            exc_info=True,
        )

    logger.info("Initializing metadata retriever...")
    metadata_retriever = MetadataRetriever(redis_client=redis_client, logger=logger)

    logger.info("Initializing LLM generator...")
    llm_generator = PlaybookGenerator(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model_name=settings.llm_model_name,
        logger=logger,
        request_timeout_seconds=settings.llm_request_timeout_seconds,
    )

    logger.info("Initializing Kafka consumer...")
    consumer = AlertConsumer(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        topic=settings.kafka_l4_topic,
        group_id="l5_rag_llm_group",
    )
    logger.info("Initializing Kafka producer...")
    producer = PlaybookProducer(bootstrap_servers=settings.kafka_bootstrap_servers)

    logger.info("Starting L5 Orchestrator...")
    orchestrator = L5Orchestrator(
        consumer=consumer,
        producer=producer,
        metadata_retriever=metadata_retriever,
        llm_generator=llm_generator,
        output_topic=settings.kafka_l6_topic,
        logger=logger,
    )

    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    logger.info("Signal handlers registered.")

    try:
        while is_running:
            try:
                orchestrator.process_next_alert()
            except Exception as exc:
                logger.error("Unhandled error in orchestrator loop: %s", exc, exc_info=True)
                time.sleep(1)
    finally:
        logger.info("Stopping L5 service. Cleaning resources...")
        consumer.close()
        producer.flush()
        redis_client.close()
        logger.info("L5 service stopped successfully.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
