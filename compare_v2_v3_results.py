"""
Compare v3.0.0 results against v2.0.0+E1 baseline using the same
diagnostics methodology from Phase 1-5 (per-graph RCA scoring).
"""
import sys, os, json

import torch
from torch_geometric.loader import DataLoader
from training_pipeline.train import RCADataset
from training_pipeline.diagnostics import score_loader, typed_to_flat, full_report
from evaluate_pipeline import load_model

def compare():
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Device: {device}")
    print()

    # Load v3.0.0 best checkpoint
    ckpt_v3 = "checkpoints/best_model.pt"
    if not os.path.exists(ckpt_v3):
        print(f"ERROR: {ckpt_v3} not found. Training may not be complete.")
        return

    print("=" * 70)
    print("v3.0.0 best checkpoint evaluation (leak-free per-graph RCA)")
    print("=" * 70)

    try:
        model_v3, edge_types_v3, _ = load_model(ckpt_v3, device)
    except Exception as e:
        print(f"ERROR loading {ckpt_v3}: {e}")
        return

    # Score test set (indices 850-999 from 70/15/15 split)
    rng = torch.Generator()
    rng.manual_seed(42)
    perm = torch.randperm(1000, generator=rng).tolist()
    test_idx = perm[850:]

    print(f"Test set size: {len(test_idx)}")

    loader_v3 = DataLoader(RCADataset(test_idx), batch_size=16, shuffle=False)
    typed_v3 = score_loader(model_v3, loader_v3, edge_types_v3, device)
    flat_v3 = typed_to_flat(typed_v3)
    report_v3 = full_report(flat_v3)

    print()
    print("v3.0.0 RESULTS:")
    print(f"  AUC:       {report_v3['roc_auc']:.4f}")
    print(f"  F1@best:   {report_v3['f1_at_best_threshold']:.4f}  (threshold={report_v3.get('best_threshold', 'N/A')})")
    print(f"  RCA Top-1: {report_v3['rca']['top1']:.4f}")
    print(f"  RCA MRR:   {report_v3['rca']['mrr']:.4f}")
    print(f"  RCA Hits@1: {report_v3['rca']['hits@1']:.4f}")
    print(f"  RCA Hits@3: {report_v3['rca']['hits@3']:.4f}")

    print()
    print("v3.0.0 per-type breakdown:")
    for nt in ['hdd', 'switch', 'ram']:
        if nt in flat_v3:
            logits, labels = flat_v3[nt]
            rep = full_report([(logits, labels)])
            print(f"  {nt:8} RCA-Top1={rep['rca']['top1']:.4f}  AUC={rep['roc_auc']:.4f}  F1@best={rep['f1_at_best_threshold']:.4f}")

    # Baseline (v2.0.0+E1, from repair_report.md)
    baseline = {
        'auc': 0.9430,
        'f1_at_best': 0.3846,
        'rca_top1': 0.5278,
        'mrr': 0.5958,
    }

    print()
    print("=" * 70)
    print("COMPARISON: v2.0.0+E1 (baseline) vs v3.0.0")
    print("=" * 70)

    metrics = [
        ('AUC', 'auc', baseline['auc'], report_v3['roc_auc']),
        ('F1@best', 'f1_at_best', baseline['f1_at_best'], report_v3['f1_at_best_threshold']),
        ('RCA Top-1', 'rca_top1', baseline['rca_top1'], report_v3['rca']['top1']),
        ('RCA MRR', 'mrr', baseline['mrr'], report_v3['rca']['mrr']),
    ]

    print()
    print(f"{'Metric':<15} {'v2.0.0+E1':<12} {'v3.0.0':<12} {'Delta':<12} {'Delta%':<10}")
    print("-" * 60)
    for name, key, base, v3 in metrics:
        delta = v3 - base
        pct = (delta / base * 100) if base != 0 else 0
        symbol = "+" if delta > 0 else ""
        print(f"{name:<15} {base:<12.4f} {v3:<12.4f} {symbol}{delta:<11.4f} {symbol}{pct:<9.1f}%")

    print()
    print("=" * 70)
    print("INTERPRETATION")
    print("=" * 70)

    # Analyze changes
    gains = {
        'auc': report_v3['roc_auc'] - baseline['auc'],
        'f1': report_v3['f1_at_best_threshold'] - baseline['f1_at_best'],
        'rca': report_v3['rca']['top1'] - baseline['rca_top1'],
        'mrr': report_v3['rca']['mrr'] - baseline['mrr'],
    }

    print(f"\nRCA Top-1 change: {gains['rca']:+.4f} ({gains['rca']/baseline['rca_top1']*100:+.1f}%)")
    if gains['rca'] > 0.05:
        print("  [SIGNIFICANT] Multi-phase ram_leak and routing fix likely major factors.")
    elif gains['rca'] > 0:
        print("  [MODEST] Model adapting to corrected topology.")
    else:
        print("  [NO GAIN] Possible data/objective mismatch.")

    print(f"\nF1@best change: {gains['f1']:+.4f} ({gains['f1']/baseline['f1_at_best']*100:+.1f}%)")
    if gains['f1'] > 0.05:
        print("  [STRONG] Calibration improvement. Threshold dynamics shifted favorably.")
    elif gains['f1'] > 0:
        print("  [MODEST] Node-level ranking improving.")
    else:
        print("  [DEGRADED] Recall/precision trade-off issue.")

    print(f"\nAUC change: {gains['auc']:+.4f} ({gains['auc']/baseline['auc']*100:+.1f}%)")
    if abs(gains['auc']) < 0.01:
        print("  [STABLE] Fundamental discriminative power unchanged.")
    elif gains['auc'] > 0:
        print("  [IMPROVED] Better ranking. Fault propagation patterns more separable.")
    else:
        print("  [DEGRADED] Ranking regression detected.")

    print()
    print("Rank histogram (v3.0.0, test):")
    print(f"  {report_v3['rca']['rank_histogram']}")

    print()
    print("=" * 70)
    print("END COMPARISON")
    print("=" * 70)

if __name__ == "__main__":
    compare()
