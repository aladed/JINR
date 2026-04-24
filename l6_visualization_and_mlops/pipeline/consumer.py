"""Background Kafka consumer for ingesting L6 playbooks into SQLite."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from logging import Logger
from typing import Any

from confluent_kafka import Consumer, Message
from sqlalchemy.orm import Session, sessionmaker

from l6_visualization_and_mlops.db.models import IncidentRecord


class L6KafkaConsumer(threading.Thread):
    """Consume L6 topic messages and persist incidents into database."""

    def __init__(
        self,
        kafka_settings: dict[str, Any],
        topic: str,
        db_session_factory: sessionmaker[Session],
        logger: Logger,
    ) -> None:
        """Initialize thread state and Kafka consumer.

        Args:
            kafka_settings: Kafka configuration dictionary.
            topic: Kafka topic with finalized L5 payloads.
            db_session_factory: SQLAlchemy session factory.
            logger: Logger for runtime diagnostics.
        """

        super().__init__(name="l6-kafka-consumer", daemon=True)
        self.topic = topic
        self.db_session_factory = db_session_factory
        self.logger = logger
        self.is_running = True

        consumer_settings = dict(kafka_settings)
        consumer_settings["enable.auto.commit"] = False
        self.consumer = Consumer(consumer_settings)

    def run(self) -> None:
        """Start polling Kafka and writing incoming incidents to database."""

        self.logger.info("Starting L6KafkaConsumer thread.")
        self.consumer.subscribe([self.topic])

        try:
            while self.is_running:
                msg = self.consumer.poll(timeout=1.0)
                if msg is None:
                    continue
                if msg.error():
                    self.logger.error("Kafka consumer error: %s", msg.error())
                    continue

                self._process_message(msg)
        finally:
            self.consumer.close()
            self.logger.info("L6KafkaConsumer thread stopped.")

    def _process_message(self, msg: Message) -> None:
        """Parse and persist a single Kafka message.

        Args:
            msg: Kafka message with JSON payload.
        """

        try:
            payload = msg.value()
            if payload is None:
                self.logger.warning("Received empty Kafka payload, committing offset.")
                self.consumer.commit(message=msg)
                return
            alert_data = json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError as exc:
            self.logger.error("Invalid JSON in L6 consumer: %s", exc)
            self.consumer.commit(message=msg)
            return

        try:
            playbook = alert_data.get("playbook", {})
            timestamp_raw = alert_data.get("timestamp")
            timestamp = self._parse_timestamp(timestamp_raw)

            record = IncidentRecord(
                timestamp=timestamp,
                root_cause_node_type=str(alert_data.get("root_cause_node_type", "unknown")),
                root_cause_node_id=int(alert_data.get("root_cause_node_id", -1)),
                anomaly_score=float(alert_data.get("anomaly_score", 0.0)),
                playbook_summary=str(playbook.get("summary", "")),
                playbook_cli_command=str(playbook.get("cli_command", "")),
                attention_weights_json=json.dumps(alert_data.get("attention_weights", {}), ensure_ascii=True),
                is_confirmed_by_engineer=None,
            )

            with self.db_session_factory() as db:
                db.add(record)
                db.commit()

            self.consumer.commit(message=msg)
            self.logger.info("Incident persisted and Kafka offset committed.")
        except Exception as exc:
            self.logger.exception("Failed to process Kafka message: %s", exc)

    def stop(self) -> None:
        """Signal thread loop to stop gracefully."""

        self.logger.info("Stop signal received for L6KafkaConsumer thread.")
        self.is_running = False

    @staticmethod
    def _parse_timestamp(timestamp_raw: Any) -> datetime:
        """Parse ISO timestamp payload with safe fallback.

        Args:
            timestamp_raw: Raw timestamp from Kafka payload.

        Returns:
            datetime: Parsed datetime value in UTC or current UTC time.
        """

        if isinstance(timestamp_raw, str):
            try:
                return datetime.fromisoformat(timestamp_raw.replace("Z", "+00:00"))
            except ValueError:
                return datetime.now(timezone.utc)
        return datetime.now(timezone.utc)
