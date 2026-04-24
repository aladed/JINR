"""Kafka consumer for L4 alert ingestion in L5."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Tuple

from confluent_kafka import Consumer, KafkaException, Message


class AlertConsumer:
    """Consume L4 alerts from Kafka with manual offset commits."""

    def __init__(self, bootstrap_servers: str, topic: str, group_id: str) -> None:
        """Initialize Kafka consumer for L5 pipeline.

        Args:
            bootstrap_servers: Comma-separated list of Kafka brokers.
            topic: Input topic with alerts from L4.
            group_id: Kafka consumer group identifier.
        """

        self.logger = logging.getLogger(__name__)
        self.topic = topic
        self.consumer = Consumer(
            {
                "bootstrap.servers": bootstrap_servers,
                "group.id": group_id,
                "auto.offset.reset": "earliest",
                "enable.auto.commit": False,
            }
        )
        self.consumer.subscribe([self.topic])

    def poll_alert(self, timeout: float = 1.0) -> Tuple[Optional[Message], Optional[Dict[str, Any]]]:
        """Read and deserialize one alert message from Kafka.

        On malformed JSON payloads, the method logs an error, commits the
        problematic message offset to avoid infinite retries, and returns
        ``(None, None)``.

        Args:
            timeout: Poll timeout in seconds.

        Returns:
            Tuple[Optional[Message], Optional[Dict[str, Any]]]:
                Raw Kafka message and parsed alert dict, or ``(None, None)``.
        """

        msg = self.consumer.poll(timeout)
        if msg is None:
            return None, None

        if msg.error():
            self.logger.error("Kafka alert message error: %s", msg.error())
            return None, None

        try:
            raw_payload = msg.value()
            if raw_payload is None:
                self.logger.warning("Received empty alert payload. Committing offset.")
                self.consumer.commit(message=msg)
                return None, None
            alert_dict = json.loads(raw_payload.decode("utf-8"))
            return msg, alert_dict
        except json.JSONDecodeError as exc:
            self.logger.error("Invalid JSON payload from Kafka: %s", exc)
            self.consumer.commit(message=msg)
            return None, None

    def commit(self, message: Message) -> None:
        """Commit offset for a successfully processed alert.

        Args:
            message: Kafka message to commit.
        """

        try:
            self.consumer.commit(message=message)
        except KafkaException as exc:
            self.logger.exception("Kafka commit failed: %s", exc)
            raise

    def close(self) -> None:
        """Close Kafka consumer gracefully."""

        self.consumer.close()
