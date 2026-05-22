# ram Diagnostic — why ram_leak localization is weak

## The numbers (small samples — read with care)

Per-type RCA Top-1 (E1 model):

| Fault type | val | test | faulted graphs (val/test) |
|---|---|---|---|
| switch (network_congestion) | 0.722 | 0.923 | 18 / 13 |
| hdd (hdd_degradation) | 0.556 | 0.667 | 18 / 12 |
| ram (ram_leak) | 0.438 | 0.273 | 16 / **11** |

ram is genuinely the weakest type, but the headline "0.273" is noise-deflated:
only 11 ram-fault test graphs (3/11). Pooled val+test ≈ 10/27 ≈ **0.37** is the
fairer estimate. ram head AUC is also lowest (0.904 vs hdd 0.932, switch 0.946).

## Root cause — a dataset-design asymmetry (code-confirmed)

Comparing the three fault injectors in `dataset_generator.py`:

| Fault | RC node | Propagates to (victim_node_ids) | RC node's 1-hop neighbour anomalous? |
|---|---|---|---|
| hdd_degradation (`:1265`) | hdd | **cpu** + job | YES — attached cpu gets iowait anomaly |
| network_congestion (`:1359`) | switch | **cpu** + **gpu** + job | YES — connected cpu/gpu anomalous |
| ram_leak (`:1479`) | ram | **job only** | **NO** — attached cpu is left clean |

`_inject_ram_leak` sets `victim_node_ids = {"job": victim_jobs}` — it perturbs
the RC ram node's own features and then jumps straight to distant job nodes. The
cpu the ram is `attached_to` is **never** anomalised.

**Consequence for the GNN.** hdd and switch root-cause nodes sit inside an
anomalous neighbourhood that 2-layer message passing can exploit — the fault is
*structurally corroborated*. The ram root-cause node sits in a **clean
neighbourhood**; the model must identify it from its own 3 perturbed features
alone, competing against 99 healthy ram nodes plus 4 injected decoy spikes
(`_break_argmax_shortcut`). The topology — the GNN's main asset — carries no
signal for ram_leak.

This matches the metrics exactly: ram head AUC 0.90 (the model *does* extract the
RC node's own-feature signal) but RCA Top-1 ≈ 0.37 (own-feature signal alone is
not enough to win rank-1 against decoys, especially at low severity).

Secondary factor: `amp = severity * 0.50` for ram vs `* 0.55` for hdd; at low
severity (amp ≈ 0.075) the RC primary-feature anomaly is comparable to the decoy
spike scale (0.10).

## Verdict

The ram weakness is a **dataset-design asymmetry, not a model bug or a pipeline
defect.** No model-side experiment (temporal positional encoding, deeper message
passing, attention changes) can manufacture structural signal that the data does
not contain — they would not specifically help ram.

## Options

1. **Phase 4 data fix (justified).** Give `ram_leak` a cpu-layer propagation —
   physically correct (a RAM leak stresses the host CPU via swap / page-fault
   handling). This makes it structurally consistent with the other two faults.
   Cost: dataset regeneration → version bump v2.0.0 → v2.1.0 → re-run Phases
   1/2/3/5 on the new data. Old vs new metrics not directly comparable.
2. **Document the ceiling and consolidate.** ram is 1 of 3 fault types; E1
   already delivered the main win (RCA 0.36→0.53). Accept ram_leak as the
   intrinsically hard case and record it as a known limitation.

A model-side experiment for ram is **not** justified by this diagnostic.
