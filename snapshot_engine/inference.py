"""
inference.py — GATv2Hetero loader and single-graph inference.

Load:
    engine = InferenceEngine.load("checkpoints/best_model.pt")

Run:
    result = engine.predict(x_dict, edge_index_dict, edge_attr_dict)

Result dict:
    rc_node_type  : str
    rc_node_id    : int
    confidence    : float   (sigmoid of top logit)
    fault_type    : str     (placeholder — training used 3 fault types)
    top5_candidates : List[{"type": str, "id": int, "score": float}]
    victim_nodes  : []      (populated downstream by publisher/RAG)
    graph_id      : int     (tick counter)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import torch

from training_pipeline.train import GATv2Hetero

logger = logging.getLogger(__name__)

_DEFAULT_CKPT = Path(__file__).parent.parent / "checkpoints" / "best_model.pt"
_FAULT_TYPES = ["hdd_degradation", "network_congestion", "ram_leak"]


class InferenceEngine:
    """Wraps GATv2Hetero for single-graph prediction."""

    def __init__(self, model: GATv2Hetero, device: torch.device) -> None:
        self._model = model
        self._device = device
        self._tick = 0

    @classmethod
    def load(
        cls,
        ckpt_path: Optional[Path] = None,
        device: Optional[torch.device] = None,
    ) -> "InferenceEngine":
        """Load model from checkpoint.

        torch.load(..., weights_only=False) required because checkpoint
        contains non-tensor objects (node_dims dict, edge_types list).
        """
        p = Path(ckpt_path) if ckpt_path else _DEFAULT_CKPT
        if not p.exists():
            raise FileNotFoundError(f"Checkpoint not found: {p}")

        dev = device or torch.device("cuda" if torch.cuda.is_available() else "cpu")
        ckpt = torch.load(str(p), map_location=dev, weights_only=False)

        model = GATv2Hetero(
            node_dims=ckpt["node_dims"],
            edge_types=ckpt["edge_types"],
        )
        model.load_state_dict(ckpt["model_state_dict"])
        model.to(dev)
        model.eval()

        logger.info(
            "Loaded GATv2Hetero from %s  (epoch=%s, val_f1=%.3f, rca_acc=%.3f)",
            p,
            ckpt.get("epoch", "?"),
            ckpt.get("val_f1", 0.0),
            ckpt.get("rca_accuracy", 0.0),
        )
        return cls(model, dev)

    @torch.no_grad()
    def predict(
        self,
        x_dict: Dict[str, torch.Tensor],
        edge_index_dict: Dict[Tuple, torch.Tensor],
        edge_attr_dict: Dict[Tuple, torch.Tensor],
    ) -> Dict[str, Any]:
        """Run forward pass and return structured prediction.

        RC = argmax sigmoid(logits) across all non-rca_context node types.
        top5 = top-5 global candidates.
        """
        self._tick += 1

        # Move tensors to device
        x_dev = {k: v.to(self._device) for k, v in x_dict.items()}
        ei_dev = {k: v.to(self._device) for k, v in edge_index_dict.items()}
        ea_dev = {k: v.to(self._device) for k, v in edge_attr_dict.items()}

        logits: Dict[str, torch.Tensor] = self._model(x_dev, ei_dev, ea_dev)

        # Collect all (score, node_type, node_id) candidates
        all_candidates: List[Dict[str, Any]] = []
        for nt, lg in logits.items():
            scores = torch.sigmoid(lg).cpu()
            for idx in range(scores.shape[0]):
                all_candidates.append({
                    "type":  nt,
                    "id":    idx,
                    "score": float(scores[idx].item()),
                })

        # Sort descending by score
        all_candidates.sort(key=lambda c: c["score"], reverse=True)

        rc = all_candidates[0]
        top5 = all_candidates[:5]

        # Heuristic fault_type from rc_node_type
        fault_map = {
            "hdd":    "hdd_degradation",
            "switch": "network_congestion",
            "ram":    "ram_leak",
        }
        fault_type = fault_map.get(rc["type"], "unknown")

        return {
            "graph_id":        self._tick,
            "fault_type":      fault_type,
            "rc_node":         {"type": rc["type"], "id": rc["id"]},
            "confidence":      round(rc["score"], 4),
            "top5_candidates": [{"id": f"{c['type']}-{c['id']}", "score": round(c['score'], 4)} for c in top5],
            "victim_nodes":    [],  # filled by downstream RAG/LLM layer
        }
