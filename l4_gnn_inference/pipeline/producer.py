"""Kafka producer module for L4 inference."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from confluent_kafka import Producer


class IncidentProducer:
    """Produce incident alerts to Kafka."""

    def __init__(self, bootstrap_servers: str) -> None:
        """Initialize Kafka producer.

        Args:
            bootstrap_servers: Comma-separated Kafka broker list.
        """

        self.logger = logging.getLogger(__name__)
        self.producer = Producer({"bootstrap.servers": bootstrap_servers})

    def send_incident(self, topic: str, incident_dict: Dict[str, Any]) -> None:
        """Serialize incident payload and publish it to Kafka.

        Args:
            topic: Output topic name.
            incident_dict: Alert payload to send.
        """

        payload = json.dumps(incident_dict, ensure_ascii=True)
        self.producer.produce(topic=topic, value=payload.encode("utf-8"))
        self.producer.poll(0)
        self.logger.info("Incident sent to topic '%s'.", topic)

    def flush(self) -> None:
        """Flush pending Kafka messages."""

        self.producer.flush()
