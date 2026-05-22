"""Construct LLM prompts from GNN inference output and RAG context."""

from __future__ import annotations

import json
from typing import Any, Dict, List


ALLOWED_ACTIONS_STR = (
    "CHECK_METRICS, ISOLATE_NODE, RESTART_SERVICE, MIGRATE_JOB, "
    "NOTIFY_OPERATOR, SCHEDULE_MAINTENANCE, APPLY_QOS"
)


SYSTEM_PROMPT = f"""You are an HPC cluster remediation assistant.
Given GNN root-cause analysis and SOP excerpts, produce a remediation playbook as JSON only.

Allowed action_id values (use only these): {ALLOWED_ACTIONS_STR}
priority: 1=critical, 2=high, 3=normal
estimated_ttr_seconds: realistic seconds for each step

Output JSON schema:
{{
  "incident_id": "graph_<id>_<fault_type>",
  "fault_type": "<fault_type>",
  "rc_node": "<human readable RC node>",
  "confidence": <float 0-1>,
  "actions": [
    {{
      "action_id": "<ALLOWED>",
      "target_node": "<target>",
      "parameters": {{}},
      "priority": 1,
      "estimated_ttr_seconds": 30
    }}
  ],
  "explanation": "<brief summary>"
}}

Return ONLY valid JSON. No markdown. Order actions by priority (critical first).
"""


def format_rc_node(rc_node: Dict[str, Any]) -> str:
    node_type = rc_node.get("type", "unknown")
    node_id = rc_node.get("id", "unknown")
    if node_type == "switch":
        return f"Switch {node_id}"
    if node_type == "hdd":
        return f"HDD {node_id}"
    if node_type == "ram":
        host = rc_node.get("host_id", "?")
        return f"RAM node {node_id} (host {host})"
    return f"{node_type} {node_id}"


def build_incident_id(graph_id: int | str, fault_type: str) -> str:
    return f"graph_{graph_id}_{fault_type}"


def build_user_prompt(
    inference: Dict[str, Any],
    sop_contexts: List[str],
) -> str:
    """Build user message payload for LLM."""
    rc_node = inference.get("rc_node", {})
    victim_count = len(inference.get("victim_nodes", []))
    payload = {
        "graph_id": inference.get("graph_id"),
        "fault_type": inference.get("fault_type"),
        "rc_node": rc_node,
        "rc_logit": inference.get("rc_logit"),
        "confidence": inference.get("confidence"),
        "victim_node_count": victim_count,
        "top5_candidates": inference.get("top5_candidates", [])[:5],
        "sop_excerpts": sop_contexts,
    }
    return (
        "Generate a remediation playbook for this incident:\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=False)}"
    )


def build_messages(
    inference: Dict[str, Any],
    sop_contexts: List[str],
) -> List[Dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(inference, sop_contexts)},
    ]
