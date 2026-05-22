"""CLI entry: read GNN inference, run remediation pipeline, print report."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from remediation.models import PRIORITY_LABELS
from remediation.pipeline import load_inference, playbook_to_dict, run_pipeline

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INFERENCE = ROOT / "artifacts" / "inference_sample.json"
DEFAULT_OUTPUT = ROOT / "artifacts" / "remediation_report.json"


def _priority_label(priority: int) -> str:
    return PRIORITY_LABELS.get(priority, "NORMAL")


def format_report(playbook, metadata: dict) -> str:
    lines = [
        "=== REMEDIATION REPORT ===",
        f"Incident: {playbook.incident_id}",
        f"Root Cause: {playbook.rc_node} (confidence: {playbook.confidence * 100:.2f}%)",
        f"Fault Type: {playbook.fault_type}",
        "",
        "Actions:",
    ]
    for i, action in enumerate(playbook.actions, start=1):
        lines.append(
            f"  {i}. [{_priority_label(action.priority)}] {action.action_id} -> {action.target_node}"
        )
        lines.append(f"     Parameters: {action.parameters}")
        lines.append(f"     Est. TTR: {action.estimated_ttr_seconds}s")
        lines.append("")

    total_ttr = sum(a.estimated_ttr_seconds for a in playbook.actions)
    lines.append(f"Explanation: {playbook.explanation}")
    lines.append("")
    lines.append(f"Firewall status: {metadata.get('firewall_status', 'UNKNOWN')}")
    if metadata.get("rule_based_fallback"):
        lines.append("LLM backend: rule_based_fallback (no API)")
    else:
        lines.append(f"LLM backend: {metadata.get('llm_backend', 'unknown')}")
    lines.append(f"Total estimated TTR: {total_ttr} seconds")
    lines.append("==========================")
    return "\n".join(lines)


def main() -> int:
    inference_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_INFERENCE
    output_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUTPUT

    if not inference_path.exists():
        print(
            f"ERROR: GNN inference file not found: {inference_path}\n"
            "Expected output from GNN pipeline at artifacts/inference_sample.json",
            file=sys.stderr,
        )
        return 1

    inference = load_inference(inference_path)
    playbook, metadata = run_pipeline(inference)

    report_text = format_report(playbook, metadata)
    print(report_text)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "playbook": playbook_to_dict(playbook),
        "metadata": metadata,
    }
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"\nSaved report to: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
