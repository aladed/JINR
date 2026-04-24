"""Heterogeneous GATv2 model module for L4 inference."""

from __future__ import annotations

from collections import defaultdict
from typing import DefaultDict, Dict, List, Tuple

import torch
import torch.nn.functional as F
from torch import Tensor
from torch import nn
from torch_geometric.nn import GATv2Conv, HeteroConv

EdgeType = Tuple[str, str, str]
AttentionWeights = Tuple[Tensor, Tensor]


class HeteroIncidentGATv2(nn.Module):
    """Heterogeneous GATv2 model for incident anomaly scoring.

    The model applies relation-specific GATv2 convolutions on a heterogeneous
    infrastructure graph and returns both node anomaly probabilities and
    attention coefficients for XAI pipelines.
    """

    def __init__(
        self,
        hidden_channels: int,
        out_channels: int = 1,
        num_heads: int = 4,
    ) -> None:
        """Initialize the hetero GATv2 architecture.

        Args:
            hidden_channels: Hidden channels per attention head.
            out_channels: Output channels for per-node binary anomaly score.
            num_heads: Number of attention heads used by each relation.

        Raises:
            ValueError: If out_channels is not equal to 1.
        """

        super().__init__()
        if out_channels != 1:
            raise ValueError("out_channels must be 1 for binary anomaly probability.")

        self.hidden_channels = hidden_channels
        self.out_channels = out_channels
        self.num_heads = num_heads

        self.edge_types: List[EdgeType] = [
            ("host", "connected_to", "switch"),
            ("vm", "allocated_on", "host"),
            ("job", "allocated_on", "vm"),
        ]

        self.conv1 = HeteroConv(
            {
                edge_type: GATv2Conv(
                    in_channels=-1,
                    out_channels=hidden_channels,
                    heads=num_heads,
                    add_self_loops=False,
                )
                for edge_type in self.edge_types
            },
            aggr="sum",
        )

        self.lin_dict = nn.ModuleDict(
            {
                "host": nn.Linear(hidden_channels * num_heads, out_channels),
                "vm": nn.Linear(hidden_channels * num_heads, out_channels),
                "job": nn.Linear(hidden_channels * num_heads, out_channels),
                "switch": nn.Linear(hidden_channels * num_heads, out_channels),
            }
        )

    def forward(
        self,
        x_dict: Dict[str, Tensor],
        edge_index_dict: Dict[EdgeType, Tensor],
    ) -> Tuple[Dict[str, Tensor], Dict[EdgeType, AttentionWeights]]:
        """Run forward pass and extract relation-level attention weights.

        Args:
            x_dict: Node feature tensors keyed by node type.
            edge_index_dict: Edge index tensors keyed by relation triplet.

        Returns:
            Tuple[Dict[str, Tensor], Dict[EdgeType, AttentionWeights]]:
                - ``anomaly_scores_dict``: Per-node-type anomaly probabilities.
                - ``attention_weights_dict``: Per-edge-type tuple of
                  ``(edge_index, attention_weights)`` from GATv2Conv.
        """

        out_dict: DefaultDict[str, List[Tensor]] = defaultdict(list)
        attention_weights_dict: Dict[EdgeType, AttentionWeights] = {}

        for edge_type in self.edge_types:
            if edge_type not in edge_index_dict:
                continue

            src_type, _, dst_type = edge_type
            conv_layer = self.conv1.convs[edge_type]
            conv_out = conv_layer(
                (x_dict[src_type], x_dict[dst_type]),
                edge_index_dict[edge_type],
                return_attention_weights=True,
            )

            dst_embeddings, attention_weights = conv_out
            out_dict[dst_type].append(dst_embeddings)
            attention_weights_dict[edge_type] = attention_weights

        hidden_dict: Dict[str, Tensor] = {}
        for node_type, node_features in x_dict.items():
            if node_type in out_dict and out_dict[node_type]:
                hidden_dict[node_type] = F.leaky_relu(torch.stack(out_dict[node_type], dim=0).sum(dim=0))
            else:
                num_nodes = int(node_features.size(0))
                hidden_dict[node_type] = node_features.new_zeros(
                    (num_nodes, self.hidden_channels * self.num_heads)
                )

        logits_dict: Dict[str, Tensor] = {
            node_type: self.lin_dict[node_type](hidden_dict[node_type])
            for node_type in self.lin_dict
        }
        anomaly_scores_dict: Dict[str, Tensor] = {
            node_type: torch.sigmoid(logits)
            for node_type, logits in logits_dict.items()
        }

        return anomaly_scores_dict, attention_weights_dict
