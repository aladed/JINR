"""Kafka producer for L5 playbook output."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from confluent_kafka import Producer


class PlaybookProducer:
    """Publish validated playbook payloads to Kafka."""

    def __init__(self, bootstrap_servers: str) -> None:
        """Initialize Kafka producer.

        Args:
            bootstrap_servers: Comma-separated list of Kafka brokers.
        """

        self.logger = logging.getLogger(__name__)
        self.producer = Producer({"bootstrap.servers": bootstrap_servers})

    def send_playbook(self, topic: str, data_dict: Dict[str, Any]) -> None:
        """Serialize and publish playbook payload to target topic.

        Args:
            topic: Output Kafka topic name.
            data_dict: Result payload including alert and generated playbook.
        """

        payload = json.dumps(data_dict, ensure_ascii=True)
        self.producer.produce(topic=topic, value=payload.encode("utf-8"))
        self.producer.poll(0)
        self.logger.info("Playbook payload sent to topic '%s'.", topic)

    def flush(self) -> None:
        """Flush pending produced messages."""

        self.producer.flush()
