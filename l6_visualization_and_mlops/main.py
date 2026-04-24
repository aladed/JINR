"""FastAPI entrypoint for L6 visualization and feedback API."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from l6_visualization_and_mlops.api import routes
from l6_visualization_and_mlops.core.config import get_settings
from l6_visualization_and_mlops.core.logger import get_logger, setup_logger
from l6_visualization_and_mlops.db import models
from l6_visualization_and_mlops.db.database import SessionLocal, engine
from l6_visualization_and_mlops.pipeline.consumer import L6KafkaConsumer

setup_logger()
logger = get_logger(__name__)
settings = get_settings()

models.Base.metadata.create_all(bind=engine)
logger.info("Database tables initialized.")

kafka_consumer_settings: dict[str, object] = {
    "bootstrap.servers": settings.kafka_bootstrap_servers,
    "group.id": "l6-visualization-consumer",
    "auto.offset.reset": "earliest",
    "enable.auto.commit": False,
}
consumer_thread = L6KafkaConsumer(
    kafka_settings=kafka_consumer_settings,
    topic=settings.kafka_l6_topic,
    db_session_factory=SessionLocal,
    logger=logger,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage startup and shutdown lifecycle for background Kafka consumer.

    Args:
        app: FastAPI application instance.

    Yields:
        None: Control back to FastAPI while app is running.
    """

    del app
    logger.info("Starting background L6KafkaConsumer thread...")
    consumer_thread.start()
    try:
        yield
    finally:
        logger.info("Stopping background L6KafkaConsumer thread...")
        consumer_thread.stop()
        consumer_thread.join(timeout=10.0)
        logger.info("Background L6KafkaConsumer thread stopped.")


app = FastAPI(lifespan=lifespan, title="Govorun MLOps API")
app.include_router(routes.router)
