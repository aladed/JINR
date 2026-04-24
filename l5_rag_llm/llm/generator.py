"""LLM client integration for playbook generation."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from openai import OpenAI
from pydantic import ValidationError

from l5_rag_llm.llm.firewall import ActionDSL, ActionType, SeverityLevel
from l5_rag_llm.llm.prompts import SYSTEM_PROMPT


class PlaybookGenerator:
    """Generate validated remediation playbooks using an LLM."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model_name: str,
        logger: logging.Logger,
        request_timeout_seconds: float = 20.0,
    ) -> None:
        """Initialize LLM client and generation settings.

        Args:
            api_key: API key for OpenAI-compatible endpoint.
            base_url: Base URL of OpenAI-compatible endpoint.
            model_name: Model identifier for chat completion.
            logger: Logger for debug and firewall events.
            request_timeout_seconds: Timeout per LLM API request.
        """

        self.logger = logger
        self.model_name = model_name
        self.request_timeout_seconds = request_timeout_seconds
        self.client = OpenAI(api_key=api_key, base_url=base_url)

    def generate_playbook(self, anomaly_data: Dict[str, Any], node_context: Dict[str, Any]) -> ActionDSL:
        """Generate and validate an action plan from anomaly context.

        The method requests a strict JSON response from LLM and validates it
        against ``ActionDSL``. If validation fails, it returns a safe fallback.

        Args:
            anomaly_data: Anomaly payload (score, node ids, attention weights).
            node_context: Retrieved metadata context for impacted node.

        Returns:
            ActionDSL: Validated playbook object safe for downstream execution.
        """

        user_payload = {
            "anomaly_data": anomaly_data,
            "node_context": node_context,
        }
        user_message = (
            "Проанализируй инцидент и верни JSON по схеме ActionDSL.\n"
            f"{json.dumps(user_payload, ensure_ascii=False)}"
        )

        try:
            completion = self.client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                response_format={"type": "json_object"},
                timeout=self.request_timeout_seconds,
            )

            raw_response = completion.choices[0].message.content or "{}"
            self.logger.info("Raw LLM response: %s", raw_response)
            return ActionDSL.model_validate_json(raw_response)
        except ValidationError as exc:
            self.logger.critical("Semantic Firewall Blocked Response: %s", exc, exc_info=True)
            return ActionDSL(
                summary="LLM response failed validation. Escalated to administrator.",
                severity=SeverityLevel.HIGH,
                action_type=ActionType.NOTIFY_ADMIN,
                target_entity=str(
                    anomaly_data.get("root_cause_node_type", "unknown")
                    + ":"
                    + str(anomaly_data.get("root_cause_node_id", "unknown"))
                ),
                cli_command="echo 'Manual intervention required: invalid LLM response'",
                reasoning=f"Validation error: {exc}",
            )
        except Exception as exc:
            self.logger.critical("Playbook generation failed: %s", exc, exc_info=True)
            return ActionDSL(
                summary="Playbook generation failed due to LLM API error.",
                severity=SeverityLevel.HIGH,
                action_type=ActionType.NOTIFY_ADMIN,
                target_entity=str(
                    anomaly_data.get("root_cause_node_type", "unknown")
                    + ":"
                    + str(anomaly_data.get("root_cause_node_id", "unknown"))
                ),
                cli_command="echo 'Manual intervention required: LLM unavailable'",
                reasoning=f"LLM request failure: {exc}",
            )
