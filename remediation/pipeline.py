"""End-to-end remediation pipeline orchestration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from llm.llm_client import LLMClient, generate_rule_based_playbook
from llm.prompt_builder import (
    build_incident_id,
    build_messages,
    format_rc_node,
)
from llm.response_parser import normalize_playbook_dict
from rag.retriever import SOPRetriever
from remediation.firewall import build_fallback_playbook, validate_playbook
from remediation.models import RemediationPlaybook


def load_inference(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def build_retrieval_query(inference: Dict[str, Any]) -> str:
    fault = inference.get("fault_type", "")
    rc = format_rc_node(inference.get("rc_node", {}))
    victims = len(inference.get("victim_nodes", []))
    return f"{fault} root cause {rc} affected nodes {victims}"


def run_pipeline(
    inference: Dict[str, Any],
    *,
    force_rule_based: bool = False,
    llm_client: Optional[LLMClient] = None,
) -> Tuple[RemediationPlaybook, Dict[str, Any]]:
    """Execute full RAG + LLM + firewall flow.

    Returns:
        (playbook, metadata) where metadata includes firewall_status, backend, etc.
    """
    fault_type = inference.get("fault_type", "unknown")
    graph_id = inference.get("graph_id", 0)
    confidence = float(inference.get("confidence", 0.0))
    rc_label = format_rc_node(inference.get("rc_node", {}))
    incident_id = build_incident_id(graph_id, fault_type)

    retriever = SOPRetriever()
    query = build_retrieval_query(inference)
    sop_texts = retriever.retrieve_texts(query, fault_type=fault_type)

    messages = build_messages(inference, sop_texts)
    client = llm_client or LLMClient(force_rule_based=force_rule_based)
    raw_playbook, backend = client.generate(messages, inference)

    defaults = {
        "incident_id": incident_id,
        "fault_type": fault_type,
        "rc_node": rc_label,
        "confidence": confidence,
        "actions": [],
        "explanation": "",
    }
    normalized = normalize_playbook_dict(raw_playbook, defaults)

    playbook, passed, err = validate_playbook(normalized)
    firewall_status = "PASSED" if passed else "BLOCKED"

    if not passed:
        playbook = build_fallback_playbook(
            incident_id=incident_id,
            fault_type=fault_type,
            rc_node=rc_label,
            confidence=confidence,
            reason=err or "validation failed",
        )

    metadata = {
        "firewall_status": firewall_status,
        "firewall_error": err,
        "llm_backend": backend,
        "rule_based_fallback": backend == "rule_based_fallback"
        or raw_playbook.get("rule_based_fallback", False),
        "sop_chunks_retrieved": len(sop_texts),
    }
    return playbook, metadata


def playbook_to_dict(playbook: RemediationPlaybook) -> Dict[str, Any]:
    return playbook.model_dump()
