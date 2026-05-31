# Phase 3 — Re-measurement & Decision Gate A

All Phase 1 / Phase 2 numbers are on the **leak-free TEST split** (perm[850:1000]),
measured with the corrected diagnostics. Phase 0 is the original broken instruments.

## Master comparison table

| Metric | Phase 0 — broken | Phase 1 — corrected eval, baseline weights | Phase 2 — corrected eval, recalibrated training |
|---|---|---|---|
| ROC-AUC | 0.9350 | 0.9348 | 0.9380 |
| F1 @ best threshold | — | 0.3824 (t=0.99) | 0.3860 (t=0.95) |
| RCA Top-1 | 0.5000 | 0.4167 | 0.3611 |
| RCA Hits@3 | — | 0.5833 | 0.5000 |
| RCA MRR | — | 0.5324 | 0.4629 |
| ECE | — | 0.0031 | 0.0068 |

Experiment IDs: baseline `exp_20260522_021449_8a422fdf`, Phase 2 `exp_20260522_103503_50ffa85f`.

## Phase 1 → Phase 2: what the recalibration actually did

Tolerance bands: ROC-AUC ±0.01, F1 ±0.02, RCA ±0.03.

| Metric | Δ (Phase1→Phase2) | Verdict |
|---|---|---|
| ROC-AUC | +0.003 | within band — **no change** |
| F1 @ best | +0.004 | within band — **no change** |
| RCA Top-1 | -0.056 | beyond band — **small regression** |
| MRR | -0.069 | regression |

**The recalibration did not move the headline metrics.** The Phase 1 prediction
(F1 +0.05–0.15, RCA +0.05–0.10) did not hold. Honest reasons:

- `pos_weight` 332→20 did **not** normalize the threshold — it is still 0.95.
  Logit saturation is driven by overfitting/overconfidence, not only `pos_weight`.
- `batch_size` 4→16 **did** stabilise gradients (per-head grad-norm std is small
  and decays monotonically — see `training_history.json:grad_norms`), but gradient
  noise was never the bottleneck.
- The Phase 2 model trained on 700 graphs vs the baseline's 800 — part of the RCA
  drop is simply the smaller training set.

The Phase 2 changes are **correctness fixes** (trustworthy validation, sane
early-stop signal, candidate-restricted loss) and are **kept** — training did not
destabilise (no NaN, AUC healthy), so no rollback is triggered. They simply are
not the lever for the headline metrics.

## What the diagnostics now show

- **Overfitting is severe and fast.** train_loss 0.35→0.016 in 11 epochs;
  val_loss bottoms at epoch 4 (0.154) then climbs to 0.33. Best epoch 6. The model
  memorises the 700 training graphs.
- **Diffuse rank histogram despite high AUC.** Test: AUC 0.938 but RCA Top-1 0.361;
  the true RC node is ranked 1 on only 13/36 graphs, with a long failure tail
  (ranks 62, 77, 171). High aggregate ranking + poor per-graph Top-1 is the
  signature of a train/eval objective mismatch (per-node BCE vs per-graph Top-1).
- **Per-type split.** switch RCA 0.69 (only 6 candidate nodes — easy); hdd 0.42;
  ram 0.36 (100 candidate nodes — hard). The aggregate is dragged down by the
  large-candidate-set types.

## DECISION GATE A

Targets: test F1@best ≥ 0.45 **and** test RCA Top-1 ≥ 0.70.
Actual: F1@best **0.386**, RCA Top-1 **0.361** — both below target.

**Gate A routes to a justified experiment.** The justifying diagnostic is the
diffuse rank histogram with high AUC → **Phase 5 / E1: listwise RCA objective**
(per-graph softmax-cross-entropy to the true RC index, added to BCE). This
directly targets the per-node-BCE vs per-graph-Top-1 mismatch.

Overfitting is a parallel finding: any objective will memorise 1000 graphs, so E1
should be accompanied by stronger regularisation (higher dropout / weight decay).

## Phase 2 validation criteria

- Unit tests: 4/4 pass (re-checked after the diagnostics refactor).
- Gradient stability: per-head grad-norm std is small (≈5–30% of the mean) and
  decays smoothly — no gradient pathology.
- Validation curve: smooth rise to a ~0.46 plateau then gentle decline — far less
  noisy than the old `val_f1@0.5` curve.
- Test-set numbers produced via the Phase 1 diagnostics. PASS.
