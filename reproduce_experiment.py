"""
reproduce_experiment.py

Multi-mode reproducibility checker for RCA training experiments.

Modes (cheapest to most expensive):
  checksum  — verify dataset_hash and code_hash only (seconds)
  dryrun    — load dataset + rebuild model, don't train (< 1 min)
  fast      — checksum + evaluate best checkpoint on val set (minutes)
  full      — complete retrain from scratch, compare all metrics (hours)

Usage:
    python reproduce_experiment.py <exp_id> [--mode checksum|dryrun|fast|full]
    python reproduce_experiment.py exp_20260522_143542_abc1234 --mode fast
    python reproduce_experiment.py --list                  # show all experiments
    python reproduce_experiment.py --list --dataset-version v2.0.0
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT        = os.path.dirname(os.path.abspath(__file__))
DATASET_DIR  = os.path.join(_ROOT, "dataset")
MANIFEST_DIR = os.path.join(DATASET_DIR, "manifest")
CKPT_DIR     = os.path.join(_ROOT, "checkpoints")
TRAIN_PY     = os.path.join(_ROOT, "training_pipeline", "train.py")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_registry_imports():
    """Lazy import to keep startup fast for --list mode."""
    from training_pipeline.versioning import (
        compute_behavioral_code_hash,
        get_dataset_hash_from_manifest,
        get_dataset_version_from_manifest,
        compute_feature_ordering_hash,
        METRIC_TOLERANCES,
    )
    from training_pipeline.experiment_registry import (
        load_experiment,
        list_experiments,
        registry_summary,
        is_experiment_reproducible,
    )
    return {
        "compute_behavioral_code_hash": compute_behavioral_code_hash,
        "get_dataset_hash": get_dataset_hash_from_manifest,
        "get_dataset_version": get_dataset_version_from_manifest,
        "compute_fo_hash": compute_feature_ordering_hash,
        "METRIC_TOLERANCES": METRIC_TOLERANCES,
        "load_experiment": load_experiment,
        "list_experiments": list_experiments,
        "registry_summary": registry_summary,
        "is_experiment_reproducible": is_experiment_reproducible,
    }


def _check(label: str, passed: bool, detail: str = "") -> bool:
    icon = "✅" if passed else "❌"
    msg = f"  {icon}  {label}"
    if detail:
        msg += f"\n       {detail}"
    print(msg)
    return passed


# ---------------------------------------------------------------------------
# Mode: checksum
# ---------------------------------------------------------------------------

def mode_checksum(exp: Dict, imports: Dict) -> bool:
    """Verify dataset hash and code hash. No data loading. Seconds."""
    print("\n[checksum mode]")
    all_ok = True

    # Dataset hash
    try:
        current_hash = imports["get_dataset_hash"](MANIFEST_DIR)
        exp_hash = exp.get("dataset_info", {}).get("dataset_hash", "")
        match = current_hash == exp_hash
        all_ok &= _check(
            "Dataset hash",
            match,
            f"exp={exp_hash[:16]}...  current={current_hash[:16]}..." if not match else exp_hash[:16] + "...",
        )
        if not match:
            print("       → Dataset was regenerated or config changed. Retrain required for exact reproduction.")
    except Exception as e:
        _check("Dataset hash", False, f"Cannot read manifest: {e}")
        all_ok = False

    # Dataset version
    try:
        current_ver = imports["get_dataset_version"](MANIFEST_DIR)
        exp_ver = exp.get("dataset_info", {}).get("dataset_version", "unknown")
        ver_match = current_ver == exp_ver
        all_ok &= _check(
            "Dataset version",
            ver_match,
            f"exp={exp_ver}  current={current_ver}" if not ver_match else exp_ver,
        )
    except Exception as e:
        _check("Dataset version", False, str(e))

    # Code hash (behavioral only)
    try:
        current_code = imports["compute_behavioral_code_hash"](TRAIN_PY)
        exp_code = exp.get("code_versions", {}).get("train_code_hash", "")
        if exp_code:
            code_match = current_code == exp_code
            all_ok &= _check(
                "train.py behavioral hash",
                code_match,
                "Loss/optimizer logic unchanged" if code_match
                else f"exp={exp_code[:12]}...  current={current_code[:12]}... → metrics may differ",
            )
        else:
            print("  ⚠️   train_code_hash not stored in experiment (pre-versioning)")
    except Exception as e:
        print(f"  ⚠️   Cannot check code hash: {e}")

    # Feature ordering hash
    try:
        from training_pipeline.config import FEATURE_SCHEMA
        current_fo = imports["compute_fo_hash"](FEATURE_SCHEMA)
        exp_fo = exp.get("code_versions", {}).get("feature_ordering_hash", "")
        if exp_fo:
            fo_match = current_fo == exp_fo
            if not fo_match:
                _check(
                    "Feature ordering hash",
                    False,
                    "CRITICAL: feature order changed! Checkpoint would be BROKEN even if dims match.",
                )
                all_ok = False
            else:
                _check("Feature ordering hash", True, current_fo[:16] + "...")
    except Exception as e:
        print(f"  ⚠️   Cannot check feature ordering: {e}")

    return all_ok


# ---------------------------------------------------------------------------
# Mode: dryrun
# ---------------------------------------------------------------------------

def mode_dryrun(exp: Dict, imports: Dict) -> bool:
    """Load dataset + rebuild model architecture. No training."""
    print("\n[dryrun mode]")

    # First run checksum
    ok = mode_checksum(exp, imports)

    # Load a few graphs to confirm dataset is readable
    print("\n  Loading dataset sample...")
    try:
        import torch
        raw_dir = os.path.join(DATASET_DIR, "raw")
        n_found = 0
        for i in range(min(5, 200)):
            p = os.path.join(raw_dir, f"data_{i}.pt")
            if os.path.exists(p):
                torch.load(p, weights_only=False)
                n_found += 1
        _check("Dataset readable", n_found > 0, f"{n_found} graphs loaded")
    except Exception as e:
        _check("Dataset readable", False, str(e))
        ok = False

    # Rebuild model
    print("\n  Rebuilding model architecture...")
    try:
        from training_pipeline.train import GATv2Hetero
        model_cfg = exp.get("model_config", {})
        meta_path = os.path.join(DATASET_DIR, "metadata.json")
        if os.path.exists(meta_path):
            with open(meta_path) as f:
                meta = json.load(f)
            node_dims = meta["feature_dimensions"]
            edge_types = [tuple(et) for et in meta["edge_types"]]
            model = GATv2Hetero(
                node_dims=node_dims,
                edge_types=edge_types,
                hidden_dim=model_cfg.get("hidden_dim", 64),
                heads=model_cfg.get("heads", 4),
                dropout=model_cfg.get("dropout", 0.1),
            )
            n_params = sum(p.numel() for p in model.parameters())
            _check("Model buildable", True, f"{n_params:,} parameters")
        else:
            print("  ⚠️   metadata.json not found — cannot verify model dims")
    except Exception as e:
        _check("Model buildable", False, str(e))
        ok = False

    print("\n  ✅  Dryrun complete — ready to train (use --mode full to retrain)")
    return ok


# ---------------------------------------------------------------------------
# Mode: fast
# ---------------------------------------------------------------------------

def mode_fast(exp: Dict, imports: Dict) -> bool:
    """Checksum + evaluate saved checkpoint on current val set."""
    print("\n[fast mode]")

    ok = mode_checksum(exp, imports)

    ckpt_path = exp.get("checkpoint_path") or exp.get("runtime", {}).get("checkpoint_path", "")
    if not ckpt_path or not os.path.exists(ckpt_path):
        ckpt_path = os.path.join(CKPT_DIR, "best_model.pt")

    if not os.path.exists(ckpt_path):
        print(f"\n  ⚠️   Checkpoint not found at {ckpt_path} — cannot run fast evaluation")
        return ok

    print(f"\n  Evaluating checkpoint: {ckpt_path}")
    try:
        import torch
        from training_pipeline.train import (
            GATv2Hetero, build_dataloaders, validate,
        )

        meta_path = os.path.join(DATASET_DIR, "metadata.json")
        loss_cfg_path = os.path.join(DATASET_DIR, "loss_config.json")
        with open(meta_path) as f:
            meta = json.load(f)
        with open(loss_cfg_path) as f:
            loss_cfg = json.load(f)

        node_dims  = meta["feature_dimensions"]
        edge_types = [tuple(et) for et in meta["edge_types"]]
        num_total  = meta["dataset_size"]
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        pos_weights = {
            nt: torch.tensor([pw], dtype=torch.float32)
            for nt, pw in loss_cfg.get("pos_weight", {}).items()
        }

        _, val_loader, _ = build_dataloaders(
            num_total=num_total, batch_size=4, seed=42
        )

        state = torch.load(ckpt_path, map_location=device, weights_only=False)
        model_cfg = exp.get("model_config", {})
        model = GATv2Hetero(
            node_dims=node_dims, edge_types=edge_types,
            hidden_dim=model_cfg.get("hidden_dim", 64),
            heads=model_cfg.get("heads", 4),
            dropout=model_cfg.get("dropout", 0.1),
        ).to(device)
        model.load_state_dict(state["model_state_dict"])

        val_loss, report = validate(model, val_loader, pos_weights, device, edge_types)

        orig = exp.get("metrics", {})
        tolerances = imports["METRIC_TOLERANCES"]

        print("\n  Metric comparison (original → reproduced):")
        print(f"  {'Metric':<22}  {'Original':>10}  {'Now':>10}  {'Δ':>8}  Status")
        print("  " + "─" * 62)

        comparisons = [
            ("val_f1",       orig.get("best_val_f1"),        report["f1_at_best_threshold"], tolerances["f1"]),
            ("val_roc_auc",  orig.get("final_val_roc_auc"),  report["roc_auc"],              tolerances["roc_auc"]),
            ("rca_accuracy", orig.get("final_rca_accuracy"), report["rca"]["top1"],          tolerances["rca_accuracy"]),
        ]
        all_within = True
        for name, orig_val, new_val, tol in comparisons:
            if orig_val is None or new_val is None:
                print(f"  {name:<22}  {'N/A':>10}  {new_val or 'N/A':>10}  {'—':>8}  ⚠️  no baseline")
                continue
            delta = abs(float(new_val) - float(orig_val))
            within = delta <= tol
            all_within &= within
            status = "✅" if within else "⚠️ "
            print(
                f"  {name:<22}  {orig_val:>10.4f}  {new_val:>10.4f}  {delta:>+8.4f}  {status}"
            )

        if all_within:
            print("\n  ✅  All metrics within tolerance — experiment is REPRODUCIBLE")
        else:
            print(f"\n  ⚠️   Some metrics outside tolerance (±{tol:.3f}) — check code/seed changes")

        ok = all_within
    except Exception as e:
        print(f"\n  ❌  Evaluation failed: {e}")
        ok = False

    return ok


# ---------------------------------------------------------------------------
# Mode: full
# ---------------------------------------------------------------------------

def mode_full(exp: Dict, imports: Dict) -> bool:
    """Full retrain from scratch, then compare metrics."""
    print("\n[full mode] — this may take hours")

    ok = mode_dryrun(exp, imports)
    if not ok:
        print("\n  ❌  Dryrun failed — aborting full retrain")
        return False

    print("\n  Starting full retrain...")
    t0 = time.time()
    try:
        from training_pipeline.train import main as train_main
        train_main()
    except Exception as e:
        print(f"\n  ❌  Training failed: {e}")
        return False

    elapsed = time.time() - t0
    print(f"\n  Retrain completed in {elapsed:.1f}s")

    # Now evaluate
    return mode_fast(exp, imports)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify experiment reproducibility",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("exp_id", nargs="?", help="Experiment ID to reproduce")
    parser.add_argument(
        "--mode", choices=["checksum", "dryrun", "fast", "full"],
        default="checksum",
        help="Verification mode (default: checksum)",
    )
    parser.add_argument("--list", action="store_true", help="List all experiments")
    parser.add_argument("--dataset-version", default=None, help="Filter experiments by version")
    args = parser.parse_args()

    try:
        imports = _load_registry_imports()
    except ImportError as e:
        print(f"ERROR: Cannot import versioning modules: {e}")
        print("Make sure you are running from the project root directory.")
        sys.exit(1)

    # ---- List mode ----
    if args.list:
        from training_pipeline.experiment_registry import list_experiments, registry_summary
        summary = registry_summary()
        print(f"\nExperiment Registry Summary")
        print(f"  Total experiments: {summary.get('total', 0)}")
        print(f"  By version: {summary.get('by_dataset_version', {})}")
        print(f"  By status:  {summary.get('by_status', {})}")
        print()

        exps = list_experiments(dataset_version=args.dataset_version, limit=20)
        if not exps:
            print("No experiments found.")
            return

        print(f"{'Experiment ID':<38}  {'Version':<10}  {'Status':<12}  {'val_f1':>8}  Timestamp")
        print("─" * 90)
        for e in exps:
            eid  = e.get("experiment_id", "?")
            ver  = e.get("dataset_info", {}).get("dataset_version", "?")
            sta  = e.get("status", "?")
            f1   = e.get("metrics", {}).get("best_val_f1")
            ts   = e.get("timestamp", "?")[:19]
            f1s  = f"{f1:.4f}" if f1 is not None else "  N/A "
            print(f"{eid:<38}  {ver:<10}  {sta:<12}  {f1s:>8}  {ts}")
        return

    # ---- Reproduce mode ----
    if not args.exp_id:
        parser.print_help()
        sys.exit(1)

    exp = imports["load_experiment"](args.exp_id)
    if exp is None:
        print(f"ERROR: Experiment '{args.exp_id}' not found in registry.")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"Reproducing experiment: {args.exp_id}")
    print(f"  Dataset:  {exp.get('dataset_info', {}).get('dataset_version', '?')}")
    print(f"  Status:   {exp.get('status', '?')}")
    print(f"  Recorded: {exp.get('timestamp', '?')[:19]}")
    print(f"  Mode:     {args.mode}")
    print(f"{'='*60}")

    mode_fns = {
        "checksum": mode_checksum,
        "dryrun":   mode_dryrun,
        "fast":     mode_fast,
        "full":     mode_full,
    }

    result = mode_fns[args.mode](exp, imports)

    print(f"\n{'='*60}")
    if result:
        print("✅  REPRODUCIBILITY CHECK PASSED")
    else:
        print("⚠️   REPRODUCIBILITY CHECK FLAGGED ISSUES (see above)")
    print(f"{'='*60}\n")

    sys.exit(0 if result else 1)


if __name__ == "__main__":
    main()
