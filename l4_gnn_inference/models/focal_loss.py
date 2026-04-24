"""Focal loss implementation module for L4 inference."""

from __future__ import annotations

from typing import Dict

import torch
from torch import Tensor
from torch import nn


class HeteroBinaryFocalLoss(nn.Module):
    """Binary focal loss for heterogeneous node-type predictions.

    The module expects dictionaries keyed by node type where each value contains
    per-node probabilities (after sigmoid) and binary targets.
    """

    def __init__(
        self,
        alpha: float = 0.25,
        gamma: float = 2.0,
        reduction: str = "mean",
        eps: float = 1e-7,
    ) -> None:
        """Initialize focal loss parameters.

        Args:
            alpha: Class balancing coefficient for positive samples.
            gamma: Focusing coefficient that down-weights easy examples.
            reduction: Reduction method, either ``"mean"`` or ``"sum"``.
            eps: Numerical stability constant for logarithm and clipping.

        Raises:
            ValueError: If reduction is not supported.
        """

        super().__init__()
        if reduction not in {"mean", "sum"}:
            raise ValueError("reduction must be either 'mean' or 'sum'.")

        self.alpha = alpha
        self.gamma = gamma
        self.reduction = reduction
        self.eps = eps

    def forward(
        self,
        preds_dict: Dict[str, Tensor],
        targets_dict: Dict[str, Tensor],
    ) -> Tensor:
        """Compute focal loss across all node types.

        Args:
            preds_dict: Predicted anomaly probabilities for each node type.
            targets_dict: Binary labels for each node type.

        Returns:
            Tensor: Scalar loss aggregated across the heterogeneous graph.

        Raises:
            ValueError: If dictionaries are empty or node types mismatch.
        """

        if not preds_dict or not targets_dict:
            raise ValueError("preds_dict and targets_dict must be non-empty.")

        pred_keys = set(preds_dict.keys())
        target_keys = set(targets_dict.keys())
        if pred_keys != target_keys:
            raise ValueError("preds_dict and targets_dict keys must match.")

        total_loss_sum: Tensor | None = None
        total_count: int = 0

        for node_type in preds_dict:
            preds = preds_dict[node_type].float().reshape(-1).clamp(self.eps, 1.0 - self.eps)
            targets = targets_dict[node_type].float().reshape(-1)

            if preds.shape != targets.shape:
                raise ValueError(
                    f"Shape mismatch for node type '{node_type}': "
                    f"preds {preds.shape} vs targets {targets.shape}."
                )

            p_t = preds * targets + (1.0 - preds) * (1.0 - targets)
            alpha_t = self.alpha * targets + (1.0 - self.alpha) * (1.0 - targets)
            focal_factor = torch.pow(1.0 - p_t, self.gamma)
            node_losses = -alpha_t * focal_factor * torch.log(p_t.clamp(min=self.eps))

            node_loss_sum = node_losses.sum()
            total_loss_sum = node_loss_sum if total_loss_sum is None else total_loss_sum + node_loss_sum
            total_count += int(node_losses.numel())

        if total_loss_sum is None:
            raise ValueError("No losses were computed from provided dictionaries.")

        if self.reduction == "sum":
            return total_loss_sum

        if total_count == 0:
            raise ValueError("Cannot compute mean focal loss with zero elements.")
        return total_loss_sum / total_count
