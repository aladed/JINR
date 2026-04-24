"""Inference engine module for L4 service."""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, Tuple

import torch
from torch import Tensor
from torch_geometric.data import HeteroData

from l4_gnn_inference.models.gatv2_hetero import AttentionWeights, EdgeType, HeteroIncidentGATv2
from l4_gnn_inference.pipeline.consumer import GraphSnapshotConsumer
from l4_gnn_inference.pipeline.producer import IncidentProducer
from l4_gnn_inference.utils.thresholding import DynamicThreshold


class InferenceEngine:
    """Orchestrate model inference, thresholding, and incident publishing."""

    def __init__(
        self,
        model: HeteroIncidentGATv2,
        threshold_calc: DynamicThreshold,
        producer: IncidentProducer,
        output_topic: str,
        redis_client: Any,
    ) -> None:
        """Initialize inference engine dependencies.

        Args:
            model: Trained hetero GATv2 model.
            threshold_calc: Dynamic threshold calculator.
            producer: Kafka incident producer.
            output_topic: L5 output topic name.
            redis_client: Redis client for deduplication keys.
        """

        self.logger = logging.getLogger(__name__)
        self.model = model
        self.threshold_calc = threshold_calc
        self.producer = producer
        self.output_topic = output_topic
        self.redis_client = redis_client
        self.model.eval()

    def process_snapshot(
        self,
        hetero_data: HeteroData,
        kafka_msg: Any,
        consumer: GraphSnapshotConsumer,
    ) -> None:
        """Process one graph snapshot and commit offset on success.

        Args:
            hetero_data: Snapshot graph for inference.
            kafka_msg: Raw Kafka message object associated with snapshot.
            consumer: Consumer used for manual offset commit.
        """

        with torch.no_grad():
            anomaly_scores, attention_weights = self.model(
                hetero_data.x_dict,
                hetero_data.edge_index_dict,
            )

        node_types_to_scan: Iterable[str] = ("host", "vm", "job")
        max_node_type: str | None = None
        max_node_id: int = -1
        max_score: float = float("-inf")

        for node_type in node_types_to_scan:
            if node_type not in anomaly_scores:
                continue
            scores = anomaly_scores[node_type].reshape(-1)
            if scores.numel() == 0:
                continue

            local_max_val, local_max_idx = torch.max(scores, dim=0)
            local_score = float(local_max_val.item())
            if local_score > max_score:
                max_score = local_score
                max_node_type = node_type
                max_node_id = int(local_max_idx.item())

        if max_node_type is None:
            self.logger.warning("No candidate nodes found in snapshot; committing offset.")
            consumer.commit(kafka_msg)
            return

        self.threshold_calc.update(max_score)
        threshold = self.threshold_calc.get_threshold()

        if max_score > threshold:
            redis_key = f"incident:{max_node_type}:{max_node_id}"
            if self.redis_client.exists(redis_key):
                self.logger.info("Duplicate incident skipped for key '%s'.", redis_key)
            else:
                incident_payload: Dict[str, Any] = {
                    "root_cause_node_type": max_node_type,
                    "root_cause_node_id": max_node_id,
                    "anomaly_score": max_score,
                    "attention_weights": self._serialize_attention_weights(attention_weights),
                }
                self.producer.send_incident(self.output_topic, incident_payload)
                self.redis_client.setex(redis_key, 300, "1")
                self.logger.info("Incident emitted and cached with key '%s'.", redis_key)

        consumer.commit(kafka_msg)

    def _serialize_attention_weights(
        self,
        attention_weights: Dict[EdgeType, AttentionWeights],
    ) -> Dict[str, Dict[str, Any]]:
        """Convert tensor-based attention data to JSON-serializable objects.

        Args:
            attention_weights: Mapping of edge type to attention tensors.

        Returns:
            Dict[str, Dict[str, Any]]: Serializable attention dictionary.
        """

        serialized: Dict[str, Dict[str, Any]] = {}
        for edge_type, (edge_index, alpha) in attention_weights.items():
            edge_key = "__".join(edge_type)
            serialized[edge_key] = {
                "edge_index": self._tensor_to_python(edge_index),
                "alpha": self._tensor_to_python(alpha),
            }
        return serialized

    @staticmethod
    def _tensor_to_python(tensor: Tensor) -> Any:
        """Convert a tensor to a nested Python list.

        Args:
            tensor: Tensor to convert.

        Returns:
            Any: Python-native representation for JSON serialization.
        """

        return tensor.detach().cpu().tolist()
