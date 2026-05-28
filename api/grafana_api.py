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
    Returns Grafana Node Graph format: { "nodes": [...], "edges": [...] }

    Node IDs use "{type}-{model_index}" to match inference_sample.json exactly.
    Switches: model indices 0-3 = leaf, 4-5 = spine (NUM_LEAF=4, NUM_SPINE=2).
    Edges built from the same static routing as snapshot_engine/topology.py:
      host_to_leaf[h] = h % NUM_LEAF  (build_routing_maps seed=None, round-robin)
      every leaf → both spines (uplinks)
    """
    topo_manifest = _load_json(TOPOLOGY_FILE)
    inference     = _load_json(INFERENCE_FILE) if INFERENCE_FILE.exists() else {}

    rc_node    = inference.get("rc_node", {})
    rc_type    = rc_node.get("type", "")
    rc_id_num  = rc_node.get("id", -1)          # integer model index
    rc_node_id = f"{rc_type}-{rc_id_num}"        # canonical string: "hdd-82"
    confidence = float(inference.get("confidence", 0.0))
    fault_type = inference.get("fault_type", "unknown")
    top5: list = inference.get("top5_candidates", [])

    # top5_scores: {"hdd-82": 0.9999, "switch-2": 0.03, ...}
    top5_scores: dict[str, float] = {c.get("id", ""): float(c.get("score", 0.0)) for c in top5}

    topo_cfg = topo_manifest.get("topology_config", {})
    n_leaf   = int(topo_cfg.get("NUM_LEAF",  4))
    n_spine  = int(topo_cfg.get("NUM_SPINE", 2))
    n_sw     = n_leaf + n_spine        # leaf indices: 0..n_leaf-1, spine: n_leaf..n_sw-1

    # Static routing: identical to build_routing_maps(seed=None) in dataset_generator
    # host_to_leaf[h] = h % n_leaf
    def host_leaf(h: int) -> int:
        return h % n_leaf

    nodes: list[dict] = []
    edges: list[dict] = []
    seen_node_ids: set[str] = set()

    def _add_node(nid: str, title: str, subtitle: str, score: float, is_rc: bool) -> None:
        if nid in seen_node_ids:
            return
        seen_node_ids.add(nid)
        fault_arc = confidence if is_rc else score
        nodes.append({
            "id":            nid,
            "title":         title,
            "subTitle":      subtitle,
            "mainStat":      "RC" if is_rc else (f"{score:.3f}" if score > 0.001 else "ok"),
            "secondaryStat": fault_type if is_rc else "",
            "arc__fault":    round(fault_arc, 4),
            "arc__ok":       round(max(0.0, 1.0 - fault_arc), 4),
            "color":         "red" if is_rc else _score_color(score),
        })

    # ── 1. All switches (leaf + spine) — always shown ─────────────────────────
    for sw_i in range(n_sw):
        nid      = f"switch-{sw_i}"
        is_rc    = (rc_type == "switch" and rc_id_num == sw_i)
        score    = top5_scores.get(nid, 0.0)
        role     = "leaf switch" if sw_i < n_leaf else "spine switch"
        _add_node(nid, nid, role, score, is_rc)

    # ── 2. Host-component nodes: RC + top-5 candidates + context hosts ─────────
    # Collect host indices from top5 (e.g. "hdd-35" → host 35)
    interesting_hosts: set[int] = set()
    for cid in top5_scores:
        parts = cid.rsplit("-", 1)
        if len(parts) == 2 and parts[0] in ("cpu", "gpu", "ram", "hdd", "job"):
            try:
                interesting_hosts.add(int(parts[1]))
            except ValueError:
                pass

    # Always include RC host
    if rc_type in ("cpu", "gpu", "ram", "hdd", "job"):
        interesting_hosts.add(int(rc_id_num))

    # A few context hosts (indices 0–3) so the graph is never empty
    for h in range(min(4, 100)):
        interesting_hosts.add(h)

    # Cap at 12 host nodes for readability
    for host_idx in sorted(interesting_hosts)[:12]:
        # Pick the most informative component type for this host index:
        # prefer whichever type is in top5, fall back to rc_type, then "cpu"
        best_score = 0.0
        best_nid   = f"cpu-{host_idx}"
        is_rc_host = False

        for ctype in ("hdd", "ram", "cpu", "gpu", "job"):
            candidate = f"{ctype}-{host_idx}"
            s = top5_scores.get(candidate, 0.0)
            if s > best_score:
                best_score = s
                best_nid   = candidate
            if rc_type == ctype and rc_id_num == host_idx:
                is_rc_host = True
                best_nid   = rc_node_id
                best_score = confidence

        subtitle = "RC" if is_rc_host else ("top-5" if best_score > 0.001 else "host")
        _add_node(best_nid, best_nid, subtitle, best_score, is_rc_host)

        # Edge: host-component → its leaf switch (real round-robin from topology)
        leaf_sw_idx = host_leaf(host_idx)
        leaf_nid    = f"switch-{leaf_sw_idx}"
        edges.append({
            "id":       f"e_{best_nid}_sw{leaf_sw_idx}",
            "source":   best_nid,
            "target":   leaf_nid,
            "mainStat": "access",
        })

    # ── 3. Leaf → Spine uplinks (from build_edge_indices logic) ───────────────
    for leaf_i in range(n_leaf):
        for spine_i in range(n_leaf, n_sw):
            edges.append({
                "id":       f"e_sw{leaf_i}_sw{spine_i}",
                "source":   f"switch-{leaf_i}",
                "target":   f"switch-{spine_i}",
                "mainStat": "uplink",
            })

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
