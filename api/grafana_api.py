"""
Grafana data API — serves inference + remediation artifacts for the
Infinity datasource plugin (yesoreyeram-infinity-datasource).

Endpoints:
  GET /health           → 200 OK
  GET /topology         → Node Graph format (nodes + edges)
  GET /scores           → GNN confidence / fault_type time-series
  GET /playbook         → Remediation actions table
  GET /summary          → Top-level incident stats (Stat panels)

Run:
  uvicorn api.grafana_api:app --host 0.0.0.0 --port 8080 --reload
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
ARTIFACTS = BASE_DIR / "artifacts"
MANIFEST  = BASE_DIR / "dataset" / "manifest"

INFERENCE_FILE   = ARTIFACTS / "inference_sample.json"
REMEDIATION_FILE = ARTIFACTS / "remediation_report.json"
TOPOLOGY_FILE    = MANIFEST  / "topology.json"

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="JINR RCA Grafana API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_json(path: Path) -> dict:
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{path.name} not found")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


# Anomaly score colour: green (ok) → yellow → red (fault)
def _score_color(confidence: float) -> str:
    if confidence >= 0.75:
        return "red"
    if confidence >= 0.45:
        return "orange"
    return "green"


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "timestamp": _now_ms()}


@app.get("/topology")
def topology() -> dict[str, Any]:
    """
    Returns Grafana Node Graph format:
      { "nodes": [...], "edges": [...] }

    Node fields expected by the Node Graph panel when using Infinity:
      id, title, subTitle, mainStat, secondaryStat, arc__fault, arc__ok,
      color (one of: green / orange / red)

    Edge fields:
      id, source, target, mainStat
    """
    topo      = _load_json(TOPOLOGY_FILE)
    inference = _load_json(INFERENCE_FILE) if INFERENCE_FILE.exists() else {}

    rc_node      = inference.get("rc_node", {})
    rc_type      = rc_node.get("type", "")
    rc_id        = rc_node.get("id", "")
    confidence   = float(inference.get("confidence", 0.0))
    fault_type   = inference.get("fault_type", "unknown")
    top5: list   = inference.get("top5_candidates", [])
    victims: list = inference.get("victim_nodes", [])

    victim_ids  = {v.get("id", "") for v in victims}
    top5_ids    = {c.get("id", "") for c in top5}
    top5_scores = {c.get("id", ""): c.get("score", 0.0) for c in top5}

    topo_cfg = topo.get("topology_config", {})
    n_hosts  = topo_cfg.get("NUM_HOSTS", 100)
    n_leaf   = topo_cfg.get("NUM_LEAF", 4)
    n_spine  = topo_cfg.get("NUM_SPINE", 2)

    # Build a representative set of nodes (not all 100 hosts — too many for viz)
    nodes: list[dict] = []
    edges: list[dict] = []

    # Spine switches
    for i in range(n_spine):
        nid = f"SPINE-{i+1}"
        score = top5_scores.get(nid, 0.0)
        is_rc = (nid == rc_id)
        nodes.append({
            "id":            nid,
            "title":         nid,
            "subTitle":      "spine switch",
            "mainStat":      f"{score:.2f}" if score else ("RC" if is_rc else "ok"),
            "secondaryStat": fault_type if is_rc else "",
            "arc__fault":    confidence if is_rc else (score if score else 0),
            "arc__ok":       1 - (confidence if is_rc else score),
            "color":         _score_color(confidence if is_rc else score),
        })

    # Leaf switches
    for i in range(n_leaf):
        nid = f"SWITCH-{i+1}"
        score = top5_scores.get(nid, 0.0)
        is_rc = (nid == rc_id)
        nodes.append({
            "id":            nid,
            "title":         nid,
            "subTitle":      "leaf switch",
            "mainStat":      f"{score:.2f}" if score else ("RC" if is_rc else "ok"),
            "secondaryStat": fault_type if is_rc else "",
            "arc__fault":    confidence if is_rc else score,
            "arc__ok":       1 - (confidence if is_rc else score),
            "color":         _score_color(confidence if is_rc else score),
        })

    # Sample of hosts (show victims + a few normal ones)
    shown_hosts: set[str] = set()
    for v in victims:
        shown_hosts.add(v.get("id", ""))
    for c in top5:
        shown_hosts.add(c.get("id", ""))
    # Add a few normal hosts for context
    for i in range(1, min(n_hosts + 1, 8)):
        shown_hosts.add(f"HOST-{i}")

    for hid in sorted(shown_hosts):
        score    = top5_scores.get(hid, 0.0)
        is_rc    = (hid == rc_id)
        is_vic   = hid in victim_ids
        subtitle = "victim" if is_vic else ("RC" if is_rc else "host")
        nodes.append({
            "id":            hid,
            "title":         hid,
            "subTitle":      subtitle,
            "mainStat":      f"{score:.2f}" if score else subtitle,
            "secondaryStat": fault_type if is_rc else "",
            "arc__fault":    confidence if is_rc else (score if score else (0.3 if is_vic else 0)),
            "arc__ok":       1 - (confidence if is_rc else (score if score else (0.3 if is_vic else 0))),
            "color":         "red" if is_rc else ("orange" if is_vic else "green"),
        })

    # Edges: leaf → spine uplinks
    for i in range(n_leaf):
        for j in range(n_spine):
            eid = f"e_sw{i+1}_sp{j+1}"
            edges.append({"id": eid, "source": f"SWITCH-{i+1}", "target": f"SPINE-{j+1}", "mainStat": "uplink"})

    # Edges: sample hosts → leaf (round-robin)
    host_list = sorted(shown_hosts)
    for idx, hid in enumerate(host_list):
        leaf = f"SWITCH-{(idx % n_leaf) + 1}"
        edges.append({"id": f"e_{hid}_{leaf}", "source": hid, "target": leaf, "mainStat": "access"})

    return {"nodes": nodes, "edges": edges}


@app.get("/scores")
def scores() -> dict[str, Any]:
    """
    Returns GNN prediction summary for Stat / Gauge panels.
    """
    inference = _load_json(INFERENCE_FILE)

    rc_node    = inference.get("rc_node", {})
    top5: list = inference.get("top5_candidates", [])

    return {
        "graph_id":       inference.get("graph_id", 0),
        "fault_type":     inference.get("fault_type", "unknown"),
        "confidence":     round(float(inference.get("confidence", 0.0)), 4),
        "rc_node_type":   rc_node.get("type", ""),
        "rc_node_id":     rc_node.get("id", ""),
        "victim_count":   len(inference.get("victim_nodes", [])),
        "top5":           top5,
        "timestamp_ms":   _now_ms(),
    }


@app.get("/playbook")
def playbook() -> list[dict[str, Any]]:
    """
    Returns remediation actions as a flat list (Table panel).
    """
    report = _load_json(REMEDIATION_FILE)

    raw_actions = (
        report.get("actions")
        or report.get("playbook", {}).get("actions")
        or []
    )

    rows = []
    for i, action in enumerate(raw_actions, start=1):
        rows.append({
            "step":        i,
            "action_id":   action.get("action_id", action.get("id", f"STEP_{i}")),
            "target":      action.get("target", action.get("component", "")),
            "description": action.get("description", action.get("action", "")),
            "priority":    action.get("priority", "medium"),
            "status":      action.get("status", "pending"),
        })

    return rows


@app.get("/summary")
def summary() -> dict[str, Any]:
    """
    High-level stats for the top Stat row: fault type, RC node,
    confidence, action count, firewall status.
    """
    inference  = _load_json(INFERENCE_FILE)
    report     = {}
    if REMEDIATION_FILE.exists():
        report = _load_json(REMEDIATION_FILE)

    raw_actions = (
        report.get("actions")
        or report.get("playbook", {}).get("actions")
        or []
    )

    firewall = report.get("firewall", {})
    fw_status = firewall.get("status", report.get("validation_status", "unknown"))

    return {
        "fault_type":     inference.get("fault_type", "unknown"),
        "rc_node":        inference.get("rc_node", {}).get("id", "unknown"),
        "confidence_pct": round(float(inference.get("confidence", 0.0)) * 100, 1),
        "action_count":   len(raw_actions),
        "firewall_status": fw_status,
        "timestamp_ms":   _now_ms(),
    }
