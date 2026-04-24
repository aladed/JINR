"""Kafka consumer module for L4 inference."""

from __future__ import annotations

import logging
import json
from typing import Any, Optional, Tuple

from confluent_kafka import Consumer, KafkaException, Message
import torch
from torch_geometric.data import HeteroData


class GraphSnapshotConsumer:
    """Consume serialized graph snapshots from Kafka."""

    def __init__(self, bootstrap_servers: str, topic: str, group_id: str) -> None:
        """Initialize Kafka consumer with manual offset commits.

        Args:
            bootstrap_servers: Comma-separated Kafka broker list.
            topic: Input topic name for L3 graph snapshots.
            group_id: Consumer group identifier.
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

    def poll_snapshot(self, timeout: float = 1.0) -> Tuple[Optional[Message], Optional[HeteroData]]:
        """Poll one message and deserialize it into ``HeteroData``.

        Args:
            timeout: Kafka polling timeout in seconds.

        Returns:
            Tuple[Optional[Message], Optional[HeteroData]]: Raw Kafka message and
            parsed graph snapshot. Returns ``(None, None)`` for empty polls,
            Kafka errors, or deserialization failures.
        """

        message = self.consumer.poll(timeout)
        if message is None:
            return None, None

        if message.error():
            self.logger.error("Kafka message error: %s", message.error())
            return None, None

        try:
            payload = message.value()
            if payload is None:
                self.logger.warning("Received empty payload from Kafka.")
                return None, None
            snapshot_dict = json.loads(payload.decode("utf-8"))
            hetero_data = self._build_heterodata(snapshot_dict)
            return message, hetero_data
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            self.logger.exception("Failed to deserialize graph snapshot: %s", exc)
            return None, None

    def commit(self, message: Message) -> None:
        """Manually commit Kafka offset for a processed message.

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

    @staticmethod
    def _build_heterodata(snapshot_dict: dict[str, Any]) -> HeteroData:
        """Construct ``HeteroData`` from JSON-safe snapshot dictionary.

        Expected schema:
            {
              "x_dict": {"node_type": [[...], ...]},
              "edge_index_dict": {"src__rel__dst": [[...], [...]]}
            }

        Args:
            snapshot_dict: Parsed JSON payload from Kafka.

        Returns:
            HeteroData: Reconstructed heterogeneous graph object.
        """

        if "x_dict" not in snapshot_dict or "edge_index_dict" not in snapshot_dict:
            raise ValueError("Snapshot payload must include x_dict and edge_index_dict.")

        data = HeteroData()
        x_dict = snapshot_dict["x_dict"]
        edge_index_dict = snapshot_dict["edge_index_dict"]

        for node_type, features in x_dict.items():
            data[node_type].x = torch.tensor(features, dtype=torch.float32)

        for edge_key, edge_index in edge_index_dict.items():
            parts = edge_key.split("__")
            if len(parts) != 3:
                raise ValueError(f"Invalid edge key format: {edge_key}")
            src_type, relation, dst_type = parts
            data[(src_type, relation, dst_type)].edge_index = torch.tensor(
                edge_index,
                dtype=torch.long,
            )

        return data
