"""
features.py — Proto Batch → temporal state → x_dict

Pipeline:
  1. EntityMapper  : entity_id → (node_type, node_index)
  2. batch_to_temporal_state() : List[pb.Sample] → Dict[str, Tensor[N, F, 4]]
  3. build_final_node_features() from dataset_generator : → Dict[str, Tensor[N, D]]

Critical layout (verified against build_final_node_features):
  temporal_state[nt] shape: [num_nodes, num_features, 4]
  channels: [value, delta_short, delta_long, rolling_var]
  These come DIRECTLY from proto — Go agent already computed them.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple

import torch

from training_pipeline.config import FEATURE_SCHEMA, NODE_TYPES
from training_pipeline.dataset_generator import build_final_node_features
from snapshot_engine.topology import NODE_COUNTS, EXPECTED_DIMS

logger = logging.getLogger(__name__)

# entity_type values the model accepts (link_edge is excluded by design)
_ACCEPTED_TYPES = {"cpu", "gpu", "ram", "hdd", "switch", "job"}


# ---------------------------------------------------------------------------
# Entity mapper: entity_id string → integer node index
# ---------------------------------------------------------------------------

class EntityMapper:
    """Stateful mapper from proto entity_ids to model node indices.

    Assigns indices incrementally on first seen, capped at NODE_COUNTS.
    Separate registries per category (host, switch, job).

    entity_id formats from Go agent:
      cpu/gpu/ram/hdd: "{hostname}:{component}"  → use hostname
      switch:          "{target}:{port}"           → use target
      job:             "{hostname}:job{id}"        → use job_id label or parsed suffix
    """

    def __init__(self) -> None:
        self._host_reg:   Dict[str, int] = {}
        self._switch_reg: Dict[str, int] = {}
        self._job_reg:    Dict[str, int] = {}

    def _assign(self, registry: Dict[str, int], key: str, cap: int) -> Optional[int]:
        if key in registry:
            return registry[key]
        idx = len(registry)
        if idx >= cap:
            return None  # overflow, skip
        registry[key] = idx
        return idx

    def resolve(
        self,
        entity_type: str,
        entity_id: str,
        labels: Dict[str, str],
    ) -> Optional[int]:
        """Return integer node index, or None if entity should be dropped."""
        if entity_type not in _ACCEPTED_TYPES:
            return None

        if entity_type in ("cpu", "gpu", "ram", "hdd"):
            hostname = entity_id.split(":")[0]
            return self._assign(self._host_reg, hostname, NODE_COUNTS["cpu"])

        if entity_type == "switch":
            # entity_id like "192.168.1.1:port3" or "leaf-01:port12"
            target = entity_id.split(":")[0]
            return self._assign(self._switch_reg, target, NODE_COUNTS["switch"])

        if entity_type == "job":
            # prefer labels["job_id"], fallback to parsing entity_id suffix
            job_key = labels.get("job_id", "")
            if not job_key:
                m = re.search(r":job(.+)$", entity_id)
                job_key = m.group(1) if m else entity_id
            return self._assign(self._job_reg, job_key, NODE_COUNTS["job"])

        return None

    def reset(self) -> None:
        """Clear all registries (call between independent inference sessions)."""
        self._host_reg.clear()
        self._switch_reg.clear()
        self._job_reg.clear()


# ---------------------------------------------------------------------------
# Proto batch → temporal state
# ---------------------------------------------------------------------------

def _make_empty_temporal() -> Dict[str, torch.Tensor]:
    """Allocate zero-filled temporal tensors for all model node types."""
    state: Dict[str, torch.Tensor] = {}
    for nt in NODE_TYPES:
        if nt == "rca_context":
            continue
        n = NODE_COUNTS[nt]
        f = len(FEATURE_SCHEMA[nt])
        state[nt] = torch.zeros(n, f, 4, dtype=torch.float32)
    return state


def batch_to_temporal_state(
    samples: list,   # List of proto pb.Sample objects
    mapper: EntityMapper,
) -> Dict[str, torch.Tensor]:
    """Convert a list of proto Samples to temporal_state dict.

    Returns Dict[node_type, Tensor[N, F, 4]] ready for build_final_node_features().
    Missing metrics stay 0. entity_type="link" is silently dropped.

    One Batch = one tick = one graph (Point-in-Time Join: last sample wins per key).
    """
    state = _make_empty_temporal()
    skipped = 0

    for s in samples:
        et = s.entity_type
        if et not in _ACCEPTED_TYPES:
            skipped += 1
            continue

        # Resolve node index
        labels = dict(s.labels)
        node_idx = mapper.resolve(et, s.entity_id, labels)
        if node_idx is None:
            skipped += 1
            continue

        # Resolve feature index
        schema = FEATURE_SCHEMA.get(et)
        if schema is None:
            continue
        mn = s.metric_name
        if mn not in schema:
            continue
        feat_idx = schema.index(mn)

        # Fill 4 channels directly from proto — Go agent already computed them
        state[et][node_idx, feat_idx, 0] = s.value
        state[et][node_idx, feat_idx, 1] = s.delta_short
        state[et][node_idx, feat_idx, 2] = s.delta_long
        state[et][node_idx, feat_idx, 3] = s.rolling_var

    if skipped:
        logger.debug("batch_to_temporal_state: skipped %d samples (link/unknown/overflow)", skipped)

    return state


def assemble_x_dict(
    samples: list,
    mapper: EntityMapper,
) -> Dict[str, torch.Tensor]:
    """Full pipeline: samples → temporal_state → x_dict.

    Also adds rca_context = ones(1, 1) and validates dimensions.
    """
    state = batch_to_temporal_state(samples, mapper)
    x_dict = build_final_node_features(state)

    # Validate dimensions — hard stop if schema changed
    for nt, expected_dim in EXPECTED_DIMS.items():
        actual = x_dict[nt].shape[1]
        if actual != expected_dim:
            raise ValueError(
                f"[{nt}] dimension mismatch: expected {expected_dim}, got {actual}. "
                "Check FEATURE_SCHEMA alignment with build_final_node_features."
            )

    return x_dict
