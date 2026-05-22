# RCA Pipeline Repair — Final Report

Branch `pipeline-repair`. Work executed as the phased plan in the implementation
prompt: verify metrics → recalibrate training → re-measure → conditional
data/architecture work.

## 1. Master before/after table (leak-free TEST split)

| Metric | P0 broken | P1 corrected eval, baseline weights | P2 recalibrated training | P5 / E1 listwise | P4 v2.1.0* |
|---|---|---|---|---|---|
| ROC-AUC | 0.9350 | 0.9348 | 0.9380 | **0.9430** | 0.9240 |
| F1 @ best threshold | — | 0.3824 | 0.3860 | 0.3846 | 0.4516 |
| RCA Top-1 | 0.5000 | 0.4167 | 0.3611 | **0.5278** | 0.4444 |
| RCA MRR | — | 0.5324 | 0.4629 | **0.5958** | 0.5375 |

P0–P5 are dataset **v2.0.0**. *P4 is dataset **v2.1.0** — a different dataset;
its column is **not** directly comparable to the others.

Registry experiment IDs: baseline `exp_20260522_021449_8a422fdf`,
P2 `exp_20260522_103503_50ffa85f`, E1 `exp_20260522_110625_62a2466b`,
P4 `exp_20260522_114014_1381e3a7`.

## 2. Per-phase: change, causal justification, measured effect

**Phase 0 — baseline capture.** Froze `baseline_model.pt`; confirmed training is
deterministic under seed 42. No code change.

**Phase 1 — metric & evaluation repair** (no retraining). Built `diagnostics.py`
(candidate-restricted, per-graph RCA / threshold sweep / calibration), a 3-way
70/15/15 split with a leak-free test set, and a batch-invariance regression test.
*Effect:* re-scoring the **unchanged** baseline showed RCA "0.50" was a per-batch
artifact (true per-graph value 0.4167) and F1 "0.32" was a fixed-0.5-threshold
artifact (best threshold ≈ 0.99). ROC-AUC 0.935 was always trustworthy.

**Phase 2 — training & loss recalibration.** pos_weight capped 332→20; loss
restricted to candidate types; batch_size 4→16; early-stop signal moved to
best-threshold F1; per-head gradient logging. *Effect:* **none beyond tolerance**
— F1 +0.004, AUC +0.003 (both within band), RCA −0.056. The training-config
knobs were not the lever. Changes kept as correctness fixes (no destabilisation).

**Phase 5 / E1 — listwise root-cause objective.** `L = BCE + 1.0·listwise`,
per-graph softmax-CE to the true RC node, reusing the BCE-head logits (no new
parameters). *Effect:* **the single biggest move** — RCA Top-1 +0.167
(0.36→0.53), MRR +0.133, AUC maintained. Confirmed the Phase-A diagnosis: the
bottleneck was the per-node-BCE vs per-graph-Top-1 objective mismatch.

**Phase 4 — ram_leak data fix** (conditional, Gate-B follow-up). Diagnostic
found `ram_leak` was the only fault whose root-cause node had no anomalous
neighbour (it propagated only to distant job nodes). Fix: `_resolve_fault_plan`
now propagates ram_leak to the host CPU (`cpu_system_percent`, 70% prob),
mirroring `hdd_degradation`. *Effect:* **negative result** — on v2.1.0, ram
RCA Top-1 stayed at 0.273 and ram-head AUC fell 0.90→0.86. The structural
asymmetry was real but is **not** the cause of the ram weakness. ram remains the
weakest type within v2.1.0 (RCA 0.27 vs hdd 0.50, switch 0.85).

## 3. RC rank histogram — before vs after (test, 36 faulted graphs)

| | rank 1 | rank ≤ 3 | worst rank |
|---|---|---|---|
| Baseline (P1, corrected eval) | 15 | 21 | 175 |
| E1 (P5) | 19 | 21 | 169 |

E1 pulled probability mass onto rank 1. A heavy failure tail persists — mostly
`ram_leak` graphs.

## 4. How much of the original failure was what

- **Measurement artifact (the largest share).** "F1 0.32" was a fixed-threshold
  artifact; "RCA 0.50" was a per-batch-aggregation artifact. The honest leak-free
  baseline was F1@best 0.38 / RCA 0.42, not 0.32 / 0.50. ROC-AUC was never wrong.
- **Training configuration: ~none.** Phase-2 recalibration moved nothing beyond
  the tolerance bands. Gradient noise, pos_weight and batch size were not the
  bottleneck.
- **Objective (architecture-adjacent): the one real lever.** The listwise loss
  lifted leak-free RCA Top-1 from 0.42 to 0.53 — a single, isolated change tied
  to a confirmed diagnosis.
- **Data: tested, not supported.** The ram structural-asymmetry hypothesis was
  falsified by Phase 4.

## 5. Final state vs targets

Gate-A targets (test F1@best ≥ 0.45 **and** RCA Top-1 ≥ 0.70): **not fully met.**
Best leak-free results: ROC-AUC ≈ 0.94, F1@best ≈ 0.38–0.45, RCA Top-1 ≈ 0.53
(v2.0.0 + E1). The model's ranking is strong (AUC 0.94); the residual gap is
per-graph Top-1 precision, dominated by `ram_leak` graphs.

## 6. Known limitations

- **`ram_leak` is intrinsically hard.** ram-head AUC ≈ 0.86 — the model
  struggles to extract the ram root-cause node's own-feature signal (a gradual
  saturation pattern). This is a feature-level / dataset property; adding
  topological corroboration (Phase 4) did not help. A slow memory leak being
  hard to pinpoint is also physically realistic.
- **Overfitting persists.** `val_loss` climbs every run after ~epoch 4–5 on 1000
  graphs. More data or stronger regularisation would be the next lever — not
  attempted, to respect "do not stack experiments".
- **Small per-type test samples** (ram n = 11). Per-type numbers are noisy;
  trust the direction, not the third decimal.
- **F1 is calibration-bound** — best threshold pinned near 0.99; the listwise
  ranking objective does not address node-level precision.

## 7. Process note

A Phase-4 slip: the first ram fix edited `_inject_ram_leak`, a parallel
implementation `generate_dataset()` does not use. It was caught immediately —
the retrain produced bit-identical metrics, which is impossible if the data had
changed — and corrected to the real path (`_resolve_fault_plan`). The
"compare two runs" discipline is what surfaced it.
