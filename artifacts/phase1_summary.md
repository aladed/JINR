# Phase 1 — Metric & Evaluation Repair: BEFORE / AFTER

**Checkpoint:** `checkpoints/baseline_model.pt` (epoch 27) — **weights UNCHANGED.**
Only the measurement code changed. This isolates the pure measurement artifact.

## Before / After (same weights)

| Metric | Phase 0 — broken instruments | Phase 1 — corrected (TEST split, leak-free) |
|---|---|---|
| ROC-AUC | 0.9350 | 0.9348 |
| F1 @ 0.5 threshold | 0.3176 | 0.2381 |
| F1 @ best threshold | not measured | 0.3824 (best t = 0.99 — sweep ceiling) |
| RCA Top-1 | 0.5000 (per-batch) | 0.4167 (per-graph, candidate-restricted) |
| RCA MRR | not measured | 0.5324 |
| RC rank histogram | not measured | 15×r1, 5×r2, long tail to rank 175 |

## How much was artifact

- **ROC-AUC: ~0 artifact.** 0.9350 → 0.9348. AUC was always trustworthy (F0 confirmed).
- **RCA: the broken metric was OPTIMISTIC.** Per-batch scoring reported 0.50; the true
  per-graph, leak-free RCA Top-1 is **0.4167**. The instrument over-stated by ~0.08.
- **F1: fixed-0.5 threshold is badly wrong.** Best threshold is ≥0.99 (hit the sweep
  ceiling) — the direct fingerprint of pos_weight=332 logit inflation (F2).

## The val/test gap is train leakage — do NOT use val for the baseline

- val split = perm[700:850]; positions 700–799 were in the baseline's OLD 80% training
  set. val RCA 0.83 / AUC 0.977 are **memorization-inflated** for this checkpoint.
- test split = perm[850:1000] — a strict subset of the baseline's old validation region,
  never trained on. **TEST is the only honest measurement of the baseline.**
- The 0.83 vs 0.42 gap is itself evidence of overfitting (consistent with the
  training_history val_loss climb 0.20 → 0.69).

## Key finding — the RC rank histogram (test, 36 faulted graphs)

```
rank 1 : 15 graphs (42%)
rank ≤2: 20 graphs (56%)
tail   : ranks 38, 39, 42, 123, 175 on individual graphs
```

The model is **bimodal**: it localizes the root cause well on ~half the graphs and
fails badly on the rest. This is a real localization weakness (consistent with F5 —
train/eval objective mismatch + overfitting), not pure measurement error. But the
instruments were genuinely wrong: the true picture is neither "RCA 0.50" nor a clean
success — it is 0.42 with a heavy failure tail.

## Validation criteria — PASS

- Unit tests: 4/4 pass (incl. batch-invariance regression guard).
- Sanity: test-split candidate-set AUC 0.9348 matches the original 0.9350.
- best-threshold F1 and corrected RCA produced for the same (Phase-0) weights.

## Implication for Phase 3 gate

Leak-free test numbers — **F1@best 0.38, RCA 0.42** — are both BELOW the Gate-A
targets (F1 ≥ 0.45, RCA ≥ 0.70). Phase 2 recalibration is required. The threshold
ceiling and the overfitting signal point squarely at F2 (pos_weight) and F4
(batch size / gradient variance).
