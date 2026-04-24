"""Core orchestration pipeline for L5 (RAG + LLM)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict

from l5_rag_llm.llm.generator import PlaybookGenerator
from l5_rag_llm.pipeline.consumer import AlertConsumer
from l5_rag_llm.pipeline.producer import PlaybookProducer
from l5_rag_llm.rag.metadata_client import MetadataRetriever


class L5Orchestrator:
    """Run end-to-end processing: Kafka -> RAG -> LLM -> Kafka."""

    def __init__(
        self,
        consumer: AlertConsumer,
        producer: PlaybookProducer,
        metadata_retriever: MetadataRetriever,
        llm_generator: PlaybookGenerator,
        output_topic: str,
        logger: Any,
    ) -> None:
        """Initialize orchestrator dependencies.

        Args:
            consumer: Kafka consumer for incoming alerts.
            producer: Kafka producer for outgoing playbooks.
            metadata_retriever: RAG metadata retriever with Redis cache.
            llm_generator: LLM playbook generation service.
            output_topic: Output Kafka topic for L6.
            logger: Structured logger instance.
        """

        self.consumer = consumer
        self.producer = producer
        self.metadata_retriever = metadata_retriever
        self.llm_generator = llm_generator
        self.output_topic = output_topic
        self.logger = logger

    def process_next_alert(self) -> None:
        """Process one alert message through RAG and LLM stages.

        Commit strategy:
            - Empty poll: no commit.
            - Broken/invalid alert payload: commit and skip.
            - Unexpected processing failure: do not commit to allow retry.
            - Successful processing: commit.
        """

        msg, alert_data = self.consumer.poll_alert()
        if msg is None or alert_data is None:
            return

        should_commit = False
        try:
            if not isinstance(alert_data, dict):
                self.logger.warning("Alert payload is not an object. Committing and skipping.")
                should_commit = True
                return

            required_keys = {
                "root_cause_node_type",
                "root_cause_node_id",
                "anomaly_score",
                "attention_weights",
            }
            if not required_keys.issubset(alert_data.keys()):
                self.logger.warning("Alert payload missing required keys. Committing and skipping.")
                should_commit = True
                return

            node_type = str(alert_data["root_cause_node_type"])
            node_id = int(alert_data["root_cause_node_id"])

            context = self.metadata_retriever.get_node_context(node_type=node_type, node_id=node_id)
            playbook_dsl = self.llm_generator.generate_playbook(alert_data, context)

            final_payload: Dict[str, Any] = dict(alert_data)
            # Use JSON mode to convert Enum values to plain strings.
            final_payload["playbook"] = playbook_dsl.model_dump(mode="json")
            final_payload["timestamp"] = datetime.now(timezone.utc).isoformat()
            self.logger.debug("Final L5 payload: %s", json.dumps(final_payload, ensure_ascii=True))

            self.producer.send_playbook(self.output_topic, final_payload)
            self.logger.info(
                "Playbook generated and sent for %s:%s",
                node_type,
                node_id,
            )
            should_commit = True
        except Exception as exc:
            self.logger.error("Failed to process alert in L5 orchestrator: %s", exc, exc_info=True)
        finally:
            if should_commit:
                self.consumer.commit(msg)
                self.logger.info("Committed Kafka offset for processed alert.")
