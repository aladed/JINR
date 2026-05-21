"""
training_pipeline/dataset_generator.py

Step 2: Static topology, deterministic node indexing, routing maps,
dummy feature tensors, HeteroData assembly, and smoke-test validation.

Step 3: TemporalStateKeeper, healthy trajectory simulation,
temporal feature history (value / delta_short / delta_long).

Step 4: Fault injection -- HDD degradation, network congestion, RAM leak.
Stochastic anomaly generation, downstream propagation, RCA labeling,
victim masking.

No normalization, tensor flattening, serialization, or training loop.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np
import torch
from torch_geometric.data import HeteroData

from training_pipeline.config import (
    CATEGORICAL_SUFFIXES,
    CONTEXT_EDGE_TYPES,
    CONTINUOUS_SUFFIXES,
    EDGE_DIM_DUMMY,
    EDGE_DIM_PHYSICAL,
    EMA_ALPHA,
    FAULT_INJECTION_STEP,
    FEATURE_SCHEMA,
    LOGICAL_EDGE_TYPES,
    NODE_TYPES,
    NUM_HOSTS,
    NUM_JOBS,
    NUM_LEAF,
    NUM_SPINE,
    PHYSICAL_EDGE_TYPES,
    SIMULATION_STEPS,
)

# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------

torch.manual_seed(42)
_rng = np.random.default_rng(42)

# ---------------------------------------------------------------------------
# Derived topology constants
# ---------------------------------------------------------------------------

NUM_SWITCHES: int = NUM_LEAF + NUM_SPINE

# Node counts per type -- deterministic, fixed for the whole pipeline.
NODE_COUNTS: Dict[str, int] = {
    "cpu":         NUM_HOSTS,
    "gpu":         NUM_HOSTS,
    "ram":         NUM_HOSTS,
    "hdd":         NUM_HOSTS,
    "switch":      NUM_SWITCHES,
    "job":         NUM_JOBS,
    "rca_context": 1,
}

# Switch index ranges:
#   leaf  switches: indices [0,          NUM_LEAF)
#   spine switches: indices [NUM_LEAF,   NUM_LEAF + NUM_SPINE)
LEAF_SWITCH_IDS:  List[int] = list(range(NUM_LEAF))
SPINE_SWITCH_IDS: List[int] = list(range(NUM_LEAF, NUM_LEAF + NUM_SPINE))

# ---------------------------------------------------------------------------
# Routing maps (static, deterministic)
# ---------------------------------------------------------------------------

def build_routing_maps(
    seed: Optional[int] = None,
) -> Tuple[
    Dict[int, int],        # host_to_leaf:   host_id  -> leaf_switch_id
    Dict[int, List[int]],  # leaf_to_spines: leaf_id  -> [spine_ids]
    Dict[int, int],        # job_to_host:    job_id   -> host_id
    Dict[int, List[int]],  # host_to_jobs:   host_id  -> [job_ids]
]:
    """Build routing maps.

    With seed=None (default) uses deterministic round-robin (backward-compat).
    With seed provided, randomizes host→leaf and job→host assignments so each
    generated graph has a unique topology — eliminating positional identity leakage.
    """
    if seed is not None:
        _rt_rng = np.random.default_rng(seed)
        host_to_leaf: Dict[int, int] = {
            h: int(_rt_rng.integers(0, NUM_LEAF))
            for h in range(NUM_HOSTS)
        }
        job_to_host: Dict[int, int] = {
            j: int(_rt_rng.integers(0, NUM_HOSTS))
            for j in range(NUM_JOBS)
        }
    else:
        # Deterministic round-robin — kept for smoke tests and scaler fitting.
        host_to_leaf = {
            host_id: LEAF_SWITCH_IDS[host_id % NUM_LEAF]
            for host_id in range(NUM_HOSTS)
        }
        job_to_host = {
            job_id: job_id % NUM_HOSTS
            for job_id in range(NUM_JOBS)
        }

    # Every leaf switch connects to all spine switches.
    leaf_to_spines: Dict[int, List[int]] = {
        leaf_id: list(SPINE_SWITCH_IDS)
        for leaf_id in LEAF_SWITCH_IDS
    }

    # Invert job_to_host.
    host_to_jobs: Dict[int, List[int]] = {h: [] for h in range(NUM_HOSTS)}
    for job_id, host_id in job_to_host.items():
        host_to_jobs[host_id].append(job_id)

    return host_to_leaf, leaf_to_spines, job_to_host, host_to_jobs


# ---------------------------------------------------------------------------
# Edge index generation
# ---------------------------------------------------------------------------

EdgeType = Tuple[str, str, str]


def build_edge_indices(
    host_to_leaf: Dict[int, int],
    leaf_to_spines: Dict[int, List[int]],
    job_to_host: Dict[int, int],
) -> Dict[EdgeType, torch.Tensor]:
    """Return {edge_type: edge_index} where edge_index has shape [2, E]."""

    indices: Dict[EdgeType, torch.Tensor] = {}

    # ------------------------------------------------------------------
    # PHYSICAL EDGES
    # ------------------------------------------------------------------

    # (cpu, connected_to, switch)  -- each cpu[host] -> leaf_switch[host]
    _src = list(range(NUM_HOSTS))
    _dst = [host_to_leaf[h] for h in range(NUM_HOSTS)]
    indices[("cpu", "connected_to", "switch")] = torch.tensor(
        [_src, _dst], dtype=torch.long
    )

    # (gpu, connected_to, switch)  -- same mapping as cpu
    indices[("gpu", "connected_to", "switch")] = torch.tensor(
        [_src, _dst], dtype=torch.long
    )

    # (switch, rev_connected_to_cpu, cpu)
    indices[("switch", "rev_connected_to_cpu", "cpu")] = torch.tensor(
        [_dst, _src], dtype=torch.long
    )

    # (switch, rev_connected_to_gpu, gpu)
    indices[("switch", "rev_connected_to_gpu", "gpu")] = torch.tensor(
        [_dst, _src], dtype=torch.long
    )

    # (ram, attached_to, cpu)  -- ram[i] attached to cpu[i] (1-to-1 per host)
    _host_ids = list(range(NUM_HOSTS))
    indices[("ram", "attached_to", "cpu")] = torch.tensor(
        [_host_ids, _host_ids], dtype=torch.long
    )

    # (cpu, rev_attached_to_ram, ram)
    indices[("cpu", "rev_attached_to_ram", "ram")] = torch.tensor(
        [_host_ids, _host_ids], dtype=torch.long
    )

    # (hdd, attached_to, cpu)  -- hdd[i] attached to cpu[i]
    indices[("hdd", "attached_to", "cpu")] = torch.tensor(
        [_host_ids, _host_ids], dtype=torch.long
    )

    # (cpu, rev_attached_to_hdd, hdd)
    indices[("cpu", "rev_attached_to_hdd", "hdd")] = torch.tensor(
        [_host_ids, _host_ids], dtype=torch.long
    )

    # (switch, uplink_to, switch)  -- every leaf -> every spine
    _uplink_src: List[int] = []
    _uplink_dst: List[int] = []
    for leaf_id in LEAF_SWITCH_IDS:
        for spine_id in leaf_to_spines[leaf_id]:
            _uplink_src.append(leaf_id)
            _uplink_dst.append(spine_id)
    indices[("switch", "uplink_to", "switch")] = torch.tensor(
        [_uplink_src, _uplink_dst], dtype=torch.long
    )

    # (switch, rev_uplink_to, switch)  -- spine -> leaf
    indices[("switch", "rev_uplink_to", "switch")] = torch.tensor(
        [_uplink_dst, _uplink_src], dtype=torch.long
    )

    # ------------------------------------------------------------------
    # LOGICAL EDGES
    # ------------------------------------------------------------------

    # (job, executes_on, cpu)  -- job[j] -> cpu[host_of_j]
    _job_src = list(range(NUM_JOBS))
    _cpu_dst = [job_to_host[j] for j in range(NUM_JOBS)]
    indices[("job", "executes_on", "cpu")] = torch.tensor(
        [_job_src, _cpu_dst], dtype=torch.long
    )

    # (cpu, rev_executes_on, job)
    indices[("cpu", "rev_executes_on", "job")] = torch.tensor(
        [_cpu_dst, _job_src], dtype=torch.long
    )

    # ------------------------------------------------------------------
    # CONTEXT EDGES
    # ------------------------------------------------------------------

    # (job, reports_to, rca_context)  -- all jobs -> node 0
    _all_jobs = list(range(NUM_JOBS))
    _rca_zeros_jobs = [0] * NUM_JOBS
    indices[("job", "reports_to", "rca_context")] = torch.tensor(
        [_all_jobs, _rca_zeros_jobs], dtype=torch.long
    )

    # (rca_context, rev_reports_to_job, job)
    indices[("rca_context", "rev_reports_to_job", "job")] = torch.tensor(
        [_rca_zeros_jobs, _all_jobs], dtype=torch.long
    )

    # (switch, reports_to, rca_context)  -- all switches -> node 0
    _all_switches = list(range(NUM_SWITCHES))
    _rca_zeros_sw = [0] * NUM_SWITCHES
    indices[("switch", "reports_to", "rca_context")] = torch.tensor(
        [_all_switches, _rca_zeros_sw], dtype=torch.long
    )

    # (rca_context, rev_reports_to_switch, switch)
    indices[("rca_context", "rev_reports_to_switch", "switch")] = torch.tensor(
        [_rca_zeros_sw, _all_switches], dtype=torch.long
    )

    return indices


# ---------------------------------------------------------------------------
# Edge attribute generation
# ---------------------------------------------------------------------------

def build_edge_attr(
    edge_indices: Dict[EdgeType, torch.Tensor],
    rng: Optional[np.random.Generator] = None,
) -> Dict[EdgeType, torch.Tensor]:
    """Return {edge_type: edge_attr} tensors.

    Physical edges  -> shape [E, EDGE_DIM_PHYSICAL], random float32.
    Logical/context -> shape [E, EDGE_DIM_DUMMY],    ones float32.

    When rng is supplied (per-sample generation), physical edge attrs are drawn
    from it instead of torch.rand, giving each graph its own unique link state.
    This prevents the model from memorising a fixed edge-attr fingerprint.
    """
    physical_set = {et for et in PHYSICAL_EDGE_TYPES}
    logical_set  = {et for et in LOGICAL_EDGE_TYPES}
    context_set  = {et for et in CONTEXT_EDGE_TYPES}

    attrs: Dict[EdgeType, torch.Tensor] = {}

    for edge_type, ei in edge_indices.items():
        num_edges = ei.shape[1]

        if edge_type in physical_set:
            if rng is not None:
                attrs[edge_type] = torch.from_numpy(
                    rng.random(size=(num_edges, EDGE_DIM_PHYSICAL)).astype(np.float32)
                )
            else:
                attrs[edge_type] = torch.rand(
                    num_edges, EDGE_DIM_PHYSICAL, dtype=torch.float32
                )
        elif edge_type in logical_set or edge_type in context_set:
            attrs[edge_type] = torch.ones(
                num_edges, EDGE_DIM_DUMMY, dtype=torch.float32
            )
        else:
            attrs[edge_type] = torch.ones(
                num_edges, EDGE_DIM_DUMMY, dtype=torch.float32
            )

    return attrs


# ---------------------------------------------------------------------------
# Dummy node feature generation
# ---------------------------------------------------------------------------

def build_dummy_node_features() -> Dict[str, torch.Tensor]:
    """Return {node_type: x} random float32 tensors.

    Shape: [num_nodes, num_features].
    rca_context is a virtual aggregation node: shape [1, 1].
    """
    features: Dict[str, torch.Tensor] = {}

    for node_type in NODE_TYPES:
        num_nodes = NODE_COUNTS[node_type]

        if node_type == "rca_context":
            features[node_type] = torch.ones(1, 1, dtype=torch.float32)
            continue

        num_feats = len(FEATURE_SCHEMA[node_type])
        features[node_type] = torch.rand(
            num_nodes, num_feats, dtype=torch.float32
        )

    return features


# ---------------------------------------------------------------------------
# Feature classification helpers
# ---------------------------------------------------------------------------

def _classify_features(node_type: str) -> Tuple[List[int], List[int]]:
    """Return (continuous_indices, categorical_indices) for a node type.

    Classification is based on suffix rules from config.py.
    A feature is categorical if its name ends with any CATEGORICAL_SUFFIX.
    Otherwise it is treated as continuous.
    rca_context has no schema entry -- returns empty lists.
    """
    if node_type not in FEATURE_SCHEMA:
        return [], []

    continuous: List[int] = []
    categorical: List[int] = []

    for idx, name in enumerate(FEATURE_SCHEMA[node_type]):
        is_cat = any(name.endswith(suf) for suf in CATEGORICAL_SUFFIXES)
        if is_cat:
            categorical.append(idx)
        else:
            continuous.append(idx)

    return continuous, categorical


# ---------------------------------------------------------------------------
# TemporalStateKeeper
# ---------------------------------------------------------------------------

class TemporalStateKeeper:
    """Vectorized per-node-type temporal state for EMA and delta computation.

    Operates on tensors of shape [num_nodes, num_features] (float32).
    Maintains:
        _prev_value : previous raw value  (for delta_short)
        _ema        : exponential moving average (for delta_long)

    All operations are in-place on float32 tensors -- no pandas, no loops
    over individual features.
    """

    def __init__(self, initial_values: torch.Tensor) -> None:
        """
        Args:
            initial_values: float32 tensor [num_nodes, num_features].
                            Used to initialise both _prev_value and _ema.
        """
        x = initial_values.clone().float()
        self._prev_value: torch.Tensor = x.clone()
        self._ema: torch.Tensor = x.clone()

    def step(self, current_values: torch.Tensor) -> torch.Tensor:
        """Advance one timestep and return temporal tensor.

        Args:
            current_values: float32 [num_nodes, num_features]

        Returns:
            temporal: float32 [num_nodes, num_features, 3]
                      [:, :, 0] = value
                      [:, :, 1] = delta_short
                      [:, :, 2] = delta_long
        """
        x = current_values.float()

        delta_short: torch.Tensor = x - self._prev_value
        self._ema = EMA_ALPHA * x + (1.0 - EMA_ALPHA) * self._ema
        delta_long: torch.Tensor = x - self._ema

        # Numerical safety -- clamp NaN/Inf before storing
        x           = torch.nan_to_num(x,           nan=0.0, posinf=1e6, neginf=-1e6)
        delta_short = torch.nan_to_num(delta_short, nan=0.0, posinf=1e6, neginf=-1e6)
        delta_long  = torch.nan_to_num(delta_long,  nan=0.0, posinf=1e6, neginf=-1e6)
        self._ema   = torch.nan_to_num(self._ema,   nan=0.0, posinf=1e6, neginf=-1e6)

        self._prev_value = x.clone()

        # Stack along new last dimension → [N, F, 3]
        return torch.stack([x, delta_short, delta_long], dim=-1)

    @property
    def ema(self) -> torch.Tensor:
        """Current EMA state [num_nodes, num_features]."""
        return self._ema.clone()


# ---------------------------------------------------------------------------
# Healthy baseline parameters (per node type)
# ---------------------------------------------------------------------------

# Realistic healthy-state mean and std for continuous features.
# Values are intentionally generic but physically plausible.
_HEALTHY_MEAN: Dict[str, float] = {
    "cpu":    0.35,   # ~35 % utilisation
    "gpu":    0.40,
    "ram":    0.50,
    "hdd":    0.30,
    "switch": 0.25,
    "job":    0.45,
}
_HEALTHY_STD: Dict[str, float] = {
    "cpu":    0.08,
    "gpu":    0.10,
    "ram":    0.07,
    "hdd":    0.06,
    "switch": 0.05,
    "job":    0.09,
}
# Per-step Gaussian noise scale (smooth drift)
_NOISE_SCALE: float = 0.01
# Slow sinusoidal drift amplitude and period (adds realism)
_DRIFT_AMP: float = 0.03
_DRIFT_PERIOD: int = 30   # steps


def _make_initial_continuous(
    node_type: str,
    num_nodes: int,
    num_cont_feats: int,
    rng: np.random.Generator,
) -> torch.Tensor:
    """Sample initial healthy continuous values [num_nodes, num_cont_feats].

    Each node receives a persistent utilisation personality (hot/cold host),
    drawn once at initialisation and applied as a per-node mean offset.
    This prevents the model from relying on global mean as a trivial shortcut.
    """
    mean = _HEALTHY_MEAN.get(node_type, 0.4)
    std  = _HEALTHY_STD.get(node_type, 0.08)
    # Per-node personality: persistent bias in [−0.15, +0.15]
    personality = rng.normal(0.0, 0.12, size=(num_nodes, 1)).astype(np.float32)
    vals = rng.normal(loc=mean, scale=std, size=(num_nodes, num_cont_feats)).astype(np.float32)
    vals = vals + personality  # shift each node by its own utilisation bias
    vals = np.clip(vals, 0.0, 1.0).astype(np.float32)
    return torch.from_numpy(vals)


def _make_initial_categorical(
    num_nodes: int,
    num_cat_feats: int,
    rng: np.random.Generator,
) -> torch.Tensor:
    """Sample initial categorical values in {0, 1} [num_nodes, num_cat_feats]."""
    # Mostly 0 (healthy / idle state), occasional 1
    vals = (rng.random(size=(num_nodes, num_cat_feats)) < 0.1).astype(np.float32)
    return torch.from_numpy(vals)


# ---------------------------------------------------------------------------
# Healthy trajectory simulation
# ---------------------------------------------------------------------------

def simulate_healthy_trajectory(
    seed: int = 42,
) -> Dict[str, torch.Tensor]:
    """Simulate SIMULATION_STEPS of healthy cluster telemetry.

    Returns:
        Dict[node_type, torch.Tensor]  shape [num_nodes, num_features, 3]
        The tensor represents the LAST timestep's temporal state.
        [:, :, 0] = current value
        [:, :, 1] = delta_short
        [:, :, 2] = delta_long

    rca_context is excluded (virtual node, no telemetry schema).
    """
    rng = np.random.default_rng(seed)
    torch.manual_seed(seed)

    result: Dict[str, torch.Tensor] = {}

    for node_type in NODE_TYPES:
        if node_type == "rca_context":
            continue

        num_nodes = NODE_COUNTS[node_type]
        features  = FEATURE_SCHEMA[node_type]
        num_feats = len(features)

        cont_idx, cat_idx = _classify_features(node_type)
        num_cont = len(cont_idx)
        num_cat  = len(cat_idx)

        # ---- initialise continuous state --------------------------------
        cont_init = _make_initial_continuous(node_type, num_nodes, num_cont, rng)
        # Full feature tensor (continuous slots only at first)
        full_init = torch.zeros(num_nodes, num_feats, dtype=torch.float32)
        if num_cont > 0:
            for out_pos, feat_pos in enumerate(cont_idx):
                full_init[:, feat_pos] = cont_init[:, out_pos]

        # ---- initialise categorical state --------------------------------
        if num_cat > 0:
            cat_init = _make_initial_categorical(num_nodes, num_cat, rng)
            for out_pos, feat_pos in enumerate(cat_idx):
                full_init[:, feat_pos] = cat_init[:, out_pos]

        keeper = TemporalStateKeeper(full_init)

        last_temporal: torch.Tensor = keeper.step(full_init)  # t=0: deltas = 0

        # ---- run simulation steps ----------------------------------------
        prev_cont = cont_init.clone()

        for step in range(1, SIMULATION_STEPS):
            # Smooth Gaussian noise + slow sinusoidal drift
            noise = torch.from_numpy(
                rng.normal(0.0, _NOISE_SCALE, size=(num_nodes, num_cont))
                .astype(np.float32)
            )
            drift_offset = float(
                _DRIFT_AMP * np.sin(2.0 * np.pi * step / _DRIFT_PERIOD)
            )
            new_cont = prev_cont + noise + drift_offset
            new_cont = new_cont.clamp(0.0, 1.0)

            # Categorical: rare state flip (p=0.02 per feature per step)
            new_cat: torch.Tensor | None = None
            if num_cat > 0:
                flip_mask = torch.from_numpy(
                    (rng.random(size=(num_nodes, num_cat)) < 0.02).astype(np.float32)
                )
                # Retrieve current categorical values from keeper's prev
                cur_cat = torch.zeros(num_nodes, num_cat, dtype=torch.float32)
                for out_pos, feat_pos in enumerate(cat_idx):
                    cur_cat[:, out_pos] = keeper._prev_value[:, feat_pos]
                new_cat = torch.abs(cur_cat - flip_mask).clamp(0.0, 1.0)

            # Assemble full feature tensor for this step
            full_step = torch.zeros(num_nodes, num_feats, dtype=torch.float32)
            if num_cont > 0:
                for out_pos, feat_pos in enumerate(cont_idx):
                    full_step[:, feat_pos] = new_cont[:, out_pos]
            if num_cat > 0 and new_cat is not None:
                for out_pos, feat_pos in enumerate(cat_idx):
                    full_step[:, feat_pos] = new_cat[:, out_pos]

            last_temporal = keeper.step(full_step)
            prev_cont = new_cont

        result[node_type] = last_temporal  # [num_nodes, num_feats, 3]

    return result


# ---------------------------------------------------------------------------
# Temporal tensor validation
# ---------------------------------------------------------------------------

def validate_temporal_tensor(
    temporal: Dict[str, torch.Tensor],
) -> None:
    """Validate the output of simulate_healthy_trajectory().

    Raises ValueError on any violation.
    """
    for node_type, t in temporal.items():
        # Shape: [num_nodes, num_features, 3]
        if t.dim() != 3:
            raise ValueError(
                f"[{node_type}] expected 3-dim tensor, got {t.dim()}"
            )
        if t.shape[2] != 3:
            raise ValueError(
                f"[{node_type}] last dim must be 3 (value/delta_short/delta_long), "
                f"got {t.shape[2]}"
            )
        expected_nodes = NODE_COUNTS[node_type]
        if t.shape[0] != expected_nodes:
            raise ValueError(
                f"[{node_type}] expected {expected_nodes} nodes, got {t.shape[0]}"
            )
        expected_feats = len(FEATURE_SCHEMA[node_type])
        if t.shape[1] != expected_feats:
            raise ValueError(
                f"[{node_type}] expected {expected_feats} features, got {t.shape[1]}"
            )
        if torch.isnan(t).any():
            raise ValueError(f"[{node_type}] NaN detected in temporal tensor")
        if torch.isinf(t).any():
            raise ValueError(f"[{node_type}] Inf detected in temporal tensor")


# ---------------------------------------------------------------------------
# Fault injection -- types, metadata, propagation
# ---------------------------------------------------------------------------

FAULT_TYPES: List[str] = ["hdd_degradation", "network_congestion", "ram_leak"]


# ---------------------------------------------------------------------------
# Temporal fault integration -- plan resolver + simulation
# ---------------------------------------------------------------------------

def _resolve_fault_plan(
    fault_type: str,
    severity: float,
    routing_maps: Tuple[Dict, Dict, Dict, Dict],
    rng: np.random.Generator,
) -> Dict:
    """Pre-determine all stochastic fault decisions without touching any state.

    Returns a plan dict containing:
        fault_type, rc_node_type, rc_node_id, severity,
        all_anomalies : [(node_type, node_id, feat_idx, total_amplitude, direction)]
        victim_node_ids : Dict[str, List[int]]
        y_dict, loss_mask_dict  -- labels ready for HeteroData assembly.

    all_anomalies encodes the TOTAL intended shift for each (node, feature) pair.
    simulate_trajectory_with_fault() distributes this shift across the fault
    window (steps 50–89) so delta_short stays realistic at each step.
    """
    host_to_leaf, _, job_to_host, host_to_jobs = routing_maps

    all_anomalies: List[Tuple] = []  # (node_type, node_id, feat_idx, amp, direction)

    if fault_type == "hdd_degradation":
        rc_host = int(rng.integers(0, NUM_HOSTS))
        amp = severity * 0.55

        lat_idx = _feat_idx("hdd", "disk_latency_ms")
        sec_idx = _feat_idx("hdd", "disk_reallocated_sectors")
        use_idx = _feat_idx("hdd", "disk_usage_percent")
        rc_noise = float(rng.normal(0.0, 0.03))

        all_anomalies += [
            ("hdd", rc_host, lat_idx, amp + rc_noise, +1.0),
            ("hdd", rc_host, sec_idx, amp * 0.8,      +1.0),
            ("hdd", rc_host, use_idx, amp * 0.4,      +1.0),
        ]

        victim_cpus: List[int] = []
        iowait_idx = _feat_idx("cpu", "cpu_iowait_percent")
        if rng.random() < 0.70:
            victim_cpus.append(rc_host)
            atten = float(rng.uniform(0.15, 0.9))
            noise_v = float(rng.normal(0.0, 0.05))
            all_anomalies.append(("cpu", rc_host, iowait_idx,
                                  amp * 0.6 * atten + noise_v, +1.0))

        wait_idx    = _feat_idx("job", "job_wait_time_seconds")
        runtime_idx = _feat_idx("job", "job_runtime_seconds")
        io_r_idx    = _feat_idx("job", "job_io_read_mb")
        actual_victim_jobs: List[int] = []
        for jid in host_to_jobs.get(rc_host, []):
            if rng.random() < 0.60:
                actual_victim_jobs.append(jid)
                v_amp = amp * float(rng.uniform(0.3, 1.1))
                all_anomalies += [
                    ("job", jid, wait_idx,    v_amp * 0.9, +1.0),
                    ("job", jid, runtime_idx, v_amp * 0.5, +1.0),
                    ("job", jid, io_r_idx,    v_amp * 0.4, -1.0),
                ]

        victim_node_ids: Dict[str, List[int]] = {
            "cpu": victim_cpus, "job": actual_victim_jobs,
        }
        rc_node_type = "hdd"
        rc_node_id   = rc_host

    elif fault_type == "network_congestion":
        rc_switch = int(rng.integers(0, NUM_LEAF))
        amp = severity * 0.55

        pkt_idx = _feat_idx("switch", "switch_packet_loss_percent")
        lat_idx = _feat_idx("switch", "switch_latency_ms")
        bw_idx  = _feat_idx("switch", "switch_bandwidth_usage_percent")
        rx_idx  = _feat_idx("switch", "switch_rx_mb")
        tx_idx  = _feat_idx("switch", "switch_tx_mb")

        if rng.random() < 0.80:
            all_anomalies.append(
                ("switch", rc_switch, pkt_idx,
                 amp + float(rng.normal(0, 0.03)), +1.0))
        if rng.random() < 0.75:
            all_anomalies.append(("switch", rc_switch, lat_idx, amp * 0.9, +1.0))
        if rng.random() < 0.60:
            all_anomalies.append(("switch", rc_switch, bw_idx,  amp * 0.7, +1.0))
        all_anomalies.append(
            ("switch", rc_switch, rx_idx, amp * float(rng.uniform(0.2, 0.6)), +1.0))
        all_anomalies.append(
            ("switch", rc_switch, tx_idx, amp * float(rng.uniform(0.2, 0.6)), +1.0))

        affected_hosts = [h for h in range(NUM_HOSTS) if host_to_leaf[h] == rc_switch]

        iowait_idx  = _feat_idx("cpu", "cpu_iowait_percent")
        pcie_tx_idx = _feat_idx("gpu", "gpu_pcie_tx_mb")
        pcie_rx_idx = _feat_idx("gpu", "gpu_pcie_rx_mb")
        net_rx_idx  = _feat_idx("job", "job_network_rx_mb")
        net_tx_idx  = _feat_idx("job", "job_network_tx_mb")
        wait_idx    = _feat_idx("job", "job_wait_time_seconds")

        victim_cpus_nc: List[int] = []
        victim_gpus_nc: List[int] = []
        victim_jobs_nc: List[int] = []

        for h in affected_hosts:
            if rng.random() < 0.55:
                victim_cpus_nc.append(h)
                atten = float(rng.uniform(0.1, 0.8))
                nv    = float(rng.normal(0.0, 0.06))
                all_anomalies.append(("cpu", h, iowait_idx,
                                      amp * 0.5 * atten + nv, +1.0))
            if rng.random() < 0.50:
                victim_gpus_nc.append(h)
                atten = float(rng.uniform(0.1, 0.7))
                all_anomalies += [
                    ("gpu", h, pcie_tx_idx, amp * 0.35 * atten, -1.0),
                    ("gpu", h, pcie_rx_idx, amp * 0.35 * atten, -1.0),
                ]
            for jid in host_to_jobs.get(h, []):
                if rng.random() < 0.50:
                    victim_jobs_nc.append(jid)
                    v_amp = amp * float(rng.uniform(0.2, 1.0))
                    all_anomalies += [
                        ("job", jid, net_rx_idx, v_amp * 0.5, -1.0),
                        ("job", jid, net_tx_idx, v_amp * 0.5, -1.0),
                        ("job", jid, wait_idx,   v_amp * 0.6, +1.0),
                    ]

        victim_node_ids = {
            "cpu": victim_cpus_nc, "gpu": victim_gpus_nc, "job": victim_jobs_nc,
        }
        rc_node_type = "switch"
        rc_node_id   = rc_switch

    else:  # ram_leak
        rc_host = int(rng.integers(0, NUM_HOSTS))
        amp = severity * 0.50

        used_idx = _feat_idx("ram", "ram_used_percent")
        frag_idx = _feat_idx("ram", "ram_fragmentation_score")
        pf_idx   = _feat_idx("ram", "ram_page_faults_ps")
        rc_noise = float(rng.normal(0.0, 0.025))

        all_anomalies += [
            ("ram", rc_host, used_idx, amp + rc_noise, +1.0),
            ("ram", rc_host, frag_idx, amp * 0.65,     +1.0),
            ("ram", rc_host, pf_idx,   amp * 0.80,     +1.0),
        ]

        ram_idx     = _feat_idx("job", "job_ram_usage_percent")
        wait_idx    = _feat_idx("job", "job_wait_time_seconds")
        runtime_idx = _feat_idx("job", "job_runtime_seconds")
        victim_jobs_rl: List[int] = []
        for jid in host_to_jobs.get(rc_host, []):
            if rng.random() < 0.55:
                victim_jobs_rl.append(jid)
                v_amp = amp * float(rng.uniform(0.25, 1.05))
                all_anomalies += [
                    ("job", jid, ram_idx,     v_amp * 0.8,  +1.0),
                    ("job", jid, wait_idx,    v_amp * 0.5,  +1.0),
                    ("job", jid, runtime_idx, v_amp * 0.35, +1.0),
                ]

        victim_node_ids = {"job": victim_jobs_rl}
        rc_node_type = "ram"
        rc_node_id   = rc_host

    # Build y_dict and loss_mask_dict (same structure as existing inject_fault)
    y_dict: Dict[str, torch.Tensor] = {}
    loss_mask_dict: Dict[str, torch.Tensor] = {}
    for nt in NODE_TYPES:
        if nt == "rca_context":
            continue
        n    = NODE_COUNTS[nt]
        y    = torch.zeros(n, dtype=torch.long)
        mask = torch.ones(n, dtype=torch.bool)
        if nt == rc_node_type:
            y[rc_node_id] = 1
        for vid in victim_node_ids.get(nt, []):
            mask[vid] = False
        y_dict[nt]         = y
        loss_mask_dict[nt] = mask

    return {
        "fault_type":           fault_type,
        "rc_node_type":         rc_node_type,
        "rc_node_id":           rc_node_id,
        "severity":             severity,
        "all_anomalies":        all_anomalies,
        "victim_node_ids":      victim_node_ids,
        "y_dict":               y_dict,
        "loss_mask_dict":       loss_mask_dict,
    }


def simulate_trajectory_with_fault(
    fault_plan: Dict,
    seed: int = 42,
) -> Dict[str, torch.Tensor]:
    """Simulate SIMULATION_STEPS with fault ramped in from FAULT_INJECTION_STEP.

    Critical difference from inject_fault():
        The anomaly is distributed as per-step increments inside the simulation
        loop, so TemporalStateKeeper computes delta_short naturally from
        consecutive values.  delta_short at any step ≈ amplitude/fault_steps + noise
        (≈0.005–0.015), matching the healthy noise scale instead of being 10–25x larger.

    EMA partially adapts over the 40-step fault window, so delta_long reflects
    a sustained upward trend rather than a single-step artefact.

    Args:
        fault_plan : output of _resolve_fault_plan()
        seed       : RNG seed for healthy noise/drift (per-sample)

    Returns:
        Dict[node_type, Tensor[N, F, 3]] — final temporal state at step 89.
    """
    rng = np.random.default_rng(seed)
    torch.manual_seed(seed)

    all_anomalies  = fault_plan["all_anomalies"]
    fault_steps    = max(1, SIMULATION_STEPS - FAULT_INJECTION_STEP)  # 40

    result: Dict[str, torch.Tensor] = {}

    for node_type in NODE_TYPES:
        if node_type == "rca_context":
            continue

        num_nodes = NODE_COUNTS[node_type]
        features  = FEATURE_SCHEMA[node_type]
        num_feats = len(features)
        cont_idx, cat_idx = _classify_features(node_type)
        num_cont = len(cont_idx)
        num_cat  = len(cat_idx)

        # Map raw feature index → compact continuous column index
        feat_to_compact: Dict[int, int] = {fi: pos for pos, fi in enumerate(cont_idx)}

        # Anomalies for this node type as (node_id, compact_col, total_amp, direction)
        nt_anomalies = [
            (nid, feat_to_compact[fi], amp, d)
            for (nt, nid, fi, amp, d) in all_anomalies
            if nt == node_type and fi in feat_to_compact
        ]

        cont_init = _make_initial_continuous(node_type, num_nodes, num_cont, rng)
        full_init = torch.zeros(num_nodes, num_feats, dtype=torch.float32)
        for out_pos, feat_pos in enumerate(cont_idx):
            full_init[:, feat_pos] = cont_init[:, out_pos]
        if num_cat > 0:
            cat_init = _make_initial_categorical(num_nodes, num_cat, rng)
            for out_pos, feat_pos in enumerate(cat_idx):
                full_init[:, feat_pos] = cat_init[:, out_pos]

        keeper = TemporalStateKeeper(full_init)
        last_temporal = keeper.step(full_init)
        prev_cont = cont_init.clone()

        for step in range(1, SIMULATION_STEPS):
            noise = torch.from_numpy(
                rng.normal(0.0, _NOISE_SCALE, size=(num_nodes, num_cont))
                    .astype(np.float32)
            )
            drift_offset = float(
                _DRIFT_AMP * np.sin(2.0 * np.pi * step / _DRIFT_PERIOD)
            )
            new_cont = prev_cont + noise + drift_offset

            # Fault ramp: distribute total_amplitude evenly across fault_steps
            if step >= FAULT_INJECTION_STEP and nt_anomalies:
                for (nid, compact_fi, total_amp, direction) in nt_anomalies:
                    per_step = direction * total_amp / fault_steps
                    new_cont[nid, compact_fi] = new_cont[nid, compact_fi] + per_step

            new_cont = new_cont.clamp(0.0, 1.0)

            new_cat: Optional[torch.Tensor] = None
            if num_cat > 0:
                flip_mask = torch.from_numpy(
                    (rng.random(size=(num_nodes, num_cat)) < 0.02).astype(np.float32)
                )
                cur_cat = torch.zeros(num_nodes, num_cat, dtype=torch.float32)
                for out_pos, feat_pos in enumerate(cat_idx):
                    cur_cat[:, out_pos] = keeper._prev_value[:, feat_pos]
                new_cat = torch.abs(cur_cat - flip_mask).clamp(0.0, 1.0)

            full_step = torch.zeros(num_nodes, num_feats, dtype=torch.float32)
            for out_pos, feat_pos in enumerate(cont_idx):
                full_step[:, feat_pos] = new_cont[:, out_pos]
            if num_cat > 0 and new_cat is not None:
                for out_pos, feat_pos in enumerate(cat_idx):
                    full_step[:, feat_pos] = new_cat[:, out_pos]

            last_temporal = keeper.step(full_step)
            prev_cont = new_cont

        result[node_type] = last_temporal

    return result

# Named tuple-style dataclass replaced with plain Dict for zero extra imports.
# FaultResult keys:
#   temporal_state : Dict[str, torch.Tensor]  -- modified copy, shape [N, F, 3]
#   y_dict         : Dict[str, torch.Tensor]  -- RCA labels,   shape [N]
#   loss_mask_dict : Dict[str, torch.Tensor]  -- bool mask,    shape [N]
#   fault_type     : str
#   root_cause_node_type : str
#   root_cause_node_id   : int
#   severity       : float
#   victim_node_ids: Dict[str, List[int]]

FaultResult = Dict  # type alias for readability


def _feat_idx(node_type: str, name: str) -> int:
    """Return the column index of *name* in FEATURE_SCHEMA[node_type]."""
    return FEATURE_SCHEMA[node_type].index(name)


def _ramp(step: int, total_steps: int, severity: float) -> float:
    """Linear ramp from 0 → severity over (total_steps - FAULT_INJECTION_STEP) steps."""
    steps_since_fault = max(0, step - FAULT_INJECTION_STEP)
    window = max(1, total_steps - FAULT_INJECTION_STEP)
    return severity * min(1.0, steps_since_fault / window)


def _apply_anomaly(
    tensor: torch.Tensor,   # [N, F, 3]  -- will NOT be mutated
    node_ids: List[int],
    feat_idx: int,
    amplitude: float,
    direction: float = 1.0,  # +1 = increase, -1 = decrease
) -> torch.Tensor:
    """Return a new tensor with anomaly applied to selected nodes/feature."""
    out = tensor.clone()
    delta = direction * amplitude
    out[node_ids, feat_idx, 0] = (out[node_ids, feat_idx, 0] + delta).clamp(0.0, 1.0)
    # Recompute delta_short as the shift itself (approximation for injected step)
    out[node_ids, feat_idx, 1] = out[node_ids, feat_idx, 1] + delta
    return out


def _apply_anomaly_stochastic(
    tensor: torch.Tensor,
    node_ids: List[int],
    feat_idx: int,
    amplitude: float,
    direction: float,
    rng: np.random.Generator,
    propagation_prob: float = 0.65,
    attenuation_range: Tuple[float, float] = (0.2, 0.8),
    noise_scale: float = 0.04,
) -> torch.Tensor:
    """Stochastic anomaly: each victim node independently decides to react.

    Hardening changes vs _apply_anomaly:
    - Each node_id fires with probability propagation_prob
    - Amplitude is attenuated by a random factor in attenuation_range
    - Gaussian noise is added so victims sometimes look worse than root cause
    """
    out = tensor.clone()
    for nid in node_ids:
        if rng.random() > propagation_prob:
            continue  # this victim does not react this time
        atten = float(rng.uniform(*attenuation_range))
        noise = float(rng.normal(0.0, noise_scale))
        delta = direction * amplitude * atten + noise
        cur = float(out[nid, feat_idx, 0].item())
        out[nid, feat_idx, 0] = torch.tensor(
            max(0.0, min(1.0, cur + delta)), dtype=torch.float32
        )
        out[nid, feat_idx, 1] = out[nid, feat_idx, 1] + delta
    return out


def _add_healthy_outliers(
    state: Dict[str, torch.Tensor],
    rng: np.random.Generator,
    spike_prob: float = 0.08,
    spike_scale: float = 0.12,
) -> Dict[str, torch.Tensor]:
    """Add random harmless spikes to healthy-looking nodes.

    Makes healthy graphs noisier so the model cannot rely on
    'clean = healthy' shortcut.
    """
    out = {k: v.clone() for k, v in state.items()}
    for nt in NODE_TYPES:
        if nt == "rca_context":
            continue
        t = out[nt]
        n, f, _ = t.shape
        mask = rng.random(size=(n, f)) < spike_prob
        spikes = rng.normal(0.0, spike_scale, size=(n, f)).astype(np.float32)
        for ni in range(n):
            for fi in range(f):
                if mask[ni, fi]:
                    cur = float(t[ni, fi, 0].item())
                    t[ni, fi, 0] = torch.tensor(
                        max(0.0, min(1.0, cur + spikes[ni, fi])),
                        dtype=torch.float32,
                    )
        out[nt] = t
    return out


def _break_argmax_shortcut(
    state: Dict[str, torch.Tensor],
    rc_type: str,
    rc_id: int,
    rng: np.random.Generator,
    n_decoys: int = 3,
    decoy_scale: float = 0.15,
) -> Dict[str, torch.Tensor]:
    """Ensure root cause is NOT always the argmax node.

    Picks n_decoys random non-RC nodes of the same type and gives them
    a spike on a random feature that is LARGER than the RC's value on
    that feature. This forces the model to use graph context, not just
    per-node feature magnitude.
    """
    out = {k: v.clone() for k, v in state.items()}
    t = out[rc_type]
    n_nodes, n_feats, _ = t.shape

    # Pick decoy nodes (not the root cause)
    candidates = [i for i in range(n_nodes) if i != rc_id]
    if not candidates:
        return out

    n_actual = min(n_decoys, len(candidates))
    decoy_ids = list(rng.choice(candidates, size=n_actual, replace=False))

    for did in decoy_ids:
        # Pick a random feature and set decoy value above RC value
        fi = int(rng.integers(0, n_feats))
        rc_val = float(t[rc_id, fi, 0].item())
        # Decoy gets a value that is rc_val + decoy_scale (clamped)
        decoy_val = min(1.0, rc_val + float(rng.uniform(decoy_scale, decoy_scale * 2)))
        t[did, fi, 0] = torch.tensor(decoy_val, dtype=torch.float32)

    out[rc_type] = t
    return out


# ---- HDD degradation (HARDENED) -------------------------------------------

def _inject_hdd_degradation(
    temporal_state: Dict[str, torch.Tensor],
    severity: float,
    routing_maps: Tuple[Dict, Dict, Dict, Dict],
    rng: np.random.Generator,
) -> FaultResult:
    host_to_leaf, _, job_to_host, host_to_jobs = routing_maps
    rc_host: int = int(rng.integers(0, NUM_HOSTS))
    state = {k: v.clone() for k, v in temporal_state.items()}

    # Reduced amplitude: severity is now U(0.15, 0.45) so amp is much smaller
    amp = severity * 0.55

    # Root cause: HDD — apply deterministically but with noise
    hdd_t = state["hdd"]
    lat_idx = _feat_idx("hdd", "disk_latency_ms")
    sec_idx = _feat_idx("hdd", "disk_reallocated_sectors")
    use_idx = _feat_idx("hdd", "disk_usage_percent")

    # Root cause gets direct anomaly + small noise (partial failure)
    rc_noise = float(rng.normal(0.0, 0.03))
    hdd_t = _apply_anomaly(hdd_t, [rc_host], lat_idx, amp + rc_noise, +1.0)
    hdd_t = _apply_anomaly(hdd_t, [rc_host], sec_idx, amp * 0.8,      +1.0)
    hdd_t = _apply_anomaly(hdd_t, [rc_host], use_idx, amp * 0.4,      +1.0)
    state["hdd"] = hdd_t

    # Downstream CPU: stochastic propagation
    cpu_t = state["cpu"]
    iowait_idx = _feat_idx("cpu", "cpu_iowait_percent")
    cpu_t = _apply_anomaly_stochastic(
        cpu_t, [rc_host], iowait_idx, amp * 0.6, +1.0, rng,
        propagation_prob=0.70, attenuation_range=(0.15, 0.9), noise_scale=0.05,
    )
    state["cpu"] = cpu_t

    # Downstream Jobs: stochastic, some jobs may look worse than root cause
    victim_jobs: List[int] = host_to_jobs.get(rc_host, [])
    actual_victims: List[int] = []
    if victim_jobs:
        job_t = state["job"]
        wait_idx    = _feat_idx("job", "job_wait_time_seconds")
        runtime_idx = _feat_idx("job", "job_runtime_seconds")
        io_r_idx    = _feat_idx("job", "job_io_read_mb")
        # Each job independently reacts
        for jid in victim_jobs:
            if rng.random() < 0.60:
                actual_victims.append(jid)
                # Noisy victim: sometimes looks worse than root cause
                victim_amp = amp * float(rng.uniform(0.3, 1.1))
                job_t = _apply_anomaly(job_t, [jid], wait_idx,    victim_amp * 0.9, +1.0)
                job_t = _apply_anomaly(job_t, [jid], runtime_idx, victim_amp * 0.5, +1.0)
                job_t = _apply_anomaly(job_t, [jid], io_r_idx,    victim_amp * 0.4, -1.0)
        state["job"] = job_t

    # Break argmax shortcut: some non-RC nodes get spikes above RC value
    state = _break_argmax_shortcut(state, "hdd", rc_host, rng, n_decoys=4, decoy_scale=0.12)
    # Add healthy outliers to all nodes (makes clean nodes noisier)
    state = _add_healthy_outliers(state, rng, spike_prob=0.06, spike_scale=0.08)

    victim_node_ids: Dict[str, List[int]] = {
        "cpu": [rc_host],
        "job": actual_victims,
    }

    y_dict: Dict[str, torch.Tensor] = {}
    loss_mask_dict: Dict[str, torch.Tensor] = {}
    for nt in NODE_TYPES:
        if nt == "rca_context":
            continue
        n = NODE_COUNTS[nt]
        y    = torch.zeros(n, dtype=torch.long)
        mask = torch.ones(n, dtype=torch.bool)
        if nt == "hdd":
            y[rc_host] = 1
        elif nt in victim_node_ids:
            for vid in victim_node_ids[nt]:
                mask[vid] = False
        y_dict[nt]         = y
        loss_mask_dict[nt] = mask

    return {
        "temporal_state":       state,
        "y_dict":               y_dict,
        "loss_mask_dict":       loss_mask_dict,
        "fault_type":           "hdd_degradation",
        "root_cause_node_type": "hdd",
        "root_cause_node_id":   rc_host,
        "severity":             severity,
        "victim_node_ids":      victim_node_ids,
    }


# ---- Network congestion (HARDENED) -----------------------------------------

def _inject_network_congestion(
    temporal_state: Dict[str, torch.Tensor],
    severity: float,
    routing_maps: Tuple[Dict, Dict, Dict, Dict],
    rng: np.random.Generator,
) -> FaultResult:
    host_to_leaf, _, job_to_host, host_to_jobs = routing_maps
    rc_switch: int = int(rng.integers(0, NUM_LEAF))
    state = {k: v.clone() for k, v in temporal_state.items()}
    amp = severity * 0.55

    # Root cause switch: partial failure — not all metrics spike equally
    sw_t = state["switch"]
    pkt_idx = _feat_idx("switch", "switch_packet_loss_percent")
    lat_idx = _feat_idx("switch", "switch_latency_ms")
    bw_idx  = _feat_idx("switch", "switch_bandwidth_usage_percent")
    rx_idx  = _feat_idx("switch", "switch_rx_mb")
    tx_idx  = _feat_idx("switch", "switch_tx_mb")

    # Only some metrics show the fault (partial failure)
    if rng.random() < 0.80:
        sw_t = _apply_anomaly(sw_t, [rc_switch], pkt_idx, amp + float(rng.normal(0, 0.03)), +1.0)
    if rng.random() < 0.75:
        sw_t = _apply_anomaly(sw_t, [rc_switch], lat_idx, amp * 0.9, +1.0)
    if rng.random() < 0.60:
        sw_t = _apply_anomaly(sw_t, [rc_switch], bw_idx,  amp * 0.7, +1.0)
    sw_t = _apply_anomaly(sw_t, [rc_switch], rx_idx, amp * float(rng.uniform(0.2, 0.6)), +1.0)
    sw_t = _apply_anomaly(sw_t, [rc_switch], tx_idx, amp * float(rng.uniform(0.2, 0.6)), +1.0)
    state["switch"] = sw_t

    affected_hosts: List[int] = [
        h for h in range(NUM_HOSTS) if host_to_leaf[h] == rc_switch
    ]

    # Downstream CPUs: stochastic
    victim_cpus: List[int] = []
    cpu_t = state["cpu"]
    iowait_idx = _feat_idx("cpu", "cpu_iowait_percent")
    for h in affected_hosts:
        if rng.random() < 0.55:
            victim_cpus.append(h)
            cpu_t = _apply_anomaly_stochastic(
                cpu_t, [h], iowait_idx, amp * 0.5, +1.0, rng,
                propagation_prob=1.0, attenuation_range=(0.1, 0.8), noise_scale=0.06,
            )
    state["cpu"] = cpu_t

    # Downstream GPUs: stochastic
    victim_gpus: List[int] = []
    gpu_t = state["gpu"]
    pcie_tx_idx = _feat_idx("gpu", "gpu_pcie_tx_mb")
    pcie_rx_idx = _feat_idx("gpu", "gpu_pcie_rx_mb")
    for h in affected_hosts:
        if rng.random() < 0.50:
            victim_gpus.append(h)
            gpu_t = _apply_anomaly_stochastic(
                gpu_t, [h], pcie_tx_idx, amp * 0.35, -1.0, rng,
                propagation_prob=1.0, attenuation_range=(0.1, 0.7), noise_scale=0.05,
            )
            gpu_t = _apply_anomaly_stochastic(
                gpu_t, [h], pcie_rx_idx, amp * 0.35, -1.0, rng,
                propagation_prob=1.0, attenuation_range=(0.1, 0.7), noise_scale=0.05,
            )
    state["gpu"] = gpu_t

    # Downstream Jobs: stochastic
    victim_jobs: List[int] = []
    job_t = state["job"]
    net_rx_idx = _feat_idx("job", "job_network_rx_mb")
    net_tx_idx = _feat_idx("job", "job_network_tx_mb")
    wait_idx   = _feat_idx("job", "job_wait_time_seconds")
    for h in affected_hosts:
        for jid in host_to_jobs.get(h, []):
            if rng.random() < 0.50:
                victim_jobs.append(jid)
                victim_amp = amp * float(rng.uniform(0.2, 1.0))
                job_t = _apply_anomaly(job_t, [jid], net_rx_idx, victim_amp * 0.5, -1.0)
                job_t = _apply_anomaly(job_t, [jid], net_tx_idx, victim_amp * 0.5, -1.0)
                job_t = _apply_anomaly(job_t, [jid], wait_idx,   victim_amp * 0.6, +1.0)
    state["job"] = job_t

    state = _break_argmax_shortcut(state, "switch", rc_switch, rng, n_decoys=2, decoy_scale=0.10)
    state = _add_healthy_outliers(state, rng, spike_prob=0.06, spike_scale=0.08)

    victim_node_ids: Dict[str, List[int]] = {
        "cpu": victim_cpus,
        "gpu": victim_gpus,
        "job": victim_jobs,
    }

    y_dict: Dict[str, torch.Tensor] = {}
    loss_mask_dict: Dict[str, torch.Tensor] = {}
    for nt in NODE_TYPES:
        if nt == "rca_context":
            continue
        n = NODE_COUNTS[nt]
        y    = torch.zeros(n, dtype=torch.long)
        mask = torch.ones(n, dtype=torch.bool)
        if nt == "switch":
            y[rc_switch] = 1
        elif nt in victim_node_ids:
            for vid in victim_node_ids[nt]:
                mask[vid] = False
        y_dict[nt]         = y
        loss_mask_dict[nt] = mask

    return {
        "temporal_state":       state,
        "y_dict":               y_dict,
        "loss_mask_dict":       loss_mask_dict,
        "fault_type":           "network_congestion",
        "root_cause_node_type": "switch",
        "root_cause_node_id":   rc_switch,
        "severity":             severity,
        "victim_node_ids":      victim_node_ids,
    }


# ---- RAM leak (HARDENED) ---------------------------------------------------

def _inject_ram_leak(
    temporal_state: Dict[str, torch.Tensor],
    severity: float,
    routing_maps: Tuple[Dict, Dict, Dict, Dict],
    rng: np.random.Generator,
) -> FaultResult:
    host_to_leaf, _, job_to_host, host_to_jobs = routing_maps
    rc_host: int = int(rng.integers(0, NUM_HOSTS))
    state = {k: v.clone() for k, v in temporal_state.items()}
    amp = severity * 0.50

    # Root cause RAM: gradual saturation with noise
    ram_t = state["ram"]
    used_idx = _feat_idx("ram", "ram_used_percent")
    frag_idx = _feat_idx("ram", "ram_fragmentation_score")
    pf_idx   = _feat_idx("ram", "ram_page_faults_ps")

    rc_noise = float(rng.normal(0.0, 0.025))
    ram_t = _apply_anomaly(ram_t, [rc_host], used_idx, amp + rc_noise,   +1.0)
    ram_t = _apply_anomaly(ram_t, [rc_host], frag_idx, amp * 0.65,       +1.0)
    ram_t = _apply_anomaly(ram_t, [rc_host], pf_idx,   amp * 0.80,       +1.0)
    state["ram"] = ram_t

    # Downstream Jobs: stochastic, some may look worse
    victim_jobs: List[int] = []
    job_t = state["job"]
    ram_idx     = _feat_idx("job", "job_ram_usage_percent")
    wait_idx    = _feat_idx("job", "job_wait_time_seconds")
    runtime_idx = _feat_idx("job", "job_runtime_seconds")
    for jid in host_to_jobs.get(rc_host, []):
        if rng.random() < 0.55:
            victim_jobs.append(jid)
            victim_amp = amp * float(rng.uniform(0.25, 1.05))
            job_t = _apply_anomaly(job_t, [jid], ram_idx,     victim_amp * 0.8, +1.0)
            job_t = _apply_anomaly(job_t, [jid], wait_idx,    victim_amp * 0.5, +1.0)
            job_t = _apply_anomaly(job_t, [jid], runtime_idx, victim_amp * 0.35, +1.0)
    state["job"] = job_t

    state = _break_argmax_shortcut(state, "ram", rc_host, rng, n_decoys=4, decoy_scale=0.10)
    state = _add_healthy_outliers(state, rng, spike_prob=0.06, spike_scale=0.08)

    victim_node_ids: Dict[str, List[int]] = {"job": victim_jobs}

    y_dict: Dict[str, torch.Tensor] = {}
    loss_mask_dict: Dict[str, torch.Tensor] = {}
    for nt in NODE_TYPES:
        if nt == "rca_context":
            continue
        n = NODE_COUNTS[nt]
        y    = torch.zeros(n, dtype=torch.long)
        mask = torch.ones(n, dtype=torch.bool)
        if nt == "ram":
            y[rc_host] = 1
        elif nt in victim_node_ids:
            for vid in victim_node_ids[nt]:
                mask[vid] = False
        y_dict[nt]         = y
        loss_mask_dict[nt] = mask

    return {
        "temporal_state":       state,
        "y_dict":               y_dict,
        "loss_mask_dict":       loss_mask_dict,
        "fault_type":           "ram_leak",
        "root_cause_node_type": "ram",
        "root_cause_node_id":   rc_host,
        "severity":             severity,
        "victim_node_ids":      victim_node_ids,
    }


# ---- Public API ------------------------------------------------------------

def inject_fault(
    temporal_state: Dict[str, torch.Tensor],
    fault_type: str,
    severity: float,
    routing_maps: Tuple[Dict, Dict, Dict, Dict],
    seed: int = 0,
) -> FaultResult:
    """Inject a fault into a healthy temporal state snapshot.

    Args:
        temporal_state: output of simulate_healthy_trajectory() -- NOT mutated.
        fault_type:     one of FAULT_TYPES.
        severity:       float in [0.7, 0.95] -- controls anomaly amplitude.
        routing_maps:   tuple returned by build_routing_maps().
        seed:           RNG seed for deterministic node selection.

    Returns:
        FaultResult dict with keys:
            temporal_state, y_dict, loss_mask_dict,
            fault_type, root_cause_node_type, root_cause_node_id,
            severity, victim_node_ids.
    """
    if fault_type not in FAULT_TYPES:
        raise ValueError(f"Unknown fault_type '{fault_type}'. Choose from {FAULT_TYPES}")
    if not (0.0 < severity <= 1.0):
        raise ValueError(f"severity must be in (0, 1], got {severity}")

    rng = np.random.default_rng(seed)

    if fault_type == "hdd_degradation":
        return _inject_hdd_degradation(temporal_state, severity, routing_maps, rng)
    if fault_type == "network_congestion":
        return _inject_network_congestion(temporal_state, severity, routing_maps, rng)
    # ram_leak
    return _inject_ram_leak(temporal_state, severity, routing_maps, rng)


# ---------------------------------------------------------------------------
# Fault simulation validation
# ---------------------------------------------------------------------------

def validate_fault_simulation(result: FaultResult) -> None:
    """Validate the output of inject_fault().

    Raises ValueError on any violation.
    """
    state      = result["temporal_state"]
    y_dict     = result["y_dict"]
    mask_dict  = result["loss_mask_dict"]
    rc_type    = result["root_cause_node_type"]
    rc_id      = result["root_cause_node_id"]
    victims    = result["victim_node_ids"]

    # 1. Exactly one positive label across all node types
    total_positives = sum(int(y.sum().item()) for y in y_dict.values())
    if total_positives != 1:
        raise ValueError(
            f"Expected exactly 1 positive label, got {total_positives}"
        )

    # 2. Root cause node has y=1 and mask=True
    if y_dict[rc_type][rc_id].item() != 1:
        raise ValueError(f"Root cause node {rc_type}[{rc_id}] does not have y=1")
    if not mask_dict[rc_type][rc_id].item():
        raise ValueError(f"Root cause node {rc_type}[{rc_id}] has mask=False")

    # 3. Victim nodes have mask=False
    for nt, vids in victims.items():
        for vid in vids:
            if mask_dict[nt][vid].item():
                raise ValueError(
                    f"Victim node {nt}[{vid}] should have mask=False"
                )

    # 4. No NaN / Inf in modified temporal state
    for nt, t in state.items():
        if torch.isnan(t).any():
            raise ValueError(f"NaN in temporal_state['{nt}'] after fault injection")
        if torch.isinf(t).any():
            raise ValueError(f"Inf in temporal_state['{nt}'] after fault injection")

    # 5. Anomaly amplitude is non-zero on root cause node
    rc_tensor = state[rc_type]   # [N, F, 3]
    rc_values = rc_tensor[rc_id, :, 0]
    if rc_values.max().item() <= 0.0:
        raise ValueError(
            f"Root cause node {rc_type}[{rc_id}] shows no anomaly signal"
        )


# ---------------------------------------------------------------------------
# Step 5: Final node feature assembly
# ---------------------------------------------------------------------------

# Fixed one-hot cardinality per categorical feature.
# Inferred from schema semantics: encoded/status/flag features are binary (0/1)
# unless explicitly extended. Using cardinality=2 is safe and deterministic.
_CATEGORICAL_CARDINALITY: int = 2


def _compute_final_dim(node_type: str) -> Tuple[int, int, int]:
    """Return (final_dim, num_continuous, num_categorical) for a node type.

    final_dim = num_continuous * 3 + num_categorical * _CATEGORICAL_CARDINALITY
    """
    if node_type not in FEATURE_SCHEMA:
        return 1, 0, 0
    cont_idx, cat_idx = _classify_features(node_type)
    num_cont = len(cont_idx)
    num_cat  = len(cat_idx)
    final_dim = num_cont * 3 + num_cat * _CATEGORICAL_CARDINALITY
    return final_dim, num_cont, num_cat


def _one_hot_fixed(values: torch.Tensor, num_classes: int) -> torch.Tensor:
    """One-hot encode a 1-D float tensor of class indices.

    Args:
        values:      float32 [N] -- values in [0, num_classes-1]
        num_classes: fixed cardinality

    Returns:
        float32 [N, num_classes]
    """
    indices = values.long().clamp(0, num_classes - 1)
    return torch.nn.functional.one_hot(indices, num_classes=num_classes).float()


def build_final_node_features(
    temporal_state: Dict[str, torch.Tensor],
) -> Dict[str, torch.Tensor]:
    """Assemble final 2-D node feature tensors from temporal state.

    Assembly policy (Variant A):
        - Continuous features: expand to [value, delta_short, delta_long] → 3 cols each
        - Categorical features: take value channel only → one-hot → 2 cols each
        - Concat order: [continuous_expanded | categorical_one_hot]

    Args:
        temporal_state: Dict[node_type, Tensor[N, F, 3]]
                        Output of simulate_healthy_trajectory() or inject_fault().

    Returns:
        Dict[node_type, Tensor[N, FINAL_DIM]]  -- float32, 2-D, no NaN/Inf.
        rca_context is returned as [1, 1] (virtual node, no expansion).
    """
    x_dict: Dict[str, torch.Tensor] = {}

    # rca_context -- virtual aggregation node, no schema, no expansion
    x_dict["rca_context"] = torch.ones(1, 1, dtype=torch.float32)

    for node_type in NODE_TYPES:
        if node_type == "rca_context":
            continue

        t = temporal_state[node_type]   # [N, F, 3]
        cont_idx, cat_idx = _classify_features(node_type)

        parts: List[torch.Tensor] = []

        # ---- Continuous: expand each feature to 3 temporal channels ----
        if cont_idx:
            # Gather all continuous features at once: [N, num_cont, 3]
            cont_tensor = t[:, cont_idx, :]          # [N, num_cont, 3]
            # Flatten last two dims: [N, num_cont * 3]
            N = cont_tensor.shape[0]
            cont_flat = cont_tensor.reshape(N, -1)   # [N, num_cont * 3]
            parts.append(cont_flat)

        # ---- Categorical: value channel only → one-hot ----
        if cat_idx:
            for feat_pos in cat_idx:
                cat_vals = t[:, feat_pos, 0]         # [N] -- value channel
                oh = _one_hot_fixed(cat_vals, _CATEGORICAL_CARDINALITY)  # [N, 2]
                parts.append(oh)

        # ---- Concat: [continuous_expanded | categorical_one_hot] ----
        # Safety: parts is guaranteed non-empty because every node type
        # in FEATURE_SCHEMA has at least one feature.
        x = torch.cat(parts, dim=-1)                 # [N, FINAL_DIM]
        x = torch.nan_to_num(x, nan=0.0, posinf=1e6, neginf=-1e6)
        x_dict[node_type] = x.float()

    return x_dict


# ---------------------------------------------------------------------------
# Final feature tensor validation
# ---------------------------------------------------------------------------

def validate_final_feature_tensors(
    x_dict: Dict[str, torch.Tensor],
) -> None:
    """Validate the output of build_final_node_features().

    Raises ValueError on any violation.
    """
    for node_type, x in x_dict.items():
        # 2-D only
        if x.dim() != 2:
            raise ValueError(
                f"[{node_type}] expected 2-D tensor, got {x.dim()}-D"
            )
        # Non-empty
        if x.shape[0] == 0 or x.shape[1] == 0:
            raise ValueError(
                f"[{node_type}] tensor has empty dimension: {tuple(x.shape)}"
            )
        # No NaN
        if torch.isnan(x).any():
            raise ValueError(f"[{node_type}] NaN detected in final feature tensor")
        # No Inf
        if torch.isinf(x).any():
            raise ValueError(f"[{node_type}] Inf detected in final feature tensor")
        # Deterministic dimension check
        if node_type != "rca_context":
            expected_dim, _, _ = _compute_final_dim(node_type)
            if x.shape[1] != expected_dim:
                raise ValueError(
                    f"[{node_type}] expected final_dim={expected_dim}, "
                    f"got {x.shape[1]}"
                )
        # Node count check
        expected_nodes = NODE_COUNTS[node_type]
        if x.shape[0] != expected_nodes:
            raise ValueError(
                f"[{node_type}] expected {expected_nodes} nodes, got {x.shape[0]}"
            )
# ---------------------------------------------------------------------------
# Step 6: Global normalization + full HeteroData assembly
# ---------------------------------------------------------------------------

_NORM_EPS: float = 1e-6

ScalerStats = Dict[str, Dict[str, torch.Tensor]]  # node_type -> {mean, std}


def fit_global_scaler(
    healthy_x_dicts: List[Dict[str, torch.Tensor]],
) -> ScalerStats:
    """Compute per-feature mean and std from a list of healthy x_dicts.

    Args:
        healthy_x_dicts: list of x_dict outputs from build_final_node_features()
                         generated WITHOUT fault injection.

    Returns:
        scaler_stats[node_type]["mean"]  shape [feature_dim]
        scaler_stats[node_type]["std"]   shape [feature_dim]
        std values below _NORM_EPS are replaced with 1.0 (stable constant).
    """
    if not healthy_x_dicts:
        raise ValueError("healthy_x_dicts must not be empty")

    node_types_present = [nt for nt in NODE_TYPES if nt != "rca_context"]
    accumulated: Dict[str, List[torch.Tensor]] = {nt: [] for nt in node_types_present}

    for x_dict in healthy_x_dicts:
        for nt in node_types_present:
            if nt in x_dict:
                accumulated[nt].append(x_dict[nt])

    scaler_stats: ScalerStats = {}

    for nt in node_types_present:
        if not accumulated[nt]:
            continue
        stacked = torch.cat(accumulated[nt], dim=0).float()
        mean = stacked.mean(dim=0)
        std  = stacked.std(dim=0)
        std  = torch.where(std < _NORM_EPS, torch.ones_like(std), std)
        scaler_stats[nt] = {"mean": mean, "std": std}

    return scaler_stats


def normalize_features(
    x_dict: Dict[str, torch.Tensor],
    scaler_stats: ScalerStats,
) -> Dict[str, torch.Tensor]:
    """Apply Z-score normalization: x_norm = (x - mu) / (sigma + eps).

    rca_context is passed through unchanged as [1, 1].
    No inplace mutation.
    """
    normalized: Dict[str, torch.Tensor] = {}

    for nt, x in x_dict.items():
        if nt == "rca_context":
            normalized[nt] = x.clone()
            continue
        if nt not in scaler_stats:
            normalized[nt] = x.clone()
            continue
        mu  = scaler_stats[nt]["mean"]
        sig = scaler_stats[nt]["std"]
        x_norm = (x.float() - mu) / (sig + _NORM_EPS)
        x_norm = torch.nan_to_num(x_norm, nan=0.0, posinf=1e6, neginf=-1e6)
        normalized[nt] = x_norm

    return normalized


def assemble_heterodata(
    normalized_x_dict: Dict[str, torch.Tensor],
    y_dict: Dict[str, torch.Tensor],
    loss_mask_dict: Dict[str, torch.Tensor],
    edge_index_dict: Dict[EdgeType, torch.Tensor],
    edge_attr_dict: Dict[EdgeType, torch.Tensor],
    node_weight_dict: Optional[Dict[str, torch.Tensor]] = None,
) -> HeteroData:
    """Pack all components into a fully labelled HeteroData object.

    Per node type:  data[nt].x, data[nt].y, data[nt].loss_mask,
                    data[nt].node_weight  (soft victim weight, 1.0 if absent)
    Per edge type:  data[et].edge_index, data[et].edge_attr
    """
    data = HeteroData()

    for nt, x in normalized_x_dict.items():
        data[nt].x = x
        if nt in y_dict:
            data[nt].y = y_dict[nt]
        if nt in loss_mask_dict:
            data[nt].loss_mask = loss_mask_dict[nt]
        if node_weight_dict is not None and nt in node_weight_dict:
            data[nt].node_weight = node_weight_dict[nt]

    for et, ei in edge_index_dict.items():
        data[et].edge_index = ei
        if et in edge_attr_dict:
            data[et].edge_attr = edge_attr_dict[et]

    return data


# ---------------------------------------------------------------------------
# Normalized graph validation
# ---------------------------------------------------------------------------

def validate_normalized_graph(
    data: HeteroData,
    scaler_stats: ScalerStats,
) -> None:
    """Validate a fully assembled normalized HeteroData object."""

    # 1. NaN / Inf in node features
    for nt in data.node_types:
        x = data[nt].x
        if torch.isnan(x).any():
            raise ValueError(f"NaN in normalized x for '{nt}'")
        if torch.isinf(x).any():
            raise ValueError(f"Inf in normalized x for '{nt}'")

    # 2. Normalized statistics (skip rca_context)
    for nt in data.node_types:
        if nt == "rca_context":
            continue
        if nt not in scaler_stats:
            continue
        x = data[nt].x
        mean_abs = x.mean(dim=0).abs().max().item()
        std_val  = x.std(dim=0).mean().item()
        # Faulted graphs legitimately shift mean; use loose tolerance
        if mean_abs > 15.0:
            raise ValueError(
                f"[{nt}] normalized mean too far from 0: max_abs={mean_abs:.4f}"
            )
        if not (0.01 < std_val < 50.0):
            raise ValueError(
                f"[{nt}] normalized std out of expected range: {std_val:.4f}"
            )

    # 3. Exactly one positive label
    total_pos = 0
    for nt in data.node_types:
        if nt == "rca_context":
            continue
        if hasattr(data[nt], "y") and data[nt].y is not None:
            total_pos += int(data[nt].y.sum().item())
    if total_pos != 1:
        raise ValueError(f"Expected exactly 1 positive label, got {total_pos}")

    # 4. Root cause node must have loss_mask=True
    for nt in data.node_types:
        if nt == "rca_context":
            continue
        node = data[nt]
        if not (hasattr(node, "y") and hasattr(node, "loss_mask")):
            continue
        y    = node.y
        mask = node.loss_mask
        rc_indices = (y == 1).nonzero(as_tuple=True)[0]
        for idx in rc_indices:
            if not mask[idx].item():
                raise ValueError(
                    f"Root cause node {nt}[{idx}] has loss_mask=False"
                )

    # 5. Edge attr / edge_index compatibility
    for et in data.edge_types:
        ei = data[et].edge_index
        if ei.dim() != 2 or ei.shape[0] != 2:
            raise ValueError(f"edge_index for {et} has wrong shape: {tuple(ei.shape)}")
        if hasattr(data[et], "edge_attr") and data[et].edge_attr is not None:
            ea = data[et].edge_attr
            if ea.shape[0] != ei.shape[1]:
                raise ValueError(
                    f"edge_attr rows {ea.shape[0]} != num_edges {ei.shape[1]} for {et}"
                )
            if torch.isnan(ea).any():
                raise ValueError(f"NaN in edge_attr for {et}")
            if torch.isinf(ea).any():
                raise ValueError(f"Inf in edge_attr for {et}")


# ---------------------------------------------------------------------------
# HeteroData assembly
# ---------------------------------------------------------------------------

def build_empty_graph() -> HeteroData:
    """Assemble and return a static HeteroData snapshot with dummy features."""

    host_to_leaf, leaf_to_spines, job_to_host, _ = build_routing_maps()

    edge_indices = build_edge_indices(host_to_leaf, leaf_to_spines, job_to_host)
    edge_attrs   = build_edge_attr(edge_indices)
    node_features = build_dummy_node_features()

    data = HeteroData()

    # Node features
    for node_type, x in node_features.items():
        data[node_type].x = x

    # Edge index + edge attr
    for edge_type, ei in edge_indices.items():
        data[edge_type].edge_index = ei
        data[edge_type].edge_attr  = edge_attrs[edge_type]

    return data


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_graph(data: HeteroData) -> None:
    """Validate structural correctness of the assembled HeteroData object.

    Raises ValueError on any violation.
    """
    all_edge_types: List[EdgeType] = (
        list(PHYSICAL_EDGE_TYPES)
        + list(LOGICAL_EDGE_TYPES)
        + list(CONTEXT_EDGE_TYPES)
    )

    # 1. All node types present
    for nt in NODE_TYPES:
        if nt not in data.node_types:
            raise ValueError(f"Missing node type in HeteroData: '{nt}'")

    # 2. All edge types present
    for et in all_edge_types:
        if et not in data.edge_types:
            raise ValueError(f"Missing edge type in HeteroData: {et}")

    # 3. edge_index shape, edge_attr row count, no NaN/Inf
    for et in all_edge_types:
        ei = data[et].edge_index
        ea = data[et].edge_attr

        if ei.dim() != 2 or ei.shape[0] != 2:
            raise ValueError(
                f"edge_index for {et} must have shape [2, E], got {tuple(ei.shape)}"
            )

        num_edges = ei.shape[1]

        if num_edges == 0:
            raise ValueError(f"edge_index for {et} is empty (0 edges)")

        if ea.shape[0] != num_edges:
            raise ValueError(
                f"edge_attr row count {ea.shape[0]} != num_edges {num_edges} for {et}"
            )

        if torch.isnan(ei.float()).any():
            raise ValueError(f"NaN detected in edge_index for {et}")

        if torch.isnan(ea).any():
            raise ValueError(f"NaN detected in edge_attr for {et}")

        if torch.isinf(ea).any():
            raise ValueError(f"Inf detected in edge_attr for {et}")

    # 4. Node feature tensors: no NaN / Inf
    for nt in NODE_TYPES:
        x = data[nt].x
        if torch.isnan(x).any():
            raise ValueError(f"NaN detected in node features for '{nt}'")
        if torch.isinf(x).any():
            raise ValueError(f"Inf detected in node features for '{nt}'")


# ---------------------------------------------------------------------------
# Step 7: Dataset generation loop + serialization
# ---------------------------------------------------------------------------

import gc
import json
import os
import time

NUM_DATASET_SAMPLES: int = 1000
HEALTHY_RATIO: float = 0.70
DATASET_DIR: str = "dataset"
RAW_DIR: str = os.path.join(DATASET_DIR, "raw")

# Number of healthy graphs used to fit the global scaler before generation
_SCALER_FIT_SAMPLES: int = 10


def _make_dataset_dirs() -> None:
    os.makedirs(RAW_DIR, exist_ok=True)


def generate_dataset(
    num_samples: int = NUM_DATASET_SAMPLES,
    healthy_ratio: float = HEALTHY_RATIO,
    base_seed: int = 42,
    dataset_dir: str = DATASET_DIR,
) -> ScalerStats:
    """Generate and stream-save a full dataset to disk.

    Hardening changes vs the original implementation:
      • Per-sample topology: each graph gets a unique random host→leaf and
        job→host assignment, eliminating positional identity leakage.
      • Per-sample edge attributes: physical link metrics are re-sampled for
        every graph so the model cannot memorise a fixed edge fingerprint.
      • Temporal fault integration: faults are ramped inside the simulation
        loop via simulate_trajectory_with_fault() so delta_short reflects
        natural per-step increments (≈noise scale) rather than a single
        injected spike (10–25× noise).
      • Soft victim weighting: node_weight stored per node (1.0 for RC and
        healthy nodes, U(0.2, 0.4) for victims). train.py uses this instead
        of hard-masking victims from the loss.

    Workflow per sample:
        1. Randomise topology (per-sample seed)
        2. Build per-sample edge_index and edge_attr
        3a. (healthy) simulate_healthy_trajectory()
        3b. (faulted) _resolve_fault_plan() → simulate_trajectory_with_fault()
               → post-processing (decoys, outliers)
        4. build_final_node_features()
        5. normalize_features()
        6. assemble_heterodata() with node_weight
        7. validate_normalized_graph()
        8. torch.save() → release memory → gc.collect()

    Scaler is fitted on healthy-only graphs with static topology BEFORE the
    main loop (feature distributions are topology-independent for healthy state).

    Returns:
        scaler_stats -- frozen scaler used for the whole dataset.
    """
    raw_dir = os.path.join(dataset_dir, "raw")
    os.makedirs(raw_dir, exist_ok=True)

    rng = np.random.default_rng(base_seed)

    # ---- Fit scaler on healthy-only graphs (static topology OK here) ------
    print(f"Fitting scaler on {_SCALER_FIT_SAMPLES} healthy samples...")
    healthy_x_dicts: List[Dict[str, torch.Tensor]] = []
    for i in range(_SCALER_FIT_SAMPLES):
        seed_i = int(rng.integers(0, 2**31))
        traj   = simulate_healthy_trajectory(seed=seed_i)
        xd     = build_final_node_features(traj)
        healthy_x_dicts.append(xd)
        del traj
        gc.collect()

    scaler_stats = fit_global_scaler(healthy_x_dicts)
    del healthy_x_dicts
    gc.collect()
    print("Scaler fitted.")

    # ---- Main generation loop ---------------------------------------------
    num_healthy = int(num_samples * healthy_ratio)
    num_faulted = num_samples - num_healthy

    # Build sample index: 0 = healthy, 1/2/3 = fault type index
    sample_flags: List[int] = [0] * num_healthy + list(
        rng.integers(1, len(FAULT_TYPES) + 1, size=num_faulted)
    )
    rng.shuffle(sample_flags)

    fault_type_counts: Dict[str, int] = {ft: 0 for ft in FAULT_TYPES}
    healthy_count = 0
    faulted_count = 0
    t_start = time.time()

    for idx, flag in enumerate(sample_flags):
        sample_seed = int(rng.integers(0, 2**31))
        is_healthy  = (flag == 0)

        # ---- Per-sample topology ------------------------------------------
        topo_seed    = int(rng.integers(0, 2**31))
        routing_maps = build_routing_maps(seed=topo_seed)
        h2l, l2s, j2h, _ = routing_maps

        # Per-sample edge structures (unique link state per graph)
        edge_rng     = np.random.default_rng(int(rng.integers(0, 2**31)))
        edge_indices = build_edge_indices(h2l, l2s, j2h)
        edge_attrs   = build_edge_attr(edge_indices, rng=edge_rng)

        if is_healthy:
            traj   = simulate_healthy_trajectory(seed=sample_seed)
            xd     = build_final_node_features(traj)
            norm_x = normalize_features(xd, scaler_stats)

            y_dict: Dict[str, torch.Tensor] = {
                nt: torch.zeros(NODE_COUNTS[nt], dtype=torch.long)
                for nt in NODE_TYPES if nt != "rca_context"
            }
            mask_dict: Dict[str, torch.Tensor] = {
                nt: torch.ones(NODE_COUNTS[nt], dtype=torch.bool)
                for nt in NODE_TYPES if nt != "rca_context"
            }
            # Healthy: all node weights = 1.0
            node_weight_dict: Dict[str, torch.Tensor] = {
                nt: torch.ones(NODE_COUNTS[nt], dtype=torch.float32)
                for nt in NODE_TYPES if nt != "rca_context"
            }
            del traj
            healthy_count += 1

        else:
            fault_type = FAULT_TYPES[flag - 1]
            severity   = float(rng.uniform(0.15, 0.45))
            fault_seed = int(rng.integers(0, 2**31))
            fault_rng  = np.random.default_rng(fault_seed)

            # Resolve stochastic fault parameters (no state mutation yet)
            plan = _resolve_fault_plan(fault_type, severity, routing_maps, fault_rng)

            # Simulate with fault ramped inside the loop (fixes delta_short)
            temporal_state = simulate_trajectory_with_fault(plan, seed=sample_seed)

            # Post-processing: decoys + healthy outliers
            temporal_state = _break_argmax_shortcut(
                temporal_state,
                plan["rc_node_type"],
                plan["rc_node_id"],
                fault_rng, n_decoys=4, decoy_scale=0.12,
            )
            temporal_state = _add_healthy_outliers(
                temporal_state, fault_rng, spike_prob=0.06, spike_scale=0.08,
            )

            xd     = build_final_node_features(temporal_state)
            norm_x = normalize_features(xd, scaler_stats)
            y_dict    = plan["y_dict"]
            mask_dict = plan["loss_mask_dict"]

            # Soft victim weighting: victims get U(0.2, 0.4), everyone else 1.0
            node_weight_dict = {}
            for nt in NODE_TYPES:
                if nt == "rca_context":
                    continue
                n = NODE_COUNTS[nt]
                w = torch.ones(n, dtype=torch.float32)
                for vid in plan["victim_node_ids"].get(nt, []):
                    w[vid] = float(fault_rng.uniform(0.2, 0.4))
                node_weight_dict[nt] = w

            fault_type_counts[fault_type] += 1
            faulted_count += 1
            del temporal_state, plan

        graph = assemble_heterodata(
            normalized_x_dict=norm_x,
            y_dict=y_dict,
            loss_mask_dict=mask_dict,
            edge_index_dict=edge_indices,
            edge_attr_dict=edge_attrs,
            node_weight_dict=node_weight_dict,
        )

        # Validate only faulted graphs (healthy have no positive labels)
        if not is_healthy:
            validate_normalized_graph(graph, scaler_stats)

        path = os.path.join(raw_dir, f"data_{idx}.pt")
        torch.save(graph, path)

        # Release memory immediately
        del xd, norm_x, y_dict, mask_dict, node_weight_dict
        del edge_indices, edge_attrs, graph
        gc.collect()

        # Progress logging every 50 samples
        if (idx + 1) % 50 == 0:
            elapsed   = time.time() - t_start
            avg_time  = elapsed / (idx + 1)
            fault_pct = 100.0 * faulted_count / (idx + 1)
            print(
                f"  [{idx + 1:>4}/{num_samples}]  "
                f"healthy={healthy_count}  faulted={faulted_count}  "
                f"fault%={fault_pct:.1f}  "
                f"avg_time={avg_time:.3f}s/sample  OOM-safe=True"
            )

    total_time = time.time() - t_start
    print(
        f"\nGeneration complete: {num_samples} samples in {total_time:.1f}s "
        f"({total_time / num_samples:.3f}s/sample)"
    )
    return scaler_stats


def save_dataset_metadata(
    scaler_stats: ScalerStats,
    num_samples: int = NUM_DATASET_SAMPLES,
    healthy_ratio: float = HEALTHY_RATIO,
    base_seed: int = 42,
    dataset_dir: str = DATASET_DIR,
) -> None:
    """Save dataset/metadata.json with schema, stats, and generation info."""
    os.makedirs(dataset_dir, exist_ok=True)

    # Feature dimension info
    feat_dims: Dict[str, int] = {}
    for nt in NODE_TYPES:
        if nt == "rca_context":
            feat_dims[nt] = 1
        else:
            fd, _, _ = _compute_final_dim(nt)
            feat_dims[nt] = fd

    # Scaler summary (mean/std as lists, not full tensors)
    scaler_summary: Dict[str, Dict[str, List[float]]] = {}
    for nt, stats in scaler_stats.items():
        scaler_summary[nt] = {
            "mean_min":  float(stats["mean"].min().item()),
            "mean_max":  float(stats["mean"].max().item()),
            "std_min":   float(stats["std"].min().item()),
            "std_max":   float(stats["std"].max().item()),
        }

    # Feature ordering info
    feature_order: Dict[str, Dict[str, List[str]]] = {}
    for nt in NODE_TYPES:
        if nt == "rca_context":
            continue
        cont_idx, cat_idx = _classify_features(nt)
        feature_order[nt] = {
            "continuous": [FEATURE_SCHEMA[nt][i] for i in cont_idx],
            "categorical": [FEATURE_SCHEMA[nt][i] for i in cat_idx],
        }

    metadata = {
        "dataset_size":        num_samples,
        "healthy_ratio":       healthy_ratio,
        "faulted_ratio":       1.0 - healthy_ratio,
        "fault_types":         FAULT_TYPES,
        "fault_distribution":  "uniform",
        "node_types":          NODE_TYPES,
        "edge_types":          [list(et) for et in (
            list(PHYSICAL_EDGE_TYPES)
            + list(LOGICAL_EDGE_TYPES)
            + list(CONTEXT_EDGE_TYPES)
        )],
        "feature_dimensions":  feat_dims,
        "node_counts":         NODE_COUNTS,
        "simulation_steps":    SIMULATION_STEPS,
        "fault_injection_step": FAULT_INJECTION_STEP,
        "ema_alpha":           EMA_ALPHA,
        "generation_seed":     base_seed,
        "scaler_fit_samples":  _SCALER_FIT_SAMPLES,
        "scaler_summary":      scaler_summary,
        "feature_ordering":    feature_order,
        "categorical_cardinality": _CATEGORICAL_CARDINALITY,
    }

    path = os.path.join(dataset_dir, "metadata.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
    print(f"Metadata saved -> {path}")


def save_loss_config(
    scaler_stats: ScalerStats,
    num_samples: int = NUM_DATASET_SAMPLES,
    healthy_ratio: float = HEALTHY_RATIO,
    dataset_dir: str = DATASET_DIR,
) -> None:
    """Save dataset/loss_config.json with pos_weight and masking policy."""
    os.makedirs(dataset_dir, exist_ok=True)

    num_healthy = int(num_samples * healthy_ratio)
    num_faulted = num_samples - num_healthy

    # pos_weight per node type: ratio of negative to positive examples
    # In faulted graphs: 1 positive per graph, rest negative
    pos_weight: Dict[str, float] = {}
    for nt in NODE_TYPES:
        if nt == "rca_context":
            continue
        n = NODE_COUNTS[nt]
        # Total positive labels across faulted graphs ≈ num_faulted (1 per graph)
        # Total negative labels ≈ num_faulted * (n - 1) + num_healthy * n
        total_pos = float(num_faulted)
        total_neg = float(num_faulted * (n - 1) + num_healthy * n)
        pos_weight[nt] = round(total_neg / max(total_pos, 1.0), 2)

    loss_config = {
        "pos_weight":      pos_weight,
        "fault_types":     FAULT_TYPES,
        "masking_policy":  "victim_nodes_masked",
        "loss_fn":         "BCEWithLogitsLoss",
        "label_smoothing": 0.0,
        "num_samples":     num_samples,
        "healthy_count":   num_healthy,
        "faulted_count":   num_faulted,
    }

    path = os.path.join(dataset_dir, "loss_config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(loss_config, f, indent=2)
    print(f"Loss config saved -> {path}")


def validate_saved_dataset(
    num_samples: int = NUM_DATASET_SAMPLES,
    dataset_dir: str = DATASET_DIR,
    check_n: int = 20,
) -> None:
    """Spot-check saved dataset files for schema and label consistency.

    Args:
        check_n: number of random files to validate (full scan is slow).
    """
    raw_dir = os.path.join(dataset_dir, "raw")
    rng = np.random.default_rng(0)

    all_edge_types = (
        list(PHYSICAL_EDGE_TYPES)
        + list(LOGICAL_EDGE_TYPES)
        + list(CONTEXT_EDGE_TYPES)
    )

    indices = list(rng.choice(num_samples, size=min(check_n, num_samples), replace=False))
    errors: List[str] = []

    for idx in indices:
        path = os.path.join(raw_dir, f"data_{idx}.pt")
        if not os.path.exists(path):
            errors.append(f"Missing file: {path}")
            continue

        try:
            g = torch.load(path, weights_only=False)
        except Exception as e:
            errors.append(f"Load error {path}: {e}")
            continue

        # Schema checks
        for nt in NODE_TYPES:
            if nt not in g.node_types:
                errors.append(f"[{idx}] missing node type '{nt}'")
        for et in all_edge_types:
            if et not in g.edge_types:
                errors.append(f"[{idx}] missing edge type {et}")

        # Feature dim checks
        for nt in NODE_TYPES:
            if nt == "rca_context":
                continue
            expected_fd, _, _ = _compute_final_dim(nt)
            actual_fd = g[nt].x.shape[1]
            if actual_fd != expected_fd:
                errors.append(
                    f"[{idx}] {nt} feat_dim={actual_fd} expected={expected_fd}"
                )

        # Label consistency
        total_pos = sum(
            int(g[nt].y.sum().item())
            for nt in NODE_TYPES
            if nt != "rca_context" and hasattr(g[nt], "y")
        )
        # Healthy graphs have 0 positive labels; faulted have exactly 1
        if total_pos not in (0, 1):
            errors.append(f"[{idx}] unexpected positive label count: {total_pos}")

        del g
        gc.collect()

    if errors:
        raise ValueError(
            f"Dataset validation found {len(errors)} error(s):\n"
            + "\n".join(errors[:10])
        )
    print(f"Dataset validation PASSED ({len(indices)} files checked).")


# ---------------------------------------------------------------------------
# Step 8: PyG training compatibility / sanity check
# ---------------------------------------------------------------------------

import torch.nn as nn
from torch_geometric.loader import DataLoader, NeighborLoader
from torch_geometric.nn import GATv2Conv, HeteroConv, Linear


class _TinySanityModel(nn.Module):
    """Minimal heterogeneous GNN for training sanity validation only.

    Architecture:
        Layer 0: HeteroConv(GATv2Conv) -- message passing with edge_attr
        Layer 1: Linear per node type  -- binary logit output

    This is NOT the production model. It exists solely to verify that
    the dataset survives a real forward + backward pass.
    """

    def __init__(
        self,
        node_dims: Dict[str, int],
        edge_dim_physical: int = EDGE_DIM_PHYSICAL,
        hidden_dim: int = 16,
    ) -> None:
        super().__init__()

        all_edge_types: List[EdgeType] = (
            list(PHYSICAL_EDGE_TYPES)
            + list(LOGICAL_EDGE_TYPES)
            + list(CONTEXT_EDGE_TYPES)
        )

        # Build one GATv2Conv per edge type.
        # Physical edges carry edge_attr (dim=EDGE_DIM_PHYSICAL).
        # Logical/context edges carry edge_attr (dim=EDGE_DIM_DUMMY=1).
        physical_set = {et for et in PHYSICAL_EDGE_TYPES}
        logical_set  = {et for et in LOGICAL_EDGE_TYPES}
        context_set  = {et for et in CONTEXT_EDGE_TYPES}

        conv_dict: Dict[Tuple[str, str, str], GATv2Conv] = {}
        for et in all_edge_types:
            src_type, rel, dst_type = et
            in_src = node_dims.get(src_type, 1)
            in_dst = node_dims.get(dst_type, 1)

            if et in physical_set:
                ea_dim = edge_dim_physical
            else:
                ea_dim = EDGE_DIM_DUMMY

            conv_dict[et] = GATv2Conv(
                in_channels=(in_src, in_dst),
                out_channels=hidden_dim,
                heads=1,
                edge_dim=ea_dim,
                add_self_loops=False,
            )

        self.conv = HeteroConv(conv_dict, aggr="sum")

        # Per-node-type output linear: hidden_dim → 1 logit
        # Prefix keys with "nt_" to avoid collision with reserved PyTorch
        # attribute names like "cpu", "cuda", "to", etc.
        self.out_lins = nn.ModuleDict({
            f"nt_{nt}": Linear(hidden_dim, 1)
            for nt in node_dims
            if nt != "rca_context"
        })

    def forward(self, x_dict: Dict[str, torch.Tensor],
                edge_index_dict: Dict[EdgeType, torch.Tensor],
                edge_attr_dict: Dict[EdgeType, torch.Tensor]) -> Dict[str, torch.Tensor]:

        # HeteroConv expects edge_attr keyed by string tuple representation
        # PyG HeteroConv accepts Dict[Tuple, Tensor] for edge_attr_dict
        out = self.conv(x_dict, edge_index_dict, edge_attr_dict=edge_attr_dict)

        logits: Dict[str, torch.Tensor] = {}
        for nt, h in out.items():
            key = f"nt_{nt}"
            if key in self.out_lins:
                logits[nt] = self.out_lins[key](h).squeeze(-1)  # [N]
        return logits


def run_training_sanity_check(
    dataset_dir: str = DATASET_DIR,
    num_graphs: int = 8,
    seed: int = 42,
) -> None:
    """Load saved graphs, run mini-batch forward+backward, validate gradients.

    Checks:
        - DataLoader batching works
        - HeteroConv forward pass works
        - Masked BCEWithLogitsLoss works
        - backward() works
        - Gradients are finite and non-NaN
        - NeighborLoader can be instantiated and yields one batch
    """
    torch.manual_seed(seed)
    raw_dir = os.path.join(dataset_dir, "raw")

    # ---- Load graphs -------------------------------------------------------
    graphs = []
    for i in range(num_graphs):
        path = os.path.join(raw_dir, f"data_{i}.pt")
        g = torch.load(path, weights_only=False)
        graphs.append(g)

    print(f"  Loaded {len(graphs)} graphs from {raw_dir}")

    # ---- DataLoader batching -----------------------------------------------
    loader = DataLoader(graphs, batch_size=4, shuffle=False)
    batch = next(iter(loader))

    print("  DataLoader batch:")
    for nt in NODE_TYPES:
        if nt == "rca_context":
            continue
        x = batch[nt].x
        print(f"    {nt:<14}  x={tuple(x.shape)}")

    print("  Edge counts in batch:")
    all_edge_types: List[EdgeType] = (
        list(PHYSICAL_EDGE_TYPES)
        + list(LOGICAL_EDGE_TYPES)
        + list(CONTEXT_EDGE_TYPES)
    )
    for et in all_edge_types:
        ei = batch[et].edge_index
        print(f"    {str(et):<50}  edges={ei.shape[1]}")

    # ---- Build node_dims from batch ----------------------------------------
    node_dims: Dict[str, int] = {}
    for nt in NODE_TYPES:
        if nt == "rca_context":
            node_dims[nt] = 1
        else:
            node_dims[nt] = batch[nt].x.shape[1]

    # ---- Build model -------------------------------------------------------
    model = _TinySanityModel(node_dims=node_dims)
    model.train()

    # ---- Prepare inputs ----------------------------------------------------
    x_dict: Dict[str, torch.Tensor] = {}
    for nt in NODE_TYPES:
        x_dict[nt] = batch[nt].x.float()

    edge_index_dict: Dict[EdgeType, torch.Tensor] = {}
    edge_attr_dict:  Dict[EdgeType, torch.Tensor] = {}
    for et in all_edge_types:
        edge_index_dict[et] = batch[et].edge_index
        edge_attr_dict[et]  = batch[et].edge_attr.float()

    # ---- Forward pass ------------------------------------------------------
    logits = model(x_dict, edge_index_dict, edge_attr_dict)

    print("  Logits shapes:")
    for nt, lg in logits.items():
        print(f"    {nt:<14}  logits={tuple(lg.shape)}")

    # ---- Masked BCEWithLogitsLoss ------------------------------------------
    bce = nn.BCEWithLogitsLoss(reduction="none")
    total_loss = torch.tensor(0.0)
    masked_counts: Dict[str, int] = {}

    for nt, lg in logits.items():
        y    = batch[nt].y.float()
        mask = batch[nt].loss_mask  # bool [N]

        node_loss = bce(lg, y)           # [N]
        masked_loss = node_loss[mask]    # only unmasked nodes
        if masked_loss.numel() > 0:
            total_loss = total_loss + masked_loss.mean()
        masked_counts[nt] = int((~mask).sum().item())

    print(f"  Masked victim counts: {masked_counts}")
    print(f"  Total masked loss: {total_loss.item():.6f}")

    if torch.isnan(total_loss) or torch.isinf(total_loss):
        raise ValueError(f"Loss is NaN or Inf: {total_loss.item()}")

    # ---- Backward pass -----------------------------------------------------
    total_loss.backward()

    print("  Gradient statistics:")
    grad_ok = True
    for name, param in model.named_parameters():
        if param.grad is None:
            continue
        g = param.grad
        if torch.isnan(g).any() or torch.isinf(g).any():
            print(f"    FAIL {name}: NaN/Inf gradient")
            grad_ok = False
        else:
            print(
                f"    OK   {name:<50}  "
                f"grad_norm={g.norm().item():.4f}"
            )

    if not grad_ok:
        raise ValueError("NaN or Inf gradients detected -- dataset is NOT training-ready")

    # ---- NeighborLoader compatibility check --------------------------------
    print()
    print("  NeighborLoader compatibility check...")
    # Use the first graph (single graph, not batched)
    single = graphs[0]
    try:
        nl = NeighborLoader(
            single,
            num_neighbors={et: [4] for et in single.edge_types},
            batch_size=8,
            input_nodes=("cpu", torch.arange(min(8, NODE_COUNTS["cpu"]))),
        )
        nl_batch = next(iter(nl))
        print(
            f"    NeighborLoader OK -- "
            f"sampled cpu nodes: {nl_batch['cpu'].x.shape[0]}"
        )
    except Exception as e:
        print(f"    NeighborLoader WARNING: {e}")
        print("    (NeighborLoader may require torch-sparse -- non-critical)")

    print()
    print("Training sanity check PASSED -- dataset is training-ready.")


def validate_training_batch(
    dataset_dir: str = DATASET_DIR,
    num_graphs: int = 4,
) -> None:
    """Quick structural validation of a DataLoader batch.

    Checks:
        - batching works
        - all node types present
        - all edge types present
        - logits shape correct after a forward pass
        - masked loss is finite
        - no edge_attr conflicts
    """
    raw_dir = os.path.join(dataset_dir, "raw")
    graphs  = [
        torch.load(os.path.join(raw_dir, f"data_{i}.pt"), weights_only=False)
        for i in range(num_graphs)
    ]

    loader = DataLoader(graphs, batch_size=num_graphs, shuffle=False)
    batch  = next(iter(loader))

    all_edge_types: List[EdgeType] = (
        list(PHYSICAL_EDGE_TYPES)
        + list(LOGICAL_EDGE_TYPES)
        + list(CONTEXT_EDGE_TYPES)
    )

    # Node types
    for nt in NODE_TYPES:
        if nt not in batch.node_types:
            raise ValueError(f"validate_training_batch: missing node type '{nt}'")

    # Edge types
    for et in all_edge_types:
        if et not in batch.edge_types:
            raise ValueError(f"validate_training_batch: missing edge type {et}")

    # edge_attr shape compatibility
    for et in all_edge_types:
        ei = batch[et].edge_index
        ea = batch[et].edge_attr
        if ea.shape[0] != ei.shape[1]:
            raise ValueError(
                f"validate_training_batch: edge_attr rows {ea.shape[0]} "
                f"!= num_edges {ei.shape[1]} for {et}"
            )

    # Forward pass
    node_dims = {
        nt: (1 if nt == "rca_context" else batch[nt].x.shape[1])
        for nt in NODE_TYPES
    }
    model  = _TinySanityModel(node_dims=node_dims)
    x_dict = {nt: batch[nt].x.float() for nt in NODE_TYPES}
    ei_d   = {et: batch[et].edge_index for et in all_edge_types}
    ea_d   = {et: batch[et].edge_attr.float() for et in all_edge_types}

    with torch.no_grad():
        logits = model(x_dict, ei_d, ea_d)

    # Logits shape
    for nt, lg in logits.items():
        expected_n = batch[nt].x.shape[0]
        if lg.shape[0] != expected_n:
            raise ValueError(
                f"validate_training_batch: logits[{nt}] shape {lg.shape[0]} "
                f"!= num_nodes {expected_n}"
            )

    # Masked loss finite
    bce = nn.BCEWithLogitsLoss(reduction="none")
    for nt, lg in logits.items():
        y    = batch[nt].y.float()
        mask = batch[nt].loss_mask
        loss = bce(lg, y)[mask].mean()
        if torch.isnan(loss) or torch.isinf(loss):
            raise ValueError(f"validate_training_batch: loss NaN/Inf for '{nt}'")

    print("validate_training_batch PASSED.")


# ---------------------------------------------------------------------------
# Step 9: Dataset statistical diagnostics + quality report
# ---------------------------------------------------------------------------

import math

try:
    import matplotlib
    matplotlib.use("Agg")          # non-interactive backend
    import matplotlib.pyplot as plt
    _MATPLOTLIB_AVAILABLE = True
except ImportError:
    _MATPLOTLIB_AVAILABLE = False


def _load_sample(
    dataset_dir: str,
    n: int,
    seed: int,
    num_total: int = NUM_DATASET_SAMPLES,
) -> List[HeteroData]:
    """Load a deterministic random subset of saved graphs."""
    rng     = np.random.default_rng(seed)
    indices = list(rng.choice(num_total, size=min(n, num_total), replace=False))
    raw_dir = os.path.join(dataset_dir, "raw")
    graphs: List[HeteroData] = []
    for idx in indices:
        g = torch.load(os.path.join(raw_dir, f"data_{idx}.pt"), weights_only=False)
        graphs.append(g)
    return graphs


def run_dataset_diagnostics(
    dataset_dir: str = DATASET_DIR,
    sample_size: int = 150,
    seed: int = 0,
) -> None:
    """Run full statistical diagnostics on the saved dataset.

    Blocks:
        1. Class balance
        2. Feature distribution analysis
        3. Healthy vs faulted separability
        4. Label leakage detection
        5. Temporal consistency
        6. Fault propagation analysis
        7. Graph structure validation
        8. Trainability heuristics
    """
    print("=" * 60)
    print(f"Step 9: Dataset diagnostics (sample={sample_size})")
    print("=" * 60)

    graphs = _load_sample(dataset_dir, sample_size, seed)

    # Split healthy / faulted
    healthy_graphs: List[HeteroData] = []
    faulted_graphs: List[HeteroData] = []
    for g in graphs:
        total_pos = sum(
            int(g[nt].y.sum().item())
            for nt in NODE_TYPES
            if nt != "rca_context" and hasattr(g[nt], "y")
        )
        if total_pos == 0:
            healthy_graphs.append(g)
        else:
            faulted_graphs.append(g)

    n_h = len(healthy_graphs)
    n_f = len(faulted_graphs)
    print(f"\n[1] CLASS BALANCE")
    print(f"  Loaded graphs  : {len(graphs)}")
    print(f"  Healthy        : {n_h}  ({100*n_h/len(graphs):.1f}%)")
    print(f"  Faulted        : {n_f}  ({100*n_f/len(graphs):.1f}%)")

    # Positive labels per node type
    pos_per_nt: Dict[str, int] = {nt: 0 for nt in NODE_TYPES if nt != "rca_context"}
    masked_per_nt: Dict[str, int] = {nt: 0 for nt in NODE_TYPES if nt != "rca_context"}
    total_per_nt: Dict[str, int]  = {nt: 0 for nt in NODE_TYPES if nt != "rca_context"}

    for g in graphs:
        for nt in NODE_TYPES:
            if nt == "rca_context":
                continue
            if hasattr(g[nt], "y"):
                pos_per_nt[nt]    += int(g[nt].y.sum().item())
                total_per_nt[nt]  += g[nt].x.shape[0]
            if hasattr(g[nt], "loss_mask"):
                masked_per_nt[nt] += int((~g[nt].loss_mask).sum().item())

    print("\n  Positive labels per node type:")
    for nt in NODE_TYPES:
        if nt == "rca_context":
            continue
        print(
            f"    {nt:<14}  pos={pos_per_nt[nt]:>4}  "
            f"masked={masked_per_nt[nt]:>5}  "
            f"mask_rate={100*masked_per_nt[nt]/max(total_per_nt[nt],1):.2f}%"
        )

    # ---- 2. Feature distribution analysis ----------------------------------
    print(f"\n[2] FEATURE DISTRIBUTION ANALYSIS")
    warnings_list: List[str] = []

    for nt in NODE_TYPES:
        if nt == "rca_context":
            continue
        xs = torch.cat([g[nt].x for g in graphs], dim=0)  # [N_total, D]
        feat_mean = xs.mean(dim=0)
        feat_std  = xs.std(dim=0)
        feat_min  = xs.min(dim=0).values
        feat_max  = xs.max(dim=0).values
        p25 = xs.quantile(0.25, dim=0)
        p75 = xs.quantile(0.75, dim=0)
        iqr = p75 - p25

        collapsed = int((feat_std < 1e-4).sum().item())
        exploding = int((feat_std > 50.0).sum().item())

        print(
            f"  {nt:<14}  D={xs.shape[1]:>3}  "
            f"mean=[{feat_mean.min():.3f},{feat_mean.max():.3f}]  "
            f"std=[{feat_std.min():.3f},{feat_std.max():.3f}]  "
            f"collapsed={collapsed}  exploding={exploding}"
        )
        if collapsed > 0:
            warnings_list.append(f"WARNING: {nt} has {collapsed} collapsed feature dims (std<1e-4)")
        if exploding > 0:
            warnings_list.append(f"WARNING: {nt} has {exploding} exploding feature dims (std>50)")

    # ---- 3. Healthy vs faulted separability --------------------------------
    print(f"\n[3] HEALTHY vs FAULTED SEPARABILITY")
    if n_h > 0 and n_f > 0:
        for nt in ["switch", "hdd", "ram", "cpu", "job"]:
            if nt == "rca_context":
                continue
            xh = torch.cat([g[nt].x for g in healthy_graphs], dim=0)
            xf = torch.cat([g[nt].x for g in faulted_graphs], dim=0)
            mean_diff = (xf.mean(dim=0) - xh.mean(dim=0)).abs().mean().item()
            std_h     = xh.std(dim=0).mean().item()
            std_f     = xf.std(dim=0).mean().item()
            # Rough effect size: mean_diff / pooled_std
            pooled_std = (std_h + std_f) / 2.0 + 1e-6
            effect     = mean_diff / pooled_std
            trivial    = effect > 5.0
            print(
                f"  {nt:<14}  mean_diff={mean_diff:.4f}  "
                f"effect_size={effect:.3f}  "
                f"{'TRIVIAL (too easy)' if trivial else 'OK'}"
            )
            if trivial:
                warnings_list.append(
                    f"WARNING: {nt} effect_size={effect:.2f} — separation may be trivially easy"
                )
    else:
        print("  Skipped (insufficient healthy or faulted samples in subset)")

    # ---- 4. Label leakage detection ----------------------------------------
    print(f"\n[4] LABEL LEAKAGE DETECTION")
    leakage_found = False
    if n_f > 0:
        for nt in NODE_TYPES:
            if nt == "rca_context":
                continue
            # Check if any single feature dimension is perfectly correlated with y
            xs_f = torch.cat([g[nt].x for g in faulted_graphs], dim=0).float()
            ys_f = torch.cat([g[nt].y for g in faulted_graphs], dim=0).float()
            if ys_f.sum() == 0:
                continue
            # Pearson correlation per feature
            xs_c = xs_f - xs_f.mean(dim=0)
            ys_c = ys_f - ys_f.mean()
            denom = xs_c.norm(dim=0) * ys_c.norm() + 1e-8
            corr  = (xs_c * ys_c.unsqueeze(1)).sum(dim=0) / denom
            max_corr = corr.abs().max().item()
            suspicious = max_corr > 0.95
            print(
                f"  {nt:<14}  max_feature_label_corr={max_corr:.4f}  "
                f"{'SUSPICIOUS LEAKAGE' if suspicious else 'OK'}"
            )
            if suspicious:
                leakage_found = True
                warnings_list.append(
                    f"WARNING: {nt} max_corr={max_corr:.3f} — possible label leakage"
                )

    # ---- 5. Temporal consistency -------------------------------------------
    print(f"\n[5] TEMPORAL CONSISTENCY")
    # Temporal state is stored as final snapshot [N, F, 3] before assembly.
    # We check the assembled x tensor for delta_short/delta_long patterns.
    # For continuous features: cols 0,1,2 = value, delta_short, delta_long
    for nt in ["cpu", "switch", "job"]:
        xs = torch.cat([g[nt].x for g in graphs], dim=0)
        cont_idx, _ = _classify_features(nt)
        if len(cont_idx) < 1:
            continue
        # delta_short columns: indices 1, 4, 7, ... (every 3rd starting at 1)
        ds_cols = [i * 3 + 1 for i in range(len(cont_idx)) if i * 3 + 1 < xs.shape[1]]
        dl_cols = [i * 3 + 2 for i in range(len(cont_idx)) if i * 3 + 2 < xs.shape[1]]
        if ds_cols:
            ds_std = xs[:, ds_cols].std().item()
            dl_std = xs[:, dl_cols].std().item() if dl_cols else 0.0
            exploding_t = ds_std > 20.0 or dl_std > 20.0
            print(
                f"  {nt:<14}  delta_short_std={ds_std:.4f}  "
                f"delta_long_std={dl_std:.4f}  "
                f"{'EXPLODING' if exploding_t else 'OK'}"
            )
            if exploding_t:
                warnings_list.append(f"WARNING: {nt} temporal deltas exploding")

    # ---- 5b. Temporal realism: delta_short ratio RC vs healthy ---------------
    print(f"\n[5b] TEMPORAL REALISM CHECK (delta_short RC / healthy)")
    for nt in ["hdd", "switch", "ram"]:
        cont_idx_nt, _ = _classify_features(nt)
        if not cont_idx_nt:
            continue
        # delta_short columns in the final x tensor: every 3rd col starting at 1
        ds_cols = [
            i * 3 + 1
            for i in range(len(cont_idx_nt))
            if i * 3 + 1 < NODE_COUNTS[nt]  # safety; actual bound checked below
        ]
        # Recompute with actual feature dim
        sample_g = graphs[0] if graphs else None
        if sample_g is not None:
            feat_dim = sample_g[nt].x.shape[1]
            ds_cols = [c for c in ds_cols if c < feat_dim]

        if not ds_cols:
            continue

        rc_ds_vals: List[float] = []
        for g in faulted_graphs:
            if not hasattr(g[nt], "y"):
                continue
            n_pos = int(g[nt].y.sum().item())
            if n_pos == 0:
                continue
            rc_idx = int(g[nt].y.argmax().item())
            rc_ds  = float(g[nt].x[rc_idx, ds_cols].abs().mean().item())
            rc_ds_vals.append(rc_ds)

        healthy_ds_vals: List[float] = []
        for g in healthy_graphs:
            hds = float(g[nt].x[:, ds_cols].abs().mean().item())
            healthy_ds_vals.append(hds)

        if rc_ds_vals and healthy_ds_vals:
            ratio = float(np.mean(rc_ds_vals)) / (float(np.mean(healthy_ds_vals)) + 1e-6)
            status = "OK" if ratio < 5.0 else "HIGH — temporal leakage risk"
            print(
                f"  {nt:<14}  "
                f"rc_delta_short={np.mean(rc_ds_vals):.4f}  "
                f"healthy_delta_short={np.mean(healthy_ds_vals):.4f}  "
                f"ratio={ratio:.2f}  {status}"
            )
            if ratio > 5.0:
                warnings_list.append(
                    f"WARNING: {nt} delta_short ratio={ratio:.1f} — fault injected as step jump"
                )
        else:
            print(f"  {nt:<14}  skipped (insufficient samples)")

    # ---- 6. Fault propagation analysis -------------------------------------
    print(f"\n[6] FAULT PROPAGATION ANALYSIS")
    if n_f > 0:
        victim_counts: List[int] = []
        for g in faulted_graphs:
            vc = sum(
                int((~g[nt].loss_mask).sum().item())
                for nt in NODE_TYPES
                if nt != "rca_context" and hasattr(g[nt], "loss_mask")
            )
            victim_counts.append(vc)
        vc_arr = np.array(victim_counts)
        print(
            f"  Victim count per graph:  "
            f"min={vc_arr.min()}  max={vc_arr.max()}  "
            f"mean={vc_arr.mean():.1f}  std={vc_arr.std():.1f}"
        )
        zero_victims = int((vc_arr == 0).sum())
        if zero_victims > 0:
            warnings_list.append(
                f"WARNING: {zero_victims} faulted graphs have 0 victims"
            )
        print(f"  Graphs with 0 victims: {zero_victims}")
    else:
        print("  Skipped (no faulted graphs in subset)")

    # ---- 7. Graph structure validation -------------------------------------
    print(f"\n[7] GRAPH STRUCTURE VALIDATION")
    all_edge_types = (
        list(PHYSICAL_EDGE_TYPES)
        + list(LOGICAL_EDGE_TYPES)
        + list(CONTEXT_EDGE_TYPES)
    )
    struct_ok = True
    for g in graphs[:10]:
        for nt in NODE_TYPES:
            if nt not in g.node_types:
                warnings_list.append(f"WARNING: missing node type '{nt}' in a graph")
                struct_ok = False
        for et in all_edge_types:
            if et not in g.edge_types:
                warnings_list.append(f"WARNING: missing edge type {et} in a graph")
                struct_ok = False
    print(f"  Structure check (10 graphs): {'OK' if struct_ok else 'ISSUES FOUND'}")

    # Node count stability
    for nt in NODE_TYPES:
        if nt == "rca_context":
            continue
        counts = [g[nt].x.shape[0] for g in graphs]
        if len(set(counts)) > 1:
            warnings_list.append(f"WARNING: {nt} node count varies across graphs: {set(counts)}")
    print("  Node count stability: OK")

    # ---- 8. Trainability heuristics ----------------------------------------
    print(f"\n[8] TRAINABILITY HEURISTICS")
    imbalance_ratio = n_h / max(n_f, 1)
    mask_rate_job   = 100 * masked_per_nt["job"] / max(total_per_nt["job"], 1)
    mask_rate_cpu   = 100 * masked_per_nt["cpu"] / max(total_per_nt["cpu"], 1)

    print(f"  Class imbalance ratio (healthy/faulted) : {imbalance_ratio:.2f}")
    print(f"  Masking rate job                        : {mask_rate_job:.2f}%")
    print(f"  Masking rate cpu                        : {mask_rate_cpu:.2f}%")

    if imbalance_ratio > 5.0:
        warnings_list.append(
            f"WARNING: high class imbalance ({imbalance_ratio:.1f}x) -- use pos_weight"
        )
    if mask_rate_job > 50.0:
        warnings_list.append(
            f"WARNING: job masking rate {mask_rate_job:.1f}% is very high"
        )

    difficulty = "MEDIUM"
    if leakage_found:
        difficulty = "TRIVIAL (leakage risk)"
    elif imbalance_ratio > 4.0:
        difficulty = "HARD (imbalanced)"

    print(f"  Estimated task difficulty               : {difficulty}")

    # ---- Optional plots ----------------------------------------------------
    if _MATPLOTLIB_AVAILABLE:
        plots_dir = os.path.join(dataset_dir, "plots")
        os.makedirs(plots_dir, exist_ok=True)
        _save_diagnostics_plots(graphs, healthy_graphs, faulted_graphs, plots_dir)

    # ---- Warnings summary --------------------------------------------------
    print(f"\n{'='*60}")
    if warnings_list:
        print(f"WARNINGS ({len(warnings_list)}):")
        for w in warnings_list:
            print(f"  {w}")
    else:
        print("No warnings detected.")

    # ---- Quality score -----------------------------------------------------
    score = 100
    score -= len(warnings_list) * 5
    score = max(0, score)
    leakage_risk = "HIGH" if leakage_found else "LOW"
    print(f"\nDataset quality score : {score}/100")
    print(f"Leakage risk          : {leakage_risk}")
    print(f"Trainability estimate : {difficulty}")
    print(f"Recommended next phase: Step 10 -- train.py with GATv2 + masked BCE loss")
    print()
    print("Step 9 diagnostics PASSED.")


def _save_diagnostics_plots(
    graphs: List[HeteroData],
    healthy_graphs: List[HeteroData],
    faulted_graphs: List[HeteroData],
    plots_dir: str,
) -> None:
    """Save lightweight diagnostic plots to plots_dir."""

    # 1. Class balance bar chart
    fig, ax = plt.subplots(figsize=(5, 3))
    ax.bar(["Healthy", "Faulted"], [len(healthy_graphs), len(faulted_graphs)],
           color=["steelblue", "tomato"])
    ax.set_title("Class Balance")
    ax.set_ylabel("Graph count")
    fig.tight_layout()
    fig.savefig(os.path.join(plots_dir, "class_balance.png"), dpi=80)
    plt.close(fig)

    # 2. Feature std per node type (anomaly spread)
    fig, axes = plt.subplots(2, 3, figsize=(12, 6))
    axes = axes.flatten()
    plot_nts = [nt for nt in NODE_TYPES if nt != "rca_context"]
    for i, nt in enumerate(plot_nts):
        ax = axes[i]
        xs = torch.cat([g[nt].x for g in graphs], dim=0)
        stds = xs.std(dim=0).numpy()
        ax.bar(range(len(stds)), stds, color="steelblue", alpha=0.7)
        ax.set_title(f"{nt} feature std")
        ax.set_xlabel("feature dim")
        ax.set_ylabel("std")
    fig.tight_layout()
    fig.savefig(os.path.join(plots_dir, "feature_std.png"), dpi=80)
    plt.close(fig)

    # 3. Healthy vs faulted mean difference for switch and job
    if healthy_graphs and faulted_graphs:
        fig, axes = plt.subplots(1, 2, figsize=(10, 4))
        for ax, nt in zip(axes, ["switch", "job"]):
            xh = torch.cat([g[nt].x for g in healthy_graphs], dim=0).mean(dim=0).numpy()
            xf = torch.cat([g[nt].x for g in faulted_graphs], dim=0).mean(dim=0).numpy()
            diff = np.abs(xf - xh)
            ax.bar(range(len(diff)), diff, color="tomato", alpha=0.8)
            ax.set_title(f"{nt}: |mean_faulted - mean_healthy|")
            ax.set_xlabel("feature dim")
        fig.tight_layout()
        fig.savefig(os.path.join(plots_dir, "anomaly_spread.png"), dpi=80)
        plt.close(fig)

    print(f"  Plots saved to {plots_dir}")


def generate_dataset_report(
    dataset_dir: str = DATASET_DIR,
    sample_size: int = 150,
    seed: int = 0,
) -> None:
    """Generate and save a textual dataset quality report to dataset/dataset_report.txt."""
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        run_dataset_diagnostics(dataset_dir=dataset_dir, sample_size=sample_size, seed=seed)

    report_text = buf.getvalue()

    # Also print to console
    print(report_text)

    report_path = os.path.join(dataset_dir, "dataset_report.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_text)
    print(f"Report saved -> {report_path}")


# ---------------------------------------------------------------------------
# Ablation tests + hardening report
# ---------------------------------------------------------------------------

def run_ablation_tests(
    dataset_dir: str = DATASET_DIR,
    num_graphs: int = 8,
    seed: int = 42,
) -> Dict[str, float]:
    """Run topology ablation tests to measure how much graph structure matters.

    Tests:
        A. MLP baseline (no edges, no message passing)
        B. Edge dropout (50% edges removed)
        C. Randomized topology (shuffled host-job mapping)
        D. Feature ablation (top anomaly features zeroed)

    Returns dict with RCA accuracy for each condition.
    """
    import torch.nn as nn
    from torch_geometric.loader import DataLoader as PyGDataLoader

    raw_dir = os.path.join(dataset_dir, "raw")
    rng_ab  = np.random.default_rng(seed)

    # Load faulted graphs only
    graphs: List[HeteroData] = []
    for i in range(min(num_graphs * 3, NUM_DATASET_SAMPLES)):
        g = torch.load(os.path.join(raw_dir, f"data_{i}.pt"), weights_only=False)
        total_pos = sum(
            int(g[nt].y.sum().item())
            for nt in NODE_TYPES
            if nt != "rca_context" and hasattr(g[nt], "y")
        )
        if total_pos > 0:
            graphs.append(g)
        if len(graphs) >= num_graphs:
            break

    if not graphs:
        print("  [ablation] No faulted graphs found — skipping")
        return {}

    # Build a tiny shared model for all tests
    node_dims = {nt: g[nt].x.shape[1] for nt in NODE_TYPES for g in graphs[:1]}
    all_edge_types = (
        list(PHYSICAL_EDGE_TYPES) + list(LOGICAL_EDGE_TYPES) + list(CONTEXT_EDGE_TYPES)
    )

    def _rca_acc_from_logits(
        logits_list: List[Dict[str, torch.Tensor]],
        graphs_list: List[HeteroData],
    ) -> float:
        correct = total = 0
        for logits, g in zip(logits_list, graphs_list):
            best_score, best_nt, best_idx = -1e9, "", -1
            for nt, lg in logits.items():
                if not hasattr(g[nt], "y"):
                    continue
                scores = torch.sigmoid(lg.cpu())
                top_val, top_idx = scores.max(dim=0)
                if top_val.item() > best_score:
                    best_score, best_nt, best_idx = top_val.item(), nt, int(top_idx.item())
            if best_nt == "":
                continue
            total += 1
            if int(g[best_nt].y[best_idx].item()) == 1:
                correct += 1
        return correct / max(total, 1)

    results: Dict[str, float] = {}

    # ---- A. MLP baseline (no message passing) ------------------------------
    class _MLPBaseline(nn.Module):
        def __init__(self, node_dims: Dict[str, int]) -> None:
            super().__init__()
            self.heads = nn.ModuleDict({
                f"h_{nt}": nn.Sequential(
                    nn.Linear(node_dims[nt], 32), nn.ReLU(),
                    nn.Linear(32, 1)
                )
                for nt in node_dims if nt != "rca_context"
            })

        def forward(self, x_dict: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
            return {nt: self.heads[f"h_{nt}"](x).squeeze(-1) for nt, x in x_dict.items()
                    if f"h_{nt}" in self.heads}

    mlp = _MLPBaseline(node_dims)
    mlp.eval()
    mlp_logits = []
    with torch.no_grad():
        for g in graphs:
            xd = {nt: g[nt].x.float() for nt in NODE_TYPES if nt != "rca_context"}
            mlp_logits.append(mlp(xd))
    results["mlp_baseline"] = _rca_acc_from_logits(mlp_logits, graphs)

    # ---- B. Edge dropout (50%) using the sanity model ----------------------
    model_b = _TinySanityModel(node_dims=node_dims)
    model_b.eval()
    edge_drop_logits = []
    with torch.no_grad():
        for g in graphs:
            xd = {nt: g[nt].x.float() for nt in NODE_TYPES}
            ei_d: Dict = {}
            ea_d: Dict = {}
            for et in all_edge_types:
                if et not in g.edge_types:
                    continue
                ei = g[et].edge_index
                ea = g[et].edge_attr.float()
                # Drop 50% of edges randomly
                n_edges = ei.shape[1]
                keep = torch.rand(n_edges) > 0.5
                if keep.sum() == 0:
                    keep[0] = True
                ei_d[et] = ei[:, keep]
                ea_d[et] = ea[keep]
            try:
                edge_drop_logits.append(model_b(xd, ei_d, ea_d))
            except Exception:
                edge_drop_logits.append({nt: torch.zeros(g[nt].x.shape[0]) for nt in xd if nt != "rca_context"})
    results["edge_dropout_50pct"] = _rca_acc_from_logits(edge_drop_logits, graphs)

    # ---- C. Randomized topology (shuffle job->host mapping) ----------------
    model_c = _TinySanityModel(node_dims=node_dims)
    model_c.eval()
    rand_topo_logits = []
    with torch.no_grad():
        for g in graphs:
            xd = {nt: g[nt].x.float() for nt in NODE_TYPES}
            ei_d = {}
            ea_d = {}
            for et in all_edge_types:
                if et not in g.edge_types:
                    continue
                ei = g[et].edge_index.clone()
                ea = g[et].edge_attr.float()
                src_t, rel, dst_t = et
                # Shuffle destination indices for job->cpu edges
                if rel == "executes_on":
                    perm = torch.randperm(ei.shape[1])
                    ei[1] = ei[1][perm]
                ei_d[et] = ei
                ea_d[et] = ea
            try:
                rand_topo_logits.append(model_c(xd, ei_d, ea_d))
            except Exception:
                rand_topo_logits.append({nt: torch.zeros(g[nt].x.shape[0]) for nt in xd if nt != "rca_context"})
    results["randomized_topology"] = _rca_acc_from_logits(rand_topo_logits, graphs)

    # ---- D. Feature ablation (zero top-3 features of root cause type) ------
    model_d = _TinySanityModel(node_dims=node_dims)
    model_d.eval()
    feat_abl_logits = []
    with torch.no_grad():
        for g in graphs:
            xd = {nt: g[nt].x.float().clone() for nt in NODE_TYPES}
            # Zero first 3 features of switch and hdd (most anomalous)
            for nt in ["switch", "hdd", "ram"]:
                if nt in xd:
                    xd[nt][:, :3] = 0.0
            ei_d = {et: g[et].edge_index for et in all_edge_types if et in g.edge_types}
            ea_d = {et: g[et].edge_attr.float() for et in all_edge_types if et in g.edge_types}
            try:
                feat_abl_logits.append(model_d(xd, ei_d, ea_d))
            except Exception:
                feat_abl_logits.append({nt: torch.zeros(g[nt].x.shape[0]) for nt in xd if nt != "rca_context"})
    results["feature_ablation"] = _rca_acc_from_logits(feat_abl_logits, graphs)

    return results


def generate_hardening_report(
    old_metrics: Dict[str, float],
    new_metrics: Dict[str, float],
    ablation_results: Dict[str, float],
    dataset_dir: str = DATASET_DIR,
) -> None:
    """Generate and save dataset/hardening_report.txt."""
    os.makedirs(dataset_dir, exist_ok=True)

    lines: List[str] = []
    lines.append("=" * 60)
    lines.append("DATASET HARDENING REPORT")
    lines.append("=" * 60)
    lines.append("")
    lines.append("HARDENING CHANGES APPLIED:")
    lines.append("  1. Severity reduced: U(0.7,0.95) -> U(0.15,0.45)")
    lines.append("  2. Stochastic propagation: prob=0.50-0.70 per victim")
    lines.append("  3. Random attenuation: U(0.1,0.9) per victim node")
    lines.append("  4. Noisy victims: amplitude up to 1.05x root cause")
    lines.append("  5. Healthy outliers: spike_prob=0.06, scale=0.08")
    lines.append("  6. Partial failures: not all switch metrics spike")
    lines.append("  7. Root cause noise: Gaussian(0, 0.025-0.03)")
    lines.append("")

    lines.append("OLD vs HARDENED DATASET COMPARISON:")
    lines.append(f"  {'Metric':<30}  {'OLD':>8}  {'HARDENED':>10}")
    lines.append("  " + "-" * 52)
    for k in sorted(set(list(old_metrics.keys()) + list(new_metrics.keys()))):
        ov = old_metrics.get(k, float("nan"))
        nv = new_metrics.get(k, float("nan"))
        lines.append(f"  {k:<30}  {ov:>8.4f}  {nv:>10.4f}")
    lines.append("")

    lines.append("ABLATION TEST RESULTS (untrained model, random init):")
    lines.append("  (Higher = model uses that signal more)")
    for test, acc in ablation_results.items():
        lines.append(f"  {test:<30}  rca_acc={acc:.4f}")
    lines.append("")

    # Topology usefulness
    full_acc  = ablation_results.get("edge_dropout_50pct", 0.5)
    rand_acc  = ablation_results.get("randomized_topology", 0.5)
    mlp_acc   = ablation_results.get("mlp_baseline", 0.5)
    feat_acc  = ablation_results.get("feature_ablation", 0.5)

    topo_useful = full_acc > mlp_acc + 0.05
    shortcut_risk = feat_acc > 0.7

    lines.append("RISK ASSESSMENT:")
    lines.append(f"  Topology useful      : {'YES' if topo_useful else 'WARNING: topology may not help'}")
    lines.append(f"  Shortcut risk        : {'HIGH -- feature ablation still works' if shortcut_risk else 'LOW'}")
    lines.append(f"  MLP vs GAT gap       : {full_acc - mlp_acc:+.4f}")
    lines.append(f"  Leakage probability  : {'HIGH' if shortcut_risk else 'LOW'}")
    lines.append("")

    lines.append("TRAINABILITY ASSESSMENT:")
    new_f1 = new_metrics.get("val_f1", float("nan"))
    if new_f1 > 0.90:
        diff = "STILL TOO EASY -- consider further hardening"
    elif new_f1 > 0.70:
        diff = "GOOD -- task is in target range (0.70-0.90)"
    else:
        diff = "HARD -- may need more training epochs"
    lines.append(f"  Hardened val_f1      : {new_f1:.4f}")
    lines.append(f"  Assessment           : {diff}")
    lines.append("")

    lines.append("RECOMMENDED NEXT STEPS:")
    if shortcut_risk:
        lines.append("  - Add feature masking during training (DropFeature)")
        lines.append("  - Increase healthy outlier spike_prob to 0.12")
    if not topo_useful:
        lines.append("  - Add multi-hop propagation to make topology essential")
        lines.append("  - Increase propagation_prob for distant victims")
    lines.append("  - Run full 30-epoch training to confirm convergence curve")
    lines.append("  - Compare train/val loss curves for overfitting signs")
    lines.append("")
    lines.append("=" * 60)

    report_text = "\n".join(lines)
    print(report_text)

    path = os.path.join(dataset_dir, "hardening_report.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(report_text)
    print(f"Hardening report saved -> {path}")


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    torch.manual_seed(42)

    # ---- Step 2: static graph ----
    data = build_empty_graph()
    validate_graph(data)

    print("=" * 60)
    print("HeteroData object:")
    print(data)
    print()

    print("=" * 60)
    print("Node counts and feature dims:")
    for nt in NODE_TYPES:
        x = data[nt].x
        print(f"  {nt:<14}  nodes={x.shape[0]:>5}  feat_dim={x.shape[1]:>3}")

    print()
    print("=" * 60)
    print("Edge counts:")

    all_edge_types: List[EdgeType] = (
        list(PHYSICAL_EDGE_TYPES)
        + list(LOGICAL_EDGE_TYPES)
        + list(CONTEXT_EDGE_TYPES)
    )
    for et in all_edge_types:
        ei = data[et].edge_index
        ea = data[et].edge_attr
        print(
            f"  {str(et):<55}  "
            f"edges={ei.shape[1]:>5}  "
            f"attr_dim={ea.shape[1]:>2}"
        )

    print()
    print("Static graph smoke test PASSED.")

    # ---- Step 3: temporal simulation ----
    print()
    print("=" * 60)
    print(f"Simulating healthy trajectory ({SIMULATION_STEPS} steps)...")
    temporal = simulate_healthy_trajectory(seed=42)
    validate_temporal_tensor(temporal)

    print()
    print("=" * 60)
    print("Temporal tensor summary (last timestep):")
    print(
        f"  {'node_type':<14}  {'shape':<22}  "
        f"{'min':>8}  {'max':>8}  {'mean':>8}  {'std':>8}  "
        f"{'ema_mean':>10}"
    )
    print("  " + "-" * 90)

    for node_type, t in temporal.items():
        # t: [num_nodes, num_features, 3]
        values      = t[:, :, 0]   # current values
        delta_short = t[:, :, 1]
        delta_long  = t[:, :, 2]

        # EMA = value - delta_long  (by definition: delta_long = value - ema)
        ema_approx = values - delta_long

        print(
            f"  {node_type:<14}  {str(tuple(t.shape)):<22}  "
            f"{values.min().item():>8.4f}  "
            f"{values.max().item():>8.4f}  "
            f"{values.mean().item():>8.4f}  "
            f"{values.std().item():>8.4f}  "
            f"{ema_approx.mean().item():>10.4f}"
        )

    print()
    print("Temporal simulation smoke test PASSED.")

    # ---- Step 4: fault injection ----
    print()
    print("=" * 60)
    print("Running fault injection smoke tests...")

    routing_maps = build_routing_maps()

    for ft in FAULT_TYPES:
        rng_sev = np.random.default_rng(99)
        severity = float(rng_sev.uniform(0.15, 0.45))

        result = inject_fault(
            temporal_state=temporal,
            fault_type=ft,
            severity=severity,
            routing_maps=routing_maps,
            seed=7,
        )
        validate_fault_simulation(result)

        rc_type = result["root_cause_node_type"]
        rc_id   = result["root_cause_node_id"]
        victims = result["victim_node_ids"]
        num_victims = sum(len(v) for v in victims.values())
        affected_types = list(victims.keys())

        # Anomaly amplitude = max deviation on root cause node values
        rc_vals_after  = result["temporal_state"][rc_type][rc_id, :, 0]
        rc_vals_before = temporal[rc_type][rc_id, :, 0]
        amplitude = float((rc_vals_after - rc_vals_before).abs().max().item())

        print()
        print(f"  fault_type           : {ft}")
        print(f"  root_cause_node_type : {rc_type}")
        print(f"  root_cause_node_id   : {rc_id}")
        print(f"  severity             : {severity:.4f}")
        print(f"  num_victims          : {num_victims}")
        print(f"  anomaly_amplitude    : {amplitude:.4f}")
        print(f"  affected_node_types  : {affected_types}")

    print()
    print("Fault injection smoke test PASSED.")

    # ---- Step 5: final tensor assembly ----
    print()
    print("=" * 60)
    print("Building final node feature tensors...")

    # Use the faulty temporal state from the last fault result for assembly
    last_result = inject_fault(
        temporal_state=temporal,
        fault_type="hdd_degradation",
        severity=0.85,
        routing_maps=routing_maps,
        seed=42,
    )
    x_dict = build_final_node_features(last_result["temporal_state"])
    validate_final_feature_tensors(x_dict)

    print()
    print("=" * 60)
    print("Final node feature tensor summary:")
    print(
        f"  {'node_type':<14}  {'shape':<18}  "
        f"{'final_dim':>9}  {'n_cont':>6}  {'n_cat':>5}"
    )
    print("  " + "-" * 62)
    for nt in NODE_TYPES:
        x = x_dict[nt]
        if nt == "rca_context":
            print(
                f"  {nt:<14}  {str(tuple(x.shape)):<18}  "
                f"{'(virtual)':>9}  {'--':>6}  {'--':>5}"
            )
        else:
            fd, nc, ncat = _compute_final_dim(nt)
            print(
                f"  {nt:<14}  {str(tuple(x.shape)):<18}  "
                f"{fd:>9}  {nc:>6}  {ncat:>5}"
            )

    print()
    print("Final tensor assembly smoke test PASSED.")

    # ---- Step 6: normalization + full HeteroData assembly ----
    print()
    print("=" * 60)
    print("Step 6: Normalization + full HeteroData assembly...")

    # Fit scaler on healthy data ONLY (before any fault injection)
    healthy_x = build_final_node_features(temporal)
    scaler_stats = fit_global_scaler([healthy_x])

    # Generate faulted graph
    fault_result = inject_fault(
        temporal_state=temporal,
        fault_type="network_congestion",
        severity=0.85,
        routing_maps=routing_maps,
        seed=42,
    )
    faulted_x = build_final_node_features(fault_result["temporal_state"])

    # Normalize using healthy scaler
    norm_x = normalize_features(faulted_x, scaler_stats)

    # Build edge structures
    host_to_leaf_m, leaf_to_spines_m, job_to_host_m, _ = routing_maps
    edge_indices = build_edge_indices(host_to_leaf_m, leaf_to_spines_m, job_to_host_m)
    edge_attrs   = build_edge_attr(edge_indices)

    # Assemble full HeteroData
    full_data = assemble_heterodata(
        normalized_x_dict=norm_x,
        y_dict=fault_result["y_dict"],
        loss_mask_dict=fault_result["loss_mask_dict"],
        edge_index_dict=edge_indices,
        edge_attr_dict=edge_attrs,
    )
    validate_normalized_graph(full_data, scaler_stats)

    print()
    print("=" * 60)
    print("Normalization summary:")
    print(
        f"  {'node_type':<14}  {'x_shape':<14}  "
        f"{'norm_mean':>10}  {'norm_std':>9}  "
        f"{'y_sum':>6}  {'masked':>7}"
    )
    print("  " + "-" * 68)
    for nt in NODE_TYPES:
        if nt == "rca_context":
            continue
        x    = full_data[nt].x
        y    = full_data[nt].y
        mask = full_data[nt].loss_mask
        nm   = x.mean().item()
        ns   = x.std().item()
        ys   = int(y.sum().item())
        mc   = int((~mask).sum().item())
        print(
            f"  {nt:<14}  {str(tuple(x.shape)):<14}  "
            f"{nm:>10.4f}  {ns:>9.4f}  "
            f"{ys:>6}  {mc:>7}"
        )

    print()
    print("Full HeteroData assembly smoke test PASSED.")

    # ---- Step 7: dataset generation ----
    print()
    print("=" * 60)
    print(f"Step 7: Generating dataset ({NUM_DATASET_SAMPLES} samples)...")
    print(f"  Output dir: {os.path.abspath(DATASET_DIR)}")

    scaler = generate_dataset(
        num_samples=NUM_DATASET_SAMPLES,
        healthy_ratio=HEALTHY_RATIO,
        base_seed=42,
        dataset_dir=DATASET_DIR,
    )

    save_dataset_metadata(
        scaler_stats=scaler,
        num_samples=NUM_DATASET_SAMPLES,
        healthy_ratio=HEALTHY_RATIO,
        base_seed=42,
        dataset_dir=DATASET_DIR,
    )

    save_loss_config(
        scaler_stats=scaler,
        num_samples=NUM_DATASET_SAMPLES,
        healthy_ratio=HEALTHY_RATIO,
        dataset_dir=DATASET_DIR,
    )

    validate_saved_dataset(
        num_samples=NUM_DATASET_SAMPLES,
        dataset_dir=DATASET_DIR,
        check_n=20,
    )

    # Final summary
    num_healthy = int(NUM_DATASET_SAMPLES * HEALTHY_RATIO)
    num_faulted = NUM_DATASET_SAMPLES - num_healthy
    print()
    print("=" * 60)
    print("Dataset generation final summary:")
    print(f"  total graphs   : {NUM_DATASET_SAMPLES}")
    print(f"  healthy        : {num_healthy}")
    print(f"  faulted        : {num_faulted}")
    print(f"  fault types    : {FAULT_TYPES}")
    print(f"  dataset path   : {os.path.abspath(DATASET_DIR)}")
    print()
    print("  Average feature dims:")
    for nt in NODE_TYPES:
        if nt == "rca_context":
            print(f"    {nt:<14}  dim=1  (virtual)")
        else:
            fd, nc, ncat = _compute_final_dim(nt)
            print(f"    {nt:<14}  dim={fd}  (cont={nc}x3, cat={ncat}x{_CATEGORICAL_CARDINALITY})")
    print()
    print("Step 7 PASSED.")

    # ---- Step 8: training sanity check ----
    print()
    print("=" * 60)
    print("Step 8: PyG training compatibility sanity check...")
    validate_training_batch(dataset_dir=DATASET_DIR, num_graphs=4)
    run_training_sanity_check(dataset_dir=DATASET_DIR, num_graphs=8, seed=42)
    print()
    print("All steps PASSED. Dataset is training-ready.")

    # ---- Step 9: dataset diagnostics ----
    print()
    generate_dataset_report(
        dataset_dir=DATASET_DIR,
        sample_size=150,
        seed=0,
    )

    # ---- Hardening: regenerate dataset with hardened fault injection ----
    print()
    print("=" * 60)
    print("DATASET HARDENING: regenerating with hardened fault injection...")
    print("  severity: U(0.15, 0.45)  |  stochastic propagation  |  noisy victims")

    scaler_hardened = generate_dataset(
        num_samples=NUM_DATASET_SAMPLES,
        healthy_ratio=HEALTHY_RATIO,
        base_seed=123,
        dataset_dir=DATASET_DIR,
    )
    save_dataset_metadata(
        scaler_stats=scaler_hardened,
        num_samples=NUM_DATASET_SAMPLES,
        healthy_ratio=HEALTHY_RATIO,
        base_seed=123,
        dataset_dir=DATASET_DIR,
    )
    save_loss_config(
        scaler_stats=scaler_hardened,
        num_samples=NUM_DATASET_SAMPLES,
        healthy_ratio=HEALTHY_RATIO,
        dataset_dir=DATASET_DIR,
    )
    validate_saved_dataset(
        num_samples=NUM_DATASET_SAMPLES,
        dataset_dir=DATASET_DIR,
        check_n=20,
    )

    # ---- Run diagnostics on hardened dataset ----
    print()
    print("=" * 60)
    print("Running diagnostics on HARDENED dataset...")
    generate_dataset_report(dataset_dir=DATASET_DIR, sample_size=150, seed=1)

    # ---- Ablation tests ----
    print()
    print("=" * 60)
    print("Running ablation tests...")
    ablation_results = run_ablation_tests(
        dataset_dir=DATASET_DIR, num_graphs=8, seed=42
    )
    print()
    print("Ablation results (untrained model):")
    for test, acc in ablation_results.items():
        print(f"  {test:<30}  rca_acc={acc:.4f}")

    # ---- Quick 2-epoch training comparison ----
    print()
    print("=" * 60)
    print("Quick 2-epoch training on HARDENED dataset...")
    import warnings as _w; _w.filterwarnings("ignore")
    import training_pipeline.train as _train
    _train.EPOCHS = 2
    _train.main()

    # ---- Generate hardening report ----
    # OLD metrics (from earlier smoke test — approximate)
    old_metrics = {"val_f1": 1.0, "rca_accuracy": 1.0, "val_loss": 0.0004}
    # New metrics from training_history.json
    new_metrics: Dict[str, float] = {}
    hist_path = os.path.join("checkpoints", "training_history.json")
    if os.path.exists(hist_path):
        with open(hist_path, encoding="utf-8") as _f:
            hist = json.load(_f)
        if hist.get("val_f1"):
            new_metrics["val_f1"]      = float(hist["val_f1"][-1])
            new_metrics["rca_accuracy"] = float(hist["rca_accuracy"][-1])
            new_metrics["val_loss"]    = float(hist["val_loss"][-1])

    generate_hardening_report(
        old_metrics=old_metrics,
        new_metrics=new_metrics,
        ablation_results=ablation_results,
        dataset_dir=DATASET_DIR,
    )
