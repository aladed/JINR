"""
compare_dataset_versions.py

Compare two dataset manifest directories and produce a human-readable diff report.

Usage:
    python compare_dataset_versions.py <manifest_dir_a> <manifest_dir_b> [--output report.txt]
    python compare_dataset_versions.py dataset/manifest dataset_v1/manifest --output diff.txt
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Load manifest helpers
# ---------------------------------------------------------------------------

def _load_json(path: str) -> Dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_manifest_raw(manifest_dir: str) -> Dict[str, Any]:
    """Load and merge all manifest files without checksum validation."""
    index_path = os.path.join(manifest_dir, "MANIFEST.json")
    if not os.path.exists(index_path):
        raise FileNotFoundError(f"MANIFEST.json not found in: {manifest_dir}")
    index = _load_json(index_path)
    merged: Dict[str, Any] = {"_index": index}
    for fname in index.get("files", {}).values():
        fpath = os.path.join(manifest_dir, fname)
        if os.path.exists(fpath):
            merged.update(_load_json(fpath))
    return merged


# ---------------------------------------------------------------------------
# Diff helpers
# ---------------------------------------------------------------------------

def _get(d: Dict, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, default)
    return d


def _fmt_diff(label: str, a, b) -> Optional[str]:
    if a == b:
        return None
    return f"  {label}: {a!r} → {b!r}"


def _section(title: str) -> str:
    bar = "=" * 72
    return f"\n{bar}\n{title}\n{bar}"


def _subsection(title: str) -> str:
    return f"\n{'─' * 60}\n{title}\n{'─' * 60}"


# ---------------------------------------------------------------------------
# Main comparison logic
# ---------------------------------------------------------------------------

def compare_manifests(dir_a: str, dir_b: str) -> str:
    lines: List[str] = []

    try:
        ma = _load_manifest_raw(dir_a)
    except Exception as e:
        return f"ERROR loading manifest A ({dir_a}): {e}"
    try:
        mb = _load_manifest_raw(dir_b)
    except Exception as e:
        return f"ERROR loading manifest B ({dir_b}): {e}"

    # Identity
    id_a = _get(ma, "dataset_identity", default={})
    id_b = _get(mb, "dataset_identity", default={})

    ver_a = id_a.get("semantic_version", "unknown")
    ver_b = id_b.get("semantic_version", "unknown")
    hash_a = id_a.get("dataset_hash", "")
    hash_b = id_b.get("dataset_hash", "")
    ts_a = id_a.get("generation_timestamp", "?")
    ts_b = id_b.get("generation_timestamp", "?")

    lines.append(_section("DATASET VERSION COMPARISON REPORT"))
    lines.append(f"\nA: {ver_a}  hash={hash_a[:16]}...  generated {ts_a}")
    lines.append(f"B: {ver_b}  hash={hash_b[:16]}...  generated {ts_b}")

    same_hash = (hash_a == hash_b)
    if same_hash:
        lines.append("\n✅  Hashes are IDENTICAL — datasets are bit-for-bit equivalent.")
        return "\n".join(lines)

    # Determine severity
    breaking: List[str] = []
    warnings: List[str] = []

    # --- Schema / temporal channels ---
    lines.append(_section("TEMPORAL FEATURES"))
    tc_a = list(_get(ma, "temporal_config", "temporal_channels", default=[]))
    tc_b = list(_get(mb, "temporal_config", "temporal_channels", default=[]))
    if tc_a != tc_b:
        lines.append(f"  [BREAKING] Temporal channels changed:")
        lines.append(f"    A: {tc_a}")
        lines.append(f"    B: {tc_b}")
        breaking.append("temporal_channels")
    else:
        lines.append(f"  Channels: {tc_a}  (unchanged)")

    for field in ["SIMULATION_STEPS", "FAULT_INJECTION_STEP", "EMA_ALPHA", "NOISE_SCALE"]:
        va = _get(ma, "temporal_config", field)
        vb = _get(mb, "temporal_config", field)
        d = _fmt_diff(field, va, vb)
        if d:
            lines.append(d)
            warnings.append(field)

    # --- Feature schema ---
    lines.append(_section("FEATURE SCHEMA"))
    fo_a = _get(ma, "feature_schema", "feature_ordering_hash", default="")
    fo_b = _get(mb, "feature_schema", "feature_ordering_hash", default="")
    if fo_a != fo_b:
        lines.append("  [BREAKING] Feature ordering hash changed:")
        lines.append(f"    A: {fo_a}")
        lines.append(f"    B: {fo_b}")
        breaking.append("feature_ordering")

    dims_a = _get(ma, "feature_schema", "feature_dim_total", default={})
    dims_b = _get(mb, "feature_schema", "feature_dim_total", default={})
    all_nts = sorted(set(list(dims_a.keys()) + list(dims_b.keys())))
    dim_changed = False
    for nt in all_nts:
        da, db = dims_a.get(nt, "?"), dims_b.get(nt, "?")
        if da != db:
            if not dim_changed:
                lines.append("\n  Feature dimension changes:")
                lines.append(f"  {'Node type':<14}  {'A':>6}  {'B':>6}  {'Δ':>6}")
                lines.append("  " + "─" * 36)
                dim_changed = True
            delta = (db - da) if (isinstance(da, int) and isinstance(db, int)) else "?"
            lines.append(f"  {nt:<14}  {da:>6}  {db:>6}  {delta:>+6}")
            breaking.append(f"dim_{nt}")
    if not dim_changed:
        lines.append("  Feature dimensions: unchanged")

    # --- Topology ---
    lines.append(_section("TOPOLOGY"))
    topo_fields = ["NUM_HOSTS", "NUM_LEAF", "NUM_SPINE"]
    topo_changed = False
    for f in topo_fields:
        va = _get(ma, "topology_config", f)
        vb = _get(mb, "topology_config", f)
        d = _fmt_diff(f, va, vb)
        if d:
            lines.append(f"  [BREAKING]{d}")
            breaking.append(f"topology_{f}")
            topo_changed = True
    if not topo_changed:
        lines.append("  Topology: unchanged")

    topo_schema_a = _get(ma, "topology_config", "topology_schema_version", default="?")
    topo_schema_b = _get(mb, "topology_config", "topology_schema_version", default="?")
    d = _fmt_diff("topology_schema_version", topo_schema_a, topo_schema_b)
    if d:
        lines.append(d)

    # --- Fault generation ---
    lines.append(_section("FAULT GENERATION"))
    fault_fields = [
        ("fault_types",      "fault_generation", "fault_types"),
        ("propagation_algo", "fault_generation", "propagation_algo"),
        ("severity_min",     "fault_generation", "severity_min"),
        ("severity_max",     "fault_generation", "severity_max"),
    ]
    fault_changed = False
    for label, *keys in fault_fields:
        va = _get(ma, *keys)
        vb = _get(mb, *keys)
        d = _fmt_diff(label, va, vb)
        if d:
            lines.append(d)
            fault_changed = True
            if label in ("fault_types", "propagation_algo"):
                breaking.append(f"fault_{label}")
            else:
                warnings.append(f"fault_{label}")
    if not fault_changed:
        lines.append("  Fault generation: unchanged")

    # --- Dataset structure ---
    lines.append(_section("DATASET STRUCTURE"))
    struct_a = _get(ma, "dataset_structure", default={})
    struct_b = _get(mb, "dataset_structure", default={})
    for f in ["total_graphs", "healthy_graphs", "faulted_graphs", "healthy_ratio"]:
        d = _fmt_diff(f, struct_a.get(f), struct_b.get(f))
        if d:
            lines.append(d)

    # --- Lineage ---
    lines.append(_section("VERSION LINEAGE"))
    lin_a = _get(ma, "version_lineage", default={})
    lin_b = _get(mb, "version_lineage", default={})
    lines.append(f"  A  changelog: {lin_a.get('changelog', '?')}")
    lines.append(f"  B  changelog: {lin_b.get('changelog', '?')}")
    bc_b = lin_b.get("breaking_changes", [])
    if bc_b:
        lines.append("\n  B declared breaking changes:")
        for bc in bc_b:
            lines.append(f"    ⚠️  {bc}")

    # --- Compatibility summary ---
    lines.append(_section("COMPATIBILITY SUMMARY"))

    has_breaking = len(breaking) > 0
    lines.append(f"\n  Breaking changes detected: {'YES ⚠️' if has_breaking else 'NO ✅'}")

    if has_breaking:
        lines.append(f"\n  Breaking fields: {', '.join(breaking)}")
        lines.append("\n  Checkpoint compatibility:")
        lines.append("    ❌  Models trained on A CANNOT be loaded with B dataset")
        lines.append("    ❌  Feature dimensions or ordering differ → retrain required")
    else:
        lines.append("\n  ✅  No breaking changes detected")
        lines.append("  ⚠️  Weights may still differ slightly due to distribution shift")
        if warnings:
            lines.append(f"  Changed parameters: {', '.join(warnings)}")

    # --- Predicted metric shift ---
    lines.append(_section("PREDICTED METRIC SHIFT (HEURISTIC)"))
    shifts: List[Tuple[str, str]] = []

    if "temporal_channels" in breaking:
        shifts.append(("ROC-AUC", "incompatible (retrain required)"))
        shifts.append(("RCA Top-1", "incompatible (retrain required)"))

    sev_a_min = _get(ma, "fault_generation", "severity_min", default=0.15)
    sev_b_min = _get(mb, "fault_generation", "severity_min", default=0.15)
    sev_a_max = _get(ma, "fault_generation", "severity_max", default=0.45)
    sev_b_max = _get(mb, "fault_generation", "severity_max", default=0.45)
    if sev_a_min != sev_b_min or sev_a_max != sev_b_max:
        if float(sev_b_max or 0) < float(sev_a_max or 0):
            shifts.append(("ROC-AUC", "expected drop ~0.03–0.05 (lower severity = harder task)"))
        else:
            shifts.append(("ROC-AUC", "expected gain ~0.02–0.04 (higher severity = easier task)"))

    prop_a = _get(ma, "fault_generation", "propagation_algo", default="")
    prop_b = _get(mb, "fault_generation", "propagation_algo", default="")
    if prop_a != prop_b and "bfs" in str(prop_b):
        shifts.append(("RCA Top-1", "topology becomes essential; old models may drop ~0.08–0.12"))

    if shifts:
        lines.append(f"\n  {'Metric':<20}  Predicted effect")
        lines.append(f"  {'─'*20}  {'─'*45}")
        for metric, effect in shifts:
            lines.append(f"  {metric:<20}  {effect}")
    else:
        lines.append("\n  No significant metric shift predicted.")
    lines.append(
        "\n  Note: All predictions are heuristic. Actual shifts require retraining and evaluation."
    )
    lines.append("  Tolerance bands: ROC-AUC ±0.01, F1 ±0.02, RCA ±0.03 (CUDA nondeterminism).")

    # --- Recommendations ---
    lines.append(_section("RECOMMENDATIONS"))
    if has_breaking:
        lines.append("  1. ✅  Treat B as a new MAJOR version")
        lines.append("  2. ✅  Retrain all models on B from scratch")
        lines.append("  3. ⚠️   Do not compare A and B metrics directly")
        lines.append("  4. 📝  Document: 'metrics from A not comparable to B due to schema changes'")
        lines.append("  5. 🔒  Archive A dataset — do not regenerate or modify")
    else:
        lines.append("  1. ✅  Treat B as MINOR or PATCH version")
        lines.append("  2. ⚠️   Retrain if fault distribution changed significantly")
        lines.append("  3. ✅  Old checkpoints may work — run compatibility check first")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Compare two dataset manifest directories")
    parser.add_argument("manifest_a", help="Path to first manifest directory")
    parser.add_argument("manifest_b", help="Path to second manifest directory")
    parser.add_argument("--output", "-o", default=None, help="Write report to file (default: stdout)")
    args = parser.parse_args()

    report = compare_manifests(args.manifest_a, args.manifest_b)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"Report written to: {args.output}")
    else:
        print(report)


if __name__ == "__main__":
    main()
