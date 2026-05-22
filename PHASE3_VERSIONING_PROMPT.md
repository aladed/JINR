# PHASE 3: RESEARCH-GRADE DATASET VERSIONING & EXPERIMENT TRACEABILITY

**Goal:** Implement immutable dataset identity, lineage tracking, reproducibility guarantee, and experiment traceability. Make every reported metric auditable to exact dataset version, generator code, and training config.

**Why this matters:**
- Regenerated datasets silently differ → metrics become incomparable
- Leakage fixes cannot be audited → scientific validity compromised
- Checkpoints disconnect from data provenance → reproducibility broken
- Code changes (e.g., train.py) invalidate old experiments → experiments become "orphaned"
- Ablation results have no provenance → meaningless to compare

---

## ⚠️ REPRODUCIBILITY GUARANTEE (Realistic Scope)

**This system provides:**
- ✅ **Deterministic dataset generation**: exact reproducibility (same seed → same data always)
- ✅ **Traceable experiments**: dataset_hash + code_hash pair uniquely identifies every run
- ✅ **Auditable lineage**: manifest + registry enable full reconstruction of methodology
- ⚠️ **Approximate training reproducibility**: ~±0.01 ROC-AUC, ±0.02 F1, ±0.03 RCA due to CUDA/PyTorch nondeterminism

**This system DOES NOT guarantee:**
- ❌ Identical metrics across runs (CUDA scatter ops, multiprocessing workers, AMP cause variance)
- ❌ Perfect bit-for-bit reproducibility (use CUBLAS_WORKSPACE_CONFIG, single-GPU, deterministic mode, but ±0.01 still expected)
- ❌ Protection against all human errors (manifest can be edited manually; only cryptographic signatures prevent tampering)

**Tolerance bands for comparison:**
| Metric | Tolerance | Reason |
|--------|-----------|--------|
| ROC-AUC | ±0.01 | CUDA non-determinism in scoring |
| F1 | ±0.02 | Threshold selection variance |
| RCA Top-1 | ±0.03 | Argmax on softly-trained model |
| Loss | ±0.005 | Batch ordering, numerical precision |

---

## PART 1 — DATASET VERSION IDENTITY

Every generated dataset receives:

### 1.1 Semantic Version (SemVer 2.0)
Format: `MAJOR.MINOR.PATCH` (e.g., `v1.0.0`, `v2.1.3`)

Increment rules:
- **MAJOR**: breaking schema changes
  - Temporal tensor layout changes (e.g., `[N,F,3]` → `[N,F,4]`)
  - Topology format changes (host-leaf mapping restructured)
  - Node feature redesign (e.g., new propagation rules that change amplitude distribution)
  - Feature schema changes (e.g., new feature added to FEATURE_SCHEMA)
- **MINOR**: new generation behaviors (non-breaking)
  - Better propagation algorithm (e.g., TruncExp delays added)
  - New noise model
  - New fault family added
  - Hardening improvement (e.g., soft victim weighting introduced)
- **PATCH**: bug fixes (behavior correction)
  - Incorrect clipping or clamping
  - Leakage fix
  - Seed bug
  - Off-by-one error

### 1.2 Semantic Dataset Fingerprint (Resilient Hash)

Computed from **semantic generation config only** (not raw file text):

```python
# Extract ONLY semantic parameters (normalized, whitespace-independent)
fingerprint_inputs = {
    "topology_config": {
        "NUM_HOSTS": NUM_HOSTS,
        "NUM_LEAF": NUM_LEAF,
        "NUM_SPINE": NUM_SPINE,
        "node_counts": NODE_COUNTS,
    },
    "feature_schema_version": "v2.0.0",  # Hash of FEATURE_SCHEMA content semantics
    "feature_ordering": sorted(FEATURE_SCHEMA.keys()),  # Explicit feature name ordering
    "temporal_config": {
        "SIMULATION_STEPS": SIMULATION_STEPS,
        "FAULT_INJECTION_STEP": FAULT_INJECTION_STEP,
        "EMA_ALPHA": EMA_ALPHA,
        "NOISE_SCALE": round(_NOISE_SCALE, 6),  # Normalize floats
        "DRIFT_AMP": round(_DRIFT_AMP, 6),
        "DRIFT_PERIOD": _DRIFT_PERIOD,
        "temporal_channels": ["value", "delta_short", "delta_long", "rolling_var"],
    },
    "fault_generation": {
        "fault_types": sorted(FAULT_TYPES),
        "severity_range_min": round(0.15, 2),
        "severity_range_max": round(0.45, 2),
        "propagation_algorithm": "phase2_bfs_temporal_activation",
        "max_hops": 3,
        "hop_delay_lambda": 0.15,
        "hop_delay_max": 10.0,
        "cross_contamination_enabled": True,
        "cross_contamination_prob_range": [0.15, 0.25],
    },
    "healthy_spike_config": {
        "enabled": True,
        "bernoulli_prob": 0.04,
        "spike_scale_range": [0.05, 0.15],
    },
    "random_seeds": {
        "master_seed": 42,
        "scaler_fit_seed": 42,
        "seed_derivation_method": "deterministic_child_seed_from_master",
    },
}

# Canonical JSON (sorted keys, consistent float precision)
canonical_json = json.dumps(fingerprint_inputs, sort_keys=True, indent=None)
# Normalize whitespace
canonical_json = " ".join(canonical_json.split())
dataset_hash = sha256(canonical_json.encode()).hexdigest()
```

**This hash is resilient to:**
- ✅ Whitespace changes in generator code
- ✅ Comment additions/removals
- ✅ Docstring changes
- ✅ Code refactoring (as long as semantics unchanged)

**This hash WILL change on:**
- ❌ NUM_HOSTS or topology change
- ❌ FEATURE_SCHEMA content change
- ❌ Temporal channel structure change
- ❌ Fault generation logic change
- ❌ Seed change

### 1.3 Generation Timestamp
ISO 8601 UTC: `2026-05-22T14:35:42Z`

### 1.4 Human-Readable Codename
Examples:
- `"temporal_overlap_hardened"` (Phase-2 with temporal activation delays)
- `"phase2_causal_propagation"` (multi-hop BFS distances)
- `"anti_shortcut_v3"` (shortcut elimination iteration 3)
- `"rollvar_temporal_v4"` (rolling variance channel added)

Used in logs, plots, papers for human readability.

---

## PART 2 — MANIFEST SCHEMA (Strict Versioning, Modular)

### 2.0 Schema Versioning

Every manifest includes:
```json
{
  "manifest_schema_version": "v2.0",
  "manifest_schema_fields": [
    "dataset_identity",
    "generation_config",
    "dataset_structure",
    "temporal_config",
    "feature_schema",
    "fault_generation",
    "healthy_graph_injection",
    "normalization",
    "reproducibility",
    "expected_benchmark_ranges",
    "diagnostics_summary",
    "known_limitations",
    "version_lineage"
  ]
}
```

When adding new fields:
- `manifest_schema_version` MUST increment
- Missing fields in old manifests should be handled gracefully (not crash)
- Schema migrations documented

### 2.1 Manifest File Organization (Modular)

Instead of one giant `dataset_manifest.json`, split into:

```
dataset_v2.0.0/
├── identity.json              # dataset_identity + manifest_schema_version
├── generation_config.json     # generation_config + reproducibility
├── topology.json              # dataset_structure (node/edge topology)
├── temporal_features.json     # temporal_config + feature_schema + feature ordering
├── fault_generation.json      # fault_generation + propagation rules
├── diagnostics.json           # diagnostics_summary + leakage checks + quality score
├── lineage.json               # version_lineage + changelog + breaking_changes
└── MANIFEST.json              # Index: pointers to all above files + manifest_schema_version
```

**Each file is:**
- Independently loadable
- Independently validatable
- Small enough for code review
- Maintainable (not god object)

### 2.2 MANIFEST.json (Index)

```json
{
  "manifest_schema_version": "v2.0",
  "manifest_format": "modular",
  "files": {
    "identity": "identity.json",
    "generation_config": "generation_config.json",
    "topology": "topology.json",
    "temporal_features": "temporal_features.json",
    "fault_generation": "fault_generation.json",
    "diagnostics": "diagnostics.json",
    "lineage": "lineage.json"
  },
  "checksums": {
    "identity.json": "sha256:abc123...",
    "generation_config.json": "sha256:def456...",
    "..."
  }
}
```

For every dataset generation, write separate JSON files:

**identity.json:**

```json
{
  "manifest_schema_version": "v2.0",
  "dataset_identity": {
    "semantic_version": "v2.0.0",
    "dataset_hash": "a7f3e9d2c...",
    "codename": "temporal_overlap_hardened",
    "generation_timestamp": "2026-05-22T14:35:42Z"
  },
  "generation_config": {
    "generator_version": "dataset_generator.py@sha256:a7f3e9d2c...",
    "generator_code_hash": "a7f3e9d2c...",
    "config_hash": "f2b8c9e3a...",
    "git_commit": "abc1234def5678",
    "python_version": "3.10.5"
  },
  "dataset_structure": {
    "total_graphs": 5000,
    "healthy_graphs": 2500,
    "faulted_graphs": 2500,
    "train_val_test_split": [0.70, 0.15, 0.15],
    "node_types": ["cpu", "gpu", "ram", "hdd", "switch", "job", "rca_context"],
    "edge_types": [
      ["cpu", "connected_to", "switch"],
      ["switch", "uplink_to", "switch"],
      ["job", "executes_on", "cpu"],
      "..."
    ],
    "node_counts": {
      "cpu": 100,
      "gpu": 100,
      "ram": 100,
      "hdd": 100,
      "switch": 6,
      "job": 500,
      "rca_context": 1
    }
  },
  "temporal_config": {
    "simulation_steps": 90,
    "fault_injection_step": 50,
    "ema_alpha": 0.0645,
    "ema_window_ticks": 30,
    "temporal_channels": ["value", "delta_short", "delta_long", "rolling_var"],
    "rolling_var_window": 5,
    "noise_scale": 0.012,
    "drift_amplitude": 0.015,
    "drift_period_steps": 45
  },
  "feature_schema": {
    "continuous_suffixes": ["_percent", "_bytes", "_ps", "..."],
    "categorical_suffixes": ["_encoded", "_flag", "_status"],
    "total_features_per_type": {
      "cpu": 8,
      "gpu": 12,
      "ram": 8,
      "hdd": 8,
      "switch": 10,
      "job": 11,
      "link_edge": 19
    },
    "feature_dim_continuous_expanded": {
      "comment": "[N, F*4] where 4 = [value, delta_short, delta_long, rolling_var]",
      "cpu": 32,
      "gpu": 48,
      "ram": 32,
      "hdd": 32,
      "switch": 40,
      "job": 44
    },
    "feature_dim_total_with_categorical": {
      "cpu": 34,
      "gpu": 50,
      "ram": 34,
      "hdd": 34,
      "switch": 40,
      "job": 46
    }
  },
  "fault_generation": {
    "fault_types": ["hdd_degradation", "network_congestion", "ram_leak"],
    "severity_range": [0.15, 0.45],
    "rc_selection": "uniform random per fault type",
    "propagation_algorithm": "phase2_bfs_temporal_activation",
    "propagation_details": {
      "max_hops": 3,
      "hop_delay_distribution": "TruncExp(lambda=0.15, max=10.0)",
      "victim_selection_mode": "stochastic per distance",
      "victim_probability_function": "P(victim) ∝ exp(-distance/tau), tau≈1.5",
      "max_victims_per_rc": 12,
      "attenuation_threshold": 0.1
    },
    "cross_type_contamination": {
      "enabled": true,
      "probability_range": [0.15, 0.25],
      "example": "disk latency bleeds onto ram_fragmentation"
    },
    "victim_weighting": {
      "algorithm": "soft_weighting",
      "rc_weight": 1.0,
      "healthy_weight": 1.0,
      "victim_weight_range": [0.2, 0.4],
      "masking_mode": "none (soft weights used in BCE loss)"
    }
  },
  "healthy_graph_injection": {
    "transient_spike_injection": true,
    "spike_config": {
      "per_feature_bernoulli_prob": 0.04,
      "spike_scale_range": [0.05, 0.15],
      "burst_nodes_per_graph": 3,
      "burst_features_per_node": "random [1,3]"
    },
    "purpose": "hard negatives: spatially isolated anomalies that don't propagate, forcing use of multi-hop context"
  },
  "normalization": {
    "scaler_type": "global per node type",
    "scaler_fit_samples": 300,
    "method": "standardization (mean=0, std=1)",
    "fitted_on": "healthy graphs only"
  },
  "reproducibility": {
    "master_seed": 42,
    "scaler_fit_seed": 42,
    "sample_seeds": "generated from master_seed",
    "randomness_sources": [
      "topology generation (per-sample)",
      "edge attribute sampling (per-sample)",
      "initial continuous state (per-sample)",
      "noise trajectory (per-sample)",
      "fault severity (per-sample)",
      "victim selection (per-sample)",
      "healthy spike injection (per-sample)"
    ]
  },
  "expected_benchmark_ranges": {
    "comment": "These are BASELINE expectations on this dataset; actual model performance depends on architecture",
    "healthy_vs_faulted_separability": "good (class imbalance ~1:1 or tuned)",
    "rca_baseline_accuracy_random": 0.5,
    "rca_baseline_accuracy_majority_class": 0.75,
    "rca_mlp_baseline_expected": [0.55, 0.70],
    "rca_gnn_expected": [0.65, 0.85],
    "rca_gnn_with_topology_ablation": [0.50, 0.65],
    "temporal_channel_importance": "high (rolling_var should not be removed)",
    "topology_importance": "medium-high (BFS distances make topology essential)"
  },
  "diagnostics_summary": {
    "label_leakage_detected": false,
    "node_type_alone_rc_accuracy": 0.45,
    "rc_vs_nonrc_feature_diff": 0.08,
    "kendall_tau_healthy_graphs": 0.12,
    "delta_short_ratio_rc_healthy": 3.2,
    "quality_score": 95,
    "trainability_estimate": "MEDIUM",
    "dataset_ready_for_training": true
  },
  "known_limitations": [
    "Healthy spike injection may add artificial anomalies that don't reflect real system behavior",
    "BFS topology assumes connected graph; isolated hosts will not receive propagated faults",
    "TruncExp delays assume uniform hop latency; real networks have variable latency",
    "Cross-type contamination is random; some fault types contaminate less realistically than others",
    "Rolling variance computed over only 5 steps; may be noisy in early simulation"
  ],
  "version_lineage": {
    "previous_version": "v1.0.0",
    "changelog": "Phase-2 hardening: added BFS temporal propagation, rolling variance channel, cross-type signatures, healthy spikes",
    "breaking_changes": [
      "Temporal tensor expanded from [N,F,3] to [N,F,4]",
      "Feature dimensionality per node increased (see feature_dim_total_with_categorical)",
      "Checkpoint from v1.x incompatible with v2.x"
    ]
  }
}
```

---

## PART 3 — CODE VERSIONING & CHECKPOINT BINDING

Every training checkpoint MUST store:

```python
# In train.py, save_checkpoint():
checkpoint = {
    "model_state": model.state_dict(),
    "optimizer_state": optimizer.state_dict(),
    "epoch": epoch,
    "train_loss": train_loss,
    
    # === VERSIONING: CRITICAL ===
    "dataset_version": "v2.0.0",
    "dataset_hash": "a7f3e9d2c...",
    "dataset_manifest_path": "path/to/dataset_manifest.json",
    "feature_schema_hash": "f2b8c9e3a...",
    "generator_code_hash": "a7f3e9d2c...",
    
    # Code versions (BEHAVIORAL hashes, not raw text)
    # Only include semantically important code (loss, optimizer, architecture)
    # Ignore: print statements, logging, docstrings, comments, whitespace
    "train_code_hash": sha256(extract_behavioral_code(train.py)),  # loss + optimizer logic only
    "model_code_hash": sha256(extract_behavioral_code(model.py)),  # layer definitions only
    "config_code_hash": sha256(canonical_json(CONFIG_DICT)),  # semantic config, not raw file
    
    # Experiment config
    "model_config": {
        "hidden_dim": 64,
        "num_layers": 3,
        "dropout": 0.2,
    },
    "loss_config": {
        "pos_weight_hdd": 1.5,
        "use_node_weight": true,
    },
    "optimizer_config": {
        "learning_rate": 0.001,
        "weight_decay": 1e-5,
    },
    
    # Reproducibility
    "torch_seed": 42,
    "numpy_seed": 42,
    "cuda_seed": 42,
}
torch.save(checkpoint, "checkpoint_v2.0.0_epoch10.pt")
```

### 3.1 Automatic Compatibility Check

**On load, verify:**

```python
def load_checkpoint_safe(checkpoint_path, manifest_path):
    checkpoint = torch.load(checkpoint_path, weights_only=False)
    manifest = json.load(open(manifest_path))
    
    # === STRICT CHECKS ===
    if not checkpoint.get("dataset_version"):
        # Old checkpoint before versioning
        raise ValueError(
            f"Checkpoint {checkpoint_path} has no version metadata.\n"
            f"This checkpoint was created before versioning was implemented.\n"
            f"Options:\n"
            f"  1. Retrain from scratch with current dataset\n"
            f"  2. Use UNSAFE_LEGACY_MODE=true (not recommended)\n"
            f"  3. Search git history for compatible dataset version"
        )
    
    ckpt_version = checkpoint["dataset_version"]
    ckpt_hash = checkpoint["dataset_hash"]
    manifest_version = manifest["dataset_identity"]["semantic_version"]
    manifest_hash = manifest["dataset_identity"]["dataset_hash"]
    
    if ckpt_version != manifest_version:
        raise IncompatibleVersionError(
            f"Dataset version mismatch:\n"
            f"  Checkpoint: {ckpt_version}\n"
            f"  Current manifest: {manifest_version}\n"
            f"  This is a MAJOR breaking change. Retraining required."
        )
    
    if ckpt_hash != manifest_hash:
        raise IncompatibleHashError(
            f"Dataset fingerprint mismatch.\n"
            f"  Checkpoint hash: {ckpt_hash}\n"
            f"  Current hash: {manifest_hash}\n"
            f"  Possible causes: seed changed, config changed, code changed.\n"
            f"  Result: metrics will not be reproducible."
        )
    
    # === CODE VERSION CHECKS ===
    train_code_now = sha256(open("train.py").read())
    if checkpoint.get("train_code_hash") != train_code_now:
        logger.warning(
            f"train.py has changed since checkpoint was created.\n"
            f"  Old hash: {checkpoint['train_code_hash']}\n"
            f"  New hash: {train_code_now}\n"
            f"  Action: checkpoint may give different results if code changes affected model behavior.\n"
            f"  Proceed with caution; consider retraining."
        )
    
    # === FEATURE DIM CHECK ===
    expected_feature_dims = manifest["feature_schema"]["feature_dim_total_with_categorical"]
    if checkpoint["model_config"].get("input_dims") != expected_feature_dims:
        raise IncompatibleFeatureError(
            f"Model input dimensions don't match dataset features.\n"
            f"  Model expects: {checkpoint['model_config']['input_dims']}\n"
            f"  Dataset has: {expected_feature_dims}\n"
            f"  This indicates a schema breaking change."
        )
    
    return checkpoint
```

### 3.2 Versioning Rules for Code Changes

| File | Change Type | Action |
|------|-------------|--------|
| `dataset_generator.py` | Algorithm change (e.g., new propagation) | → MINOR bump if non-breaking, MAJOR if schema changes |
| `train.py` | Loss function change | → Code hash changes; warn user; checkpoint may not load cleanly |
| `config.py` | Feature schema change | → MAJOR bump; recompute dataset hash; old checkpoints incompatible |
| `model.py` | Architecture change | → Code hash changes; old checkpoints may not load |

**Rule:** Checkpoint validity depends on (dataset_hash, code_hash) **pair**, not dataset alone.

---

## PART 4 — EXPERIMENT REGISTRY

Every training run automatically logs to `experiments/registry.jsonl`:

```json
{
  "experiment_id": "exp_20260522_143542_abc1234",
  "timestamp": "2026-05-22T14:35:42Z",
  "status": "completed",
  
  "dataset_info": {
    "dataset_version": "v2.0.0",
    "dataset_hash": "a7f3e9d2c...",
    "dataset_codename": "temporal_overlap_hardened"
  },
  
  "code_versions": {
    "train_code_hash": "abc1234def5678",
    "model_code_hash": "xyz9876abc1234",
    "config_code_hash": "def5678xyz9876"
  },
  
  "model_config": {
    "model_type": "HeteroGATv2",
    "hidden_dim": 64,
    "num_layers": 3,
    "dropout": 0.2,
    "heads": 4,
    "edge_dim": 19
  },
  
  "loss_config": {
    "loss_type": "BCEWithLogitsLoss",
    "pos_weight_cpu": 1.2,
    "pos_weight_gpu": 1.2,
    "pos_weight_ram": 1.5,
    "pos_weight_hdd": 2.0,
    "pos_weight_switch": 1.8,
    "pos_weight_job": 1.1,
    "use_node_weight": true
  },
  
  "optimizer_config": {
    "optimizer": "Adam",
    "learning_rate": 0.001,
    "weight_decay": 1e-5,
    "grad_clip": 1.0
  },
  
  "training_config": {
    "epochs": 50,
    "batch_size": 32,
    "validation_split": 0.15,
    "device": "cuda:0",
    "mixed_precision": false,
    "seed": 42
  },
  
  "ablation_mode": "none",
  "notes": "Phase-2 baseline training with rolling variance",
  
  "metrics": {
    "train_loss_final": 0.125,
    "val_loss_final": 0.145,
    "train_roc_auc": 0.968,
    "val_roc_auc": 0.952,
    "test_roc_auc": 0.945,
    "test_rca_top1_accuracy": 0.72,
    "test_f1": 0.38
  },
  
  "runtime": {
    "total_seconds": 3247,
    "wall_clock": "00:54:07",
    "peak_memory_mb": 4812,
    "checkpoint_path": "checkpoints/exp_20260522_143542_abc1234/best_model.pt"
  },
  
  "reproducibility_info": {
    "reproducible": true,
    "reproducibility_notes": "All seeds fixed, deterministic CUDA enabled",
    "expected_metric_variance": "±0.01 (stochastic sampling only)"
  },
  
  "compatibility_check": {
    "dataset_compatible": true,
    "code_compatible": true,
    "warnings": []
  }
}
```

**Registry structure:**
```
experiments/
├── registry.jsonl          # One JSON object per line, chronological
├── registry_index.json     # For fast lookup: exp_id → line_number
└── exp_20260522_143542_abc1234/
    ├── config.json         # Full config snapshot
    ├── best_model.pt       # Checkpoint with versioning metadata
    ├── training_log.txt    # Full stdout
    └── metrics_plot.png
```

### 4.0 Atomic Write Safety

**Every registry append MUST be atomic:**

```python
def append_to_registry_safe(experiment_metadata, registry_path="experiments/registry.jsonl"):
    """Write to registry with atomic guarantees."""
    
    # Step 1: Write to temp file
    temp_path = registry_path + f".tmp.{os.getpid()}.{time.time()}"
    with open(temp_path, "w") as f:
        json.dump(experiment_metadata, f)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())  # Force write to disk
    
    # Step 2: Atomic rename (atomic on POSIX)
    try:
        os.replace(temp_path, registry_path)  # Atomic on Windows + POSIX
    except Exception:
        os.remove(temp_path)
        raise
    
    # Step 3: Update index
    index_path = registry_path.replace(".jsonl", "_index.json")
    with open(index_path + ".tmp", "w") as f:
        index = json.load(open(index_path)) if os.path.exists(index_path) else {}
        index[experiment_metadata["experiment_id"]] = {
            "line_number": sum(1 for _ in open(registry_path)) - 1,
        }
        json.dump(index, f)
        f.flush()
        os.fsync(f.fileno())
    
    os.replace(index_path + ".tmp", index_path)
```

**Guarantees:**
- ✅ No partial writes (temp → atomic rename)
- ✅ No lost data if crash mid-write
- ✅ Index stays in sync with registry
- ✅ Works on Windows + POSIX

### 4.1 Registry Queries

```python
def find_experiments_by_dataset(dataset_hash):
    """Find all experiments using this dataset version."""
    results = []
    with open("experiments/registry.jsonl") as f:
        for line in f:
            exp = json.loads(line)
            if exp["dataset_info"]["dataset_hash"] == dataset_hash:
                results.append(exp)
    return results

def find_experiments_by_code(train_code_hash):
    """Find all experiments trained with this code version."""
    results = []
    with open("experiments/registry.jsonl") as f:
        for line in f:
            exp = json.loads(line)
            if exp["code_versions"]["train_code_hash"] == train_code_hash:
                results.append(exp)
    return results

def is_experiment_still_valid(exp_id):
    """Check if an old experiment can be reproduced now."""
    exp = load_experiment_metadata(exp_id)
    current_manifest = json.load(open("dataset_manifest.json"))
    
    dataset_compatible = exp["dataset_info"]["dataset_hash"] == current_manifest["dataset_identity"]["dataset_hash"]
    code_compatible = all(
        exp["code_versions"][k] == compute_hash(f)
        for k, f in [
            ("train_code_hash", "train.py"),
            ("model_code_hash", "model.py"),
            ("config_code_hash", "config.py"),
        ]
    )
    return dataset_compatible and code_compatible
```

---

## PART 5 — DATASET DIFF TOOL

Implement `compare_dataset_versions.py`:

```bash
python compare_dataset_versions.py v1.0.0 v2.0.0 --output diff_report.txt
```

**Output: `diff_report.txt`**

```
================================================================================
DATASET VERSION COMPARISON REPORT
================================================================================

Comparison:
  v1.0.0 (a7f3e9d2c...) generated 2026-05-15T10:22:00Z
  v2.0.0 (b8g4f0e3d...) generated 2026-05-22T14:35:42Z

Breaking Changes Detected: YES ⚠️

================================================================================
SCHEMA DIFFERENCES
================================================================================

[BREAKING] Temporal channels: 3 → 4
  v1.0.0 channels: [value, delta_short, delta_long]
  v2.0.0 channels: [value, delta_short, delta_long, rolling_var]
  
  Impact: Feature dimensionality changed
    cpu:     24 → 32  (+8 features)
    gpu:     36 → 48  (+12 features)
    ram:     24 → 32  (+8 features)
    hdd:     24 → 32  (+8 features)
    switch:  30 → 40  (+10 features)
    job:     33 → 44  (+11 features)
  
  ❌ Checkpoint incompatibility: CRITICAL
     Models trained on v1.0.0 CANNOT load on v2.0.0
     Reason: Input layer expects 24 features (cpu), gets 32
  
  Workaround: Retrain from scratch on v2.0.0

================================================================================
PROPAGATION ALGORITHM DIFFERENCES
================================================================================

[MAJOR] Temporal activation: UNIFORM → BFS-DELAY

v1.0.0:
  - All anomalies start at FAULT_INJECTION_STEP=50
  - Propagate uniformly over remaining 40 steps
  - No topology-dependent activation times
  
v2.0.0:
  - Per-victim activation computed via BFS + TruncExp delays
  - Victims activate stochastically based on hop distance
  - Max 3 hops, TruncExp(λ=0.15, max=10.0) per hop
  
Impact on metrics:
  ❌ Models trained on v1.0.0 may overfit to uniform activation
  ⚠️  v2.0.0 makes topology essential; v1.0.0 models may generalize poorly
  Predicted metric shift: ROC-AUC v1.0.0 models on v2.0.0: -0.05 to -0.08

Statistical difference: Wasserstein distance on fault activation times
  D_W = 0.18 (moderate difference; expect some accuracy drop)

================================================================================
FAULT GENERATION DIFFERENCES
================================================================================

Severity range: [0.7, 0.95] → [0.15, 0.45]
  Severity increased in v1 (higher amplitude, easier detection)
  Severity decreased in v2 (lower amplitude, more challenging)
  Expected impact: ROC-AUC drop ~0.03-0.05 for same model

Cross-type contamination:
  v1.0.0: not implemented
  v2.0.0: 15-25% probability, overlaps fault signatures
  Expected impact: prevents trivial feature correlation shortcuts

Victim selection:
  v1.0.0: deterministic per fault type (probability-based, not stochastic)
  v2.0.0: stochastic per hop distance, attenuation by distance
  Impact: more challenging RCA task; topology now matters

================================================================================
HEALTHY GRAPH INJECTION
================================================================================

[NEW] Transient spike injection (hard negatives)

v1.0.0: none
v2.0.0: Bernoulli(p=0.04) per feature + concentrated bursts
  - Healthy graphs now contain spatially isolated anomalies
  - Forces model to use multi-hop context instead of node features alone
  Expected impact: models must learn propagation; baseline accuracy drops ~0.05

================================================================================
FEATURE DIMENSIONALITY IMPACT TABLE
================================================================================

Dataset         v1.0.0 dims    v2.0.0 dims    Δ          Δ%      Model impact
─────────────────────────────────────────────────────────────────────────────
cpu             24             32             +8         +33%    Need retrain
gpu             36             48             +12        +33%    Need retrain
ram             24             32             +8         +33%    Need retrain
hdd             24             32             +8         +33%    Need retrain
switch          30             40             +10        +33%    Need retrain
job             33             44             +11        +33%    Need retrain

✅ Recommendation: Retrain all models on v2.0.0

================================================================================
METRIC COMPATIBILITY ANALYSIS
================================================================================

Can a v1.0.0-trained model be evaluated on v2.0.0 dataset?
  Answer: NO
  Reasons:
    1. Input dimension mismatch (24 vs 32 for cpu)
    2. Activation distribution incompatible
    3. Fault severity range incompatible

Can v1.0.0 and v2.0.0 metrics be compared directly?
  Answer: PROCEED WITH CAUTION
  
  If you re-evaluate v1.0.0 models on v2.0.0 after padding features:
    Expected degradation:
      - ROC-AUC drop: -0.04 to -0.08 (due to activation algorithm change)
      - RCA accuracy drop: -0.08 to -0.12 (due to topology importance)
      - F1 drop: -0.05 to -0.10 (due to severity reduction)
    
    These shifts are NOT bugs; they reflect dataset hardening.
    
  How to compare fairly:
    1. Retrain v1.0.0 architecture on v2.0.0 dataset (as baseline)
    2. Train v2.0.0 architecture on v2.0.0 dataset (as improved)
    3. Compare: improved / baseline

================================================================================
REPRODUCIBILITY IMPACT
================================================================================

Old experiment on v1.0.0:
  Reproducible today? PARTIAL
  ✅ Can still generate v1.0.0 dataset (git checkout previous commit)
  ✅ Can rerun training on v1.0.0 dataset
  ✅ Metrics will be identical (deterministic)
  ❌ Cannot merge results with v2.0.0 experiments (incompatible metrics)

New experiment on v2.0.0:
  Reproducible in future? YES
  ✅ Dataset manifest contains all parameters
  ✅ Code hash pins exact generator version
  ✅ Checkpoint stores dataset_hash + code_hash
  ✅ Can verify compatibility before loading

================================================================================
BACKWARD COMPATIBILITY CHECKLIST
================================================================================

[✅] Can old datasets still be loaded?
     Yes, but versioning was added after, so old datasets lack manifest.json
     Recommendation: regenerate old datasets or add legacy metadata.

[✅] Can old checkpoints still be loaded?
     If versioning metadata was backfilled: yes (with warnings)
     If not: no (checkpoints from before versioning have no metadata)

[❌] Can old experiments be reproduced with new code?
     No, if dataset_hash differs.
     Use git checkout to restore old dataset_generator.py + config.py

[❌] Can old metrics be compared with new metrics?
     Only with caution, and only after statistical adjustment for dataset changes.

================================================================================
RECOMMENDATION SUMMARY
================================================================================

Action items:
  1. ✅ Release v2.0.0 as MAJOR version bump
  2. ✅ Retrain all baseline models on v2.0.0 dataset
  3. ⚠️  Compare v2.0.0 results to v1.0.0 + adjustment factors, not directly
  4. 📝 Document in paper: "metrics from v1.0.0 not directly comparable due to hardening"
  5. 🔒 Lock v1.0.0 datasets in git (never regenerate, always use manifest)
  6. 📊 Generate ablation tables showing v1.0.0 vs v2.0.0 performance on same model

Timeline:
  - v1.0.0 archived: do not modify
  - v2.0.0 new primary: all new work uses this
  - Future v3.0: follow same protocol (new manifest, new tests, clear lineage)

================================================================================
```

---

## PART 6 — STRICT REPRODUCIBILITY: `reproduce_experiment.py`

```bash
python reproduce_experiment.py exp_20260522_143542_abc1234 --verify-metrics
```

**Script:**

```python
def reproduce_experiment(exp_id, verify_metrics=True, device="cuda:0"):
    """
    Load experiment metadata and reproduce training exactly.
    
    Steps:
    1. Load experiment config from registry
    2. Restore dataset version (regenerate if needed)
    3. Verify dataset_hash matches checkpoint
    4. Retrain model with identical config
    5. Compare metrics (should be deterministic to ±epsilon)
    6. Generate reproducibility report
    """
    
    # Step 1: Load experiment metadata
    exp_metadata = load_experiment_metadata(exp_id)
    print(f"[*] Loaded experiment: {exp_id}")
    print(f"    Dataset version: {exp_metadata['dataset_info']['dataset_version']}")
    print(f"    Train code hash: {exp_metadata['code_versions']['train_code_hash']}")
    
    # Step 2: Restore dataset
    required_version = exp_metadata["dataset_info"]["dataset_version"]
    required_hash = exp_metadata["dataset_info"]["dataset_hash"]
    
    current_manifest = json.load(open("dataset_manifest.json"))
    current_hash = current_manifest["dataset_identity"]["dataset_hash"]
    
    if current_hash != required_hash:
        print(f"\n[!] Dataset hash mismatch!")
        print(f"    Required: {required_hash}")
        print(f"    Current:  {current_hash}")
        print(f"    Action: Regenerating dataset version {required_version}...")
        regenerate_dataset_from_manifest(required_version)
        current_manifest = json.load(open("dataset_manifest.json"))
        assert json.load(open("dataset_manifest.json"))["dataset_identity"]["dataset_hash"] == required_hash
        print(f"    ✓ Dataset restored")
    
    # Step 3: Verify code (warn if changed)
    train_code_now = compute_hash(open("train.py").read())
    if train_code_now != exp_metadata["code_versions"]["train_code_hash"]:
        print(f"\n[⚠] Warning: train.py has changed since experiment!")
        print(f"    This may produce different results.")
        response = input("    Proceed anyway? [y/N] ")
        if response.lower() != "y":
            return
    
    # Step 4: Rebuild model and retrain
    print(f"\n[*] Rebuilding model...")
    model = HeteroGATv2(
        node_dims=current_manifest["feature_schema"]["feature_dim_total_with_categorical"],
        hidden_dim=exp_metadata["model_config"]["hidden_dim"],
        num_layers=exp_metadata["model_config"]["num_layers"],
    )
    model = model.to(device)
    
    print(f"[*] Retraining with identical config...")
    set_seed(exp_metadata["training_config"]["seed"])
    
    metrics = train_model(
        model=model,
        dataset_dir="dataset",
        epochs=exp_metadata["training_config"]["epochs"],
        batch_size=exp_metadata["training_config"]["batch_size"],
        learning_rate=exp_metadata["optimizer_config"]["learning_rate"],
        device=device,
        seed=exp_metadata["training_config"]["seed"],
    )
    
    # Step 5: Compare metrics
    if verify_metrics:
        print(f"\n[*] Verifying metric reproducibility...")
        orig_metrics = exp_metadata["metrics"]
        
        for key in orig_metrics:
            orig_val = orig_metrics[key]
            new_val = metrics.get(key, float("nan"))
            diff = abs(orig_val - new_val)
            epsilon = 0.01  # Allow 1% tolerance for stochastic variance
            
            status = "✅" if diff < epsilon else "⚠️"
            print(f"    {status} {key:25s}: {orig_val:.4f} → {new_val:.4f} (Δ={diff:.4f})")
        
        if all(abs(orig_metrics[k] - metrics.get(k, float("nan"))) < 0.01 for k in orig_metrics):
            print(f"\n✅ REPRODUCIBILITY VERIFIED")
            print(f"   All metrics match (within ±0.01 stochastic tolerance)")
        else:
            print(f"\n⚠️ REPRODUCIBILITY WARNING")
            print(f"   Some metrics differ. Possible causes:")
            print(f"   - Code changes in train.py (even if not in versioning)")
            print(f"   - CUDA non-determinism (set CUBLAS_WORKSPACE_CONFIG=:4294967296)")
            print(f"   - Seed differences in DataLoader or sampler")
    
    # Step 6: Generate report
    report = {
        "experiment_id": exp_id,
        "reproduced_at": datetime.now().isoformat(),
        "reproducible": all(
            abs(exp_metadata["metrics"][k] - metrics.get(k, float("nan"))) < 0.01
            for k in exp_metadata["metrics"]
        ),
        "original_metrics": exp_metadata["metrics"],
        "reproduced_metrics": metrics,
        "metric_diffs": {
            k: abs(exp_metadata["metrics"][k] - metrics.get(k, float("nan")))
            for k in exp_metadata["metrics"]
        }
    }
    
    with open(f"reproducibility_report_{exp_id}.json", "w") as f:
        json.dump(report, f, indent=2)
    
    print(f"\n[*] Report saved: reproducibility_report_{exp_id}.json")
```

---

## PART 7 — SCIENTIFIC TRACEABILITY: `benchmark_summary.md`

Generate automatically after every experiment:

```bash
python generate_benchmark_summary.py --dataset v2.0.0 --experiments recent --output benchmark_summary.md
```

**Output: `benchmark_summary.md`**

```markdown
# RCA Dataset & Training Benchmark Summary

**Generated:** 2026-05-22T14:35:42Z

---

## Dataset Information

**Version:** v2.0.0 (temporal_overlap_hardened)
**Hash:** a7f3e9d2c...
**Generated:** 2026-05-22T10:22:00Z

### Changelog from v1.0.0

- [MAJOR] Temporal channels: 3 → 4 (added rolling_var)
- [MAJOR] Activation algorithm: uniform → BFS-delay
- [MINOR] Cross-type contamination added
- [MINOR] Healthy transient spikes added
- [PATCH] Severity range adjusted [0.7, 0.95] → [0.15, 0.45]

### Datasets Details

| Property | Value |
|----------|-------|
| Total graphs | 5000 |
| Healthy / Faulted | 2500 / 2500 |
| Node types | 7 (cpu, gpu, ram, hdd, switch, job, rca_context) |
| Temporal channels | 4 (value, delta_short, delta_long, rolling_var) |
| Feature dim (cpu) | 32 (continuous=8×4, categorical=0) |
| Feature dim (gpu) | 50 (continuous=12×4, categorical=2) |

---

## Training Results

### Baseline Models

#### 1. MLP (No Topology)
- Architecture: 2-layer MLP per node type
- Parameters: ~50K per node type
- **Metrics:**
  - Val ROC-AUC: 0.78 ± 0.02
  - Test ROC-AUC: 0.76 ± 0.03
  - RCA Top-1: 0.62 ± 0.04

#### 2. GNN (HeteroGATv2, Full Topology)
- Architecture: 3-layer GATv2, hidden_dim=64
- Parameters: ~200K
- **Metrics:**
  - Val ROC-AUC: 0.95 ± 0.01
  - Test ROC-AUC: 0.93 ± 0.02
  - RCA Top-1: 0.71 ± 0.03
  - F1: 0.38 ± 0.04

### Ablation Results

| Ablation | Val ROC-AUC | Test ROC-AUC | RCA Top-1 | Observation |
|----------|-------------|--------------|-----------|-------------|
| Full GNN | 0.95 | 0.93 | 0.71 | Baseline |
| No edges (MLP) | 0.78 | 0.76 | 0.62 | **Δ = -0.17** ← Topology critical |
| Edge dropout 50% | 0.91 | 0.89 | 0.67 | **Δ = -0.04** ← Some redundancy |
| Randomized topology | 0.85 | 0.83 | 0.58 | **Δ = -0.10** ← Host-job mapping matters |
| Temporal shuffle | 0.80 | 0.78 | 0.60 | **Δ = -0.15** ← Temporal ordering critical |

**Interpretation:**
- Temporal channels (rolling_var) are **essential** for good performance
- Topology is **necessary** but has redundancy
- Multi-hop propagation is learned and used

### Leakage Diagnostics

| Check | Result | Status |
|-------|--------|--------|
| Node-type RC prediction | 0.45 | ✅ Low (baseline=0.45) |
| RC vs non-RC feature diff | 0.08 | ✅ Low |
| Kendall τ (healthy) | 0.12 | ✅ < 0.15 threshold |
| delta_short RC/healthy ratio | 3.2 | ✅ Moderate (OK if <10) |

**Conclusion:** No significant leakage detected.

---

## Reproducibility & Traceability

### Experiment Registry

```
Experiments using v2.0.0:
  - exp_20260522_143542_abc1234   [baseline GNN]        ROC-AUC=0.933
  - exp_20260522_151203_def5678   [MLP ablation]        ROC-AUC=0.761
  - exp_20260522_155847_ghi9012   [edge dropout]        ROC-AUC=0.891
  - exp_20260522_162134_jkl3456   [temporal shuffle]    ROC-AUC=0.783
```

### Reproducibility Guarantee

All experiments are **reproducible**:
- ✅ Dataset manifest locks all generation parameters
- ✅ Checkpoint stores dataset_hash + code_hash
- ✅ Seeds fixed; CUDA determinism enabled
- ✅ Code versions tracked independently
- ✅ Can regenerate dataset or retrain any experiment at any time

### Code Versions

| File | Hash | Status |
|------|------|--------|
| train.py | abc1234def5678 | Locked in checkpoint |
| dataset_generator.py | xyz9876abc1234 | Locked in manifest |
| config.py | def5678xyz9876 | Locked in manifest |
| model.py | jkl3456def5678 | Locked in checkpoint |

---

## Known Limitations

1. **Healthy spike injection** adds artificial anomalies; may not reflect real system noise
2. **BFS topology** assumes connected graph; isolated hosts don't receive propagation
3. **TruncExp delays** assume uniform hop latency; real networks have variable latency
4. **Rolling variance** computed over only 5 steps; noisy in early simulation
5. **Cross-type contamination** is random; some fault types more realistic than others

---

## Metric Confidence Intervals

Based on 5 independent runs with different seeds:

| Metric | Mean | Std Dev | 95% CI |
|--------|------|---------|--------|
| Test ROC-AUC | 0.933 | 0.009 | [0.915, 0.951] |
| Test F1 | 0.380 | 0.018 | [0.345, 0.415] |
| RCA Top-1 | 0.708 | 0.032 | [0.645, 0.771] |

---

## Future Work

- [ ] Compare v2.0.0 results to v1.0.0 (with adjustment factors for dataset differences)
- [ ] Implement temporal-attention layers (focus on important time steps)
- [ ] Add physics-informed constraints (respect actual cluster topology)
- [ ] Extend to multi-fault scenarios (simultaneous HDD + network failures)

---

## References & Artifacts

- Dataset manifest: `dataset_manifest.json`
- Experiment registry: `experiments/registry.jsonl`
- Reproducibility report: `reproducibility_report_exp_20260522_143542_abc1234.json`
- Ablation analysis: `ablation_results.csv`
- Benchmark plots: `plots/benchmark_*.png`

---

**Generated with research-grade versioning. All metrics traceable to dataset + code.**
```

---

## PART 8 — VERSIONING RULES (Semantic Versioning 2.0)

### MAJOR Version Bump

Breaking schema changes that require retraining:

1. **Temporal tensor layout**
   - Example: `[N,F,3]` → `[N,F,4]`
   - Impact: Input layer sizes change; old checkpoints incompatible

2. **Feature schema changes**
   - Example: New feature added to FEATURE_SCHEMA
   - Impact: Feature dimensionality differs; models can't load

3. **Propagation algorithm fundamental change**
   - Example: Uniform activation → BFS-delay
   - Impact: Fault patterns change; old models may overfit to old patterns

4. **Normalization method change**
   - Example: per-node standardization → global standardization
   - Impact: Feature scales differ; model weights need recalibration

### MINOR Version Bump

Non-breaking improvements (can use with old checkpoints with caution):

1. **New fault family**
   - Example: Add "memory_fragmentation" fault type
   - Impact: Dataset larger, metrics might improve, but old models still work

2. **Better propagation model (same dimensionality)**
   - Example: Improved victim selection probability
   - Impact: Faults more realistic, metrics change, but features unchanged

3. **New noise model**
   - Example: Improved temporal noise to match real system
   - Impact: More challenging task, metrics drop, but input dims same

### PATCH Version Bump

Bug fixes (should not change metrics, just fix bugs):

1. **Seed bug fix**
   - Example: Off-by-one error in RNG initialization
   - Impact: Reproducing old runs gives slightly different results, but retraining gives same results

2. **Clipping bug**
   - Example: Feature values sometimes exceed [0, 1]
   - Impact: May have affected training; retraining should fix

3. **Loss computation bug**
   - Example: Incorrect masking in BCEWithLogitsLoss
   - Impact: Metrics were wrong; recalculating gives correct results

---

## PART 9 — IMPLEMENTATION REQUIREMENTS (CRITICAL)

### 9.1 Semantic Hash Validation (Not Raw Text)

```python
def compute_semantic_dataset_hash(config_dict):
    """Hash only semantic config, immune to whitespace/formatting changes."""
    
    semantic = {
        "topology": {k: config_dict["topology"][k] for k in ["NUM_HOSTS", "NUM_LEAF", "NUM_SPINE"]},
        "temporal": {k: config_dict["temporal"][k] for k in ["SIMULATION_STEPS", "EMA_ALPHA", "NOISE_SCALE"]},
        "fault": {k: config_dict["fault"][k] for k in ["severity_range", "fault_types"]},
        "seeds": config_dict["seeds"],
    }
    
    canonical = json.dumps(semantic, sort_keys=True, separators=(',', ':'))
    canonical = re.sub(r'(\d\.\d{7,})', lambda m: str(round(float(m.group(1)), 6)), canonical)
    return hashlib.sha256(canonical.encode()).hexdigest()

def validate_dataset_hash(manifest_path):
    """Verify dataset hash; fails if config changed."""
    manifest = json.load(open(os.path.join(manifest_path, "identity.json")))
    config = json.load(open(os.path.join(manifest_path, "generation_config.json")))
    
    stored_hash = manifest["dataset_identity"]["dataset_hash"]
    computed_hash = compute_semantic_dataset_hash(config)
    
    if stored_hash != computed_hash:
        raise ValueError(
            f"Dataset fingerprint validation FAILED.\n"
            f"  Stored:    {stored_hash}\n"
            f"  Recomputed: {computed_hash}\n"
            f"  Config may have been modified after generation."
        )
```

### 9.2 Manifest Reconstruction

**Manifests MUST fully reconstruct datasets:**

```python
def regenerate_dataset_from_manifest(manifest_path):
    """Regenerate dataset using only manifest + code."""
    manifest = json.load(open(manifest_path))
    
    # Extract ALL parameters from manifest
    config = manifest["fault_generation"]
    temporal = manifest["temporal_config"]
    
    # Regenerate with exact config
    dataset = generate_dataset(
        num_samples=manifest["dataset_structure"]["total_graphs"],
        healthy_ratio=manifest["dataset_structure"]["healthy_graphs"] / manifest["dataset_structure"]["total_graphs"],
        fault_types=manifest["fault_generation"]["fault_types"],
        severity_range=tuple(manifest["fault_generation"]["severity_range"]),
        seed=manifest["reproducibility"]["master_seed"],
    )
    
    # Verify fingerprint matches
    new_manifest = dataset.save_and_get_manifest()
    assert new_manifest["dataset_identity"]["dataset_hash"] == manifest["dataset_identity"]["dataset_hash"]
    
    return dataset
```

### 9.3 Incompatibility Detection

**System MUST automatically detect incompatibilities:**

```python
def check_checkpoint_compatibility(checkpoint_path, dataset_manifest_path):
    """Comprehensive incompatibility check."""
    checkpoint = torch.load(checkpoint_path)
    manifest = json.load(open(dataset_manifest_path))
    
    issues = []
    warnings = []
    
    # Check 1: Version metadata exists
    if "dataset_version" not in checkpoint:
        issues.append("Checkpoint missing dataset_version (pre-versioning checkpoint)")
    
    # Check 2: Hash match
    if checkpoint.get("dataset_hash") != manifest["dataset_identity"]["dataset_hash"]:
        issues.append(f"Dataset hash mismatch (checkpoint={checkpoint.get('dataset_hash')}, current={manifest['dataset_identity']['dataset_hash']})")
    
    # Check 3: Feature dimensionality
    expected_dims = manifest["feature_schema"]["feature_dim_total_with_categorical"]
    model_dims = checkpoint.get("model_config", {}).get("input_dims")
    if model_dims and model_dims != expected_dims:
        issues.append(f"Feature dimension mismatch (model expects {model_dims}, dataset has {expected_dims})")
    
    # Check 4: Code changes
    if checkpoint.get("train_code_hash"):
        current_train_hash = compute_hash(open("train.py").read())
        if checkpoint["train_code_hash"] != current_train_hash:
            warnings.append(f"train.py changed since checkpoint creation (may affect results)")
    
    # Check 5: Version compatibility
    ckpt_version = checkpoint.get("dataset_version")
    manifest_version = manifest["dataset_identity"]["semantic_version"]
    if ckpt_version and ckpt_version.split(".")[0] != manifest_version.split(".")[0]:
        issues.append(f"MAJOR version mismatch (checkpoint {ckpt_version}, current {manifest_version})")
    
    return {
        "compatible": len(issues) == 0,
        "issues": issues,
        "warnings": warnings,
    }
```

### 9.4 Experiment Registry Durability

**Registry MUST survive crashes:**

```python
def append_to_registry(experiment_metadata):
    """Append to registry.jsonl with fsync for durability."""
    registry_path = "experiments/registry.jsonl"
    
    with open(registry_path, "a") as f:
        json.dump(experiment_metadata, f)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())  # Ensure written to disk
    
    # Update index
    with open("experiments/registry_index.json", "r") as f:
        index = json.load(f)
    
    index[experiment_metadata["experiment_id"]] = {
        "line_number": sum(1 for _ in open(registry_path)) - 1,
        "timestamp": experiment_metadata["timestamp"],
    }
    
    with open("experiments/registry_index.json", "w") as f:
        json.dump(index, f)
        f.flush()
        os.fsync(f.fileno())
```

### 9.5 Metadata Machine-Readability

**Metadata MUST be strictly JSON (no comments, no loose structure):**

```python
def validate_manifest_format(manifest_path):
    """Strict JSON validation + schema check."""
    try:
        manifest = json.load(open(manifest_path))
    except json.JSONDecodeError as e:
        raise ValueError(f"Manifest is not valid JSON: {e}")
    
    # Check required fields
    required_fields = [
        "dataset_identity.semantic_version",
        "dataset_identity.dataset_hash",
        "generation_config.generator_code_hash",
        "dataset_structure.total_graphs",
        "temporal_config.simulation_steps",
        "fault_generation.fault_types",
        "feature_schema.feature_dim_total_with_categorical",
    ]
    
    for field_path in required_fields:
        keys = field_path.split(".")
        obj = manifest
        try:
            for key in keys:
                obj = obj[key]
        except (KeyError, TypeError):
            raise ValueError(f"Manifest missing required field: {field_path}")
    
    return True
```

---

## PART 10 — REPRODUCIBILITY GUARANTEE

### How reproducibility is guaranteed:

1. **Deterministic dataset generation**
   - All RNG seeded; same seed → identical dataset
   - Manifest contains all seeds and configs
   - Regenerating from manifest always gives same data

2. **Immutable dataset identity**
   - Hash computed from all generation parameters
   - Hash changes if ANY parameter differs
   - Impossible to silently regenerate with different settings

3. **Code versioning**
   - Every checkpoint stores code hashes (train.py, model.py, config.py)
   - Incompatible code versions detected automatically
   - Warnings issued if code has changed

4. **Experiment registry**
   - Every training run logged with dataset_hash + code_hash pair
   - Can query: "all experiments using dataset v2.0.0"
   - Can determine: "which experiments need retraining after code change"

### How dataset lineage is tracked:

1. **Version history**
   - Manifest records version lineage (which version preceded this one)
   - Changelog documents what changed between versions
   - Breaking changes explicitly marked

2. **Experiment traceability**
   - registry.jsonl: timestamp + dataset_hash + code_hash for every run
   - Can trace experiment → dataset → manifest → generation parameters
   - Can answer: "what exact fault amplitude distribution did this use?"

3. **Git integration (optional)**
   - dataset_generator.py and config.py in git
   - git_commit_hash stored in manifest
   - Can checkout exact code version used for generation

### How incompatible experiments are prevented:

1. **Pre-load checks**
   - Check checkpoint dataset_hash vs current manifest hash
   - Check feature dimensions match
   - Check MAJOR version compatibility

2. **Explicit errors**
   ```
   Error: Dataset version mismatch
     Checkpoint: v1.0.0
     Current:    v2.0.0
     Action: Retraining required (breaking changes detected)
   ```

3. **Code-aware loading**
   - Compare checkpoint code_hash with current code_hash
   - Warn if train.py changed
   - Suggest retraining if significant changes

### How future benchmark evolution remains auditable:

1. **Immutable version records**
   - Every dataset version locked with manifest
   - Cannot re-run "v1.0.0 generation" and get different results (hash ensures this)
   - Papers can cite: "results on v2.0.0 dataset (hash: a7f3e9d2c...)"

2. **Diff tool documents evolution**
   - Comparing v1.0.0 → v2.0.0 clearly shows what changed
   - Expected metric shifts documented (ROC-AUC drop -0.05)
   - Readers understand why metrics differ

3. **Experiment registry is permanent record**
   - registry.jsonl grows only; never modified
   - All past experiments recorded with full config
   - Future readers can see: "in 2026, we trained X model on Y dataset with Z config, got ROC=0.93"

4. **Backward compatibility preserved**
   - Old datasets always regenerable (manifest contains all params)
   - Old experiments always reproducible (checkpoint stores everything)
   - Future researchers can compare new results to old baselines fairly

---

## FINAL IMPLEMENTATION CHECKLIST

- [ ] Part 1: Semantic versioning scheme in code
- [ ] Part 2: Dataset manifest generation (JSON file per dataset)
- [ ] Part 3: Checkpoint versioning metadata + compatibility checks
- [ ] Part 4: Experiment registry (registry.jsonl + index.json)
- [ ] Part 5: compare_dataset_versions.py with detailed diff report
- [ ] Part 6: reproduce_experiment.py with metric verification
- [ ] Part 7: benchmark_summary.md automatic generation
- [ ] Part 8: Version bump rules documented + enforced
- [ ] Part 9: Semantic hash validation, behavioral code hash, feature ordering protection, atomic writes, manifest schema validation
- [ ] Part 10: Full backward compatibility testing + lineage traceability verification

**Goal:** Every metric reported is 100% traceable to exact dataset version, code version, and experiment config. No ambiguity. Science-grade reproducibility.
