"""Entry point for the L4 GNN inference microservice."""

from __future__ import annotations

import signal
import sys
from types import FrameType

import redis
import torch

from l4_gnn_inference.core.config import get_settings
from l4_gnn_inference.core.logger import get_logger, setup_logger
from l4_gnn_inference.models.gatv2_hetero import HeteroIncidentGATv2
from l4_gnn_inference.pipeline.consumer import GraphSnapshotConsumer
from l4_gnn_inference.pipeline.inference_engine import InferenceEngine
from l4_gnn_inference.pipeline.producer import IncidentProducer
from l4_gnn_inference.utils.thresholding import DynamicThreshold

is_running = True


def _handle_shutdown_signal(signum: int, frame: FrameType | None) -> None:
    """Handle SIGINT/SIGTERM and request graceful shutdown.

    Args:
        signum: Numeric signal identifier.
        frame: Current stack frame provided by signal module.
    """

    del frame
    global is_running
    is_running = False
    logger = get_logger(__name__)
    logger.info("Shutdown signal received: signum=%s. Stopping service loop.", signum)


def main() -> int:
    """Run the L4 inference microservice process.

    Returns:
        int: Process exit code.
    """

    setup_logger()
    logger = get_logger(__name__)
    logger.info("Starting L4 GNN inference service.")

    settings = get_settings()
    logger.info("Configuration loaded successfully.")

    try:
        redis_client = redis.Redis(host=settings.redis_host, port=settings.redis_port, decode_responses=True)
        redis_client.ping()
        logger.info("Connected to Redis at %s:%s.", settings.redis_host, settings.redis_port)
    except Exception as exc:  # pragma: no cover - startup failure path
        logger.critical("Redis connectivity check failed: %s", exc, exc_info=True)
        return 1

    model = HeteroIncidentGATv2(
        hidden_channels=settings.hidden_channels,
        out_channels=1,
        num_heads=settings.num_heads,
    )
    # TODO: Заменить на реальную загрузку весов torch.load(...) после этапа обучения
    mock_state_dict = torch.load("model_weights.pt", map_location="cpu") if False else {}
    model.load_state_dict(mock_state_dict, strict=False)
    logger.info("Model initialized with mock weight-loading path.")

    threshold_calc = DynamicThreshold(window_size=settings.threshold_window)
    logger.info("Dynamic threshold calculator initialized.")

    consumer = GraphSnapshotConsumer(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        topic=settings.kafka_input_topic,
        group_id="l4_gnn_inference_group",
    )
    producer = IncidentProducer(bootstrap_servers=settings.kafka_bootstrap_servers)
    engine = InferenceEngine(
        model=model,
        threshold_calc=threshold_calc,
        producer=producer,
        output_topic=settings.kafka_output_topic,
        redis_client=redis_client,
    )
    logger.info("Kafka pipeline and inference engine initialized.")

    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    logger.info("Signal handlers registered for graceful shutdown.")

    try:
        while is_running:
            try:
                kafka_msg, hetero_data = consumer.poll_snapshot(timeout=1.0)
                if hetero_data is None or kafka_msg is None:
                    continue
                engine.process_snapshot(
                    hetero_data=hetero_data,
                    kafka_msg=kafka_msg,
                    consumer=consumer,
                )
            except Exception as exc:
                logger.exception("Unexpected error in inference loop: %s", exc)
                continue
    finally:
        logger.info("Stopping service. Releasing external resources.")
        consumer.close()
        producer.flush()
        redis_client.close()
        logger.info("L4 inference service stopped safely.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
