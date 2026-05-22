# Phase 5 / E1 — Listwise Root-Cause Objective

Experiment `exp_20260522_110625_62a2466b`. One isolated change: added
`L = BCE + 1.0 * listwise` (per-graph softmax-CE to the true RC node,
reusing the existing BCE-head logits — no new parameters).

## Result — E1 vs Phase-2 baseline (leak-free TEST split)

| Metric | Phase 2 baseline | Phase 5 / E1 | Δ | Verdict |
|---|---|---|---|---|
| ROC-AUC | 0.9380 | 0.9430 | +0.005 | maintained (±0.01) |
| F1 @ best threshold | 0.3860 | 0.3846 | -0.001 | no change |
| RCA Top-1 | 0.3611 | **0.5278** | **+0.167** | large gain |
| RCA Hits@3 | 0.5000 | 0.5833 | +0.083 | gain |
| RCA MRR | 0.4629 | **0.5958** | **+0.133** | large gain |
| ECE | 0.0068 | 0.0104 | +0.004 | negligible |

**Success criterion (RCA Top-1 > +0.03 OR MRR beyond tolerance, AUC within
±0.01): PASSED decisively** — RCA +0.167, MRR +0.133, AUC +0.005.

E1 is **kept**. It is the largest single metric move in the whole project
and directly confirms the Phase-A diagnosis: the bottleneck was the
train/eval objective mismatch (per-node BCE vs per-graph Top-1).

## Master table — all phases (leak-free TEST split)

| Metric | P0 broken | P1 baseline | P2 recalibrated | P5 / E1 | Gate-A target |
|---|---|---|---|---|---|
| ROC-AUC | 0.9350 | 0.9348 | 0.9380 | 0.9430 | — |
| F1 @ best | — | 0.3824 | 0.3860 | 0.3846 | ≥ 0.45 |
| RCA Top-1 | 0.5000 | 0.4167 | 0.3611 | **0.5278** | ≥ 0.70 |
| RCA MRR | — | 0.5324 | 0.4629 | **0.5958** | — |

## Rank histogram concentrated

Test, 36 faulted graphs: rank 1 went from 13/36 (Phase 2) to **19/36** (E1).
The objective change pulled probability mass onto the true RC node.

## Per-type breakdown (test) — ram is the remaining bottleneck

| Type | RCA Top-1 | note |
|---|---|---|
| switch | 0.923 | near-solved (6 candidates) |
| hdd | 0.667 | strong gain (was 0.417) |
| ram | 0.273 | **regressed** (was 0.364); ram_leak is the weak fault |

## Decision Gate B

Gate-A targets re-checked: F1@best 0.385 (< 0.45) and RCA Top-1 0.528
(< 0.70) — **both still unmet**. E1 closed most of the RCA gap but not all.

Remaining, evidence-backed gaps:
- **Overfitting persists** — val_loss climbs 0.19 → 0.36; best epoch 5.
- **ram localization is weak** — RCA 0.273, AUC 0.904 (lowest type). The
  `ram_leak` fault signature is the hardest; needs a diagnostic before any
  further experiment.
- **F1 is calibration-bound** — best threshold pinned at 0.99; the listwise
  ranking objective does not address node-level precision/recall.
