"""
training_pipeline/train.py

Step 10: Production-grade training pipeline for heterogeneous GATv2
RCA model on the synthetic supercomputer cluster dataset.

Node-level binary classification: identify the root-cause node.
Masking policy: victims are excluded from loss (loss_mask == True only).
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset
from torch_geometric.data import HeteroData
from torch_geometric.loader import DataLoader
from torch_geometric.nn import GATv2Conv, HeteroConv
from torch_geometric.nn import Linear as PyGLinear

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT        = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASET_DIR  = os.path.join(_ROOT, "dataset")
RAW_DIR      = os.path.join(DATASET_DIR, "raw")
CKPT_DIR     = os.path.join(_ROOT, "checkpoints")
MANIFEST_DIR     = os.path.join(DATASET_DIR, "manifest")
METADATA_PATH    = os.path.join(DATASET_DIR, "metadata.json")
LOSS_CFG_PATH    = os.path.join(DATASET_DIR, "loss_config.json")
BEST_MODEL_PATH  = os.path.join(CKPT_DIR, "best_model.pt")
HISTORY_PATH     = os.path.join(CKPT_DIR, "training_history.json")

# Versioning support (optional — training works without manifest present)
try:
    from training_pipeline.versioning import (
        build_checkpoint_versioning_metadata,
        check_checkpoint_compatibility,
        get_dataset_hash_from_manifest,
        get_dataset_version_from_manifest,
        compute_behavioral_code_hash,
        compute_feature_ordering_hash,
    )
    from training_pipeline.experiment_registry import (
        new_experiment_id,
        append_experiment,
        build_experiment_metadata,
    )
    from training_pipeline.config import FEATURE_SCHEMA
    _VERSIONING_AVAILABLE = True
except ImportError:
    _VERSIONING_AVAILABLE = False

# ---------------------------------------------------------------------------
# Hyperparameters
# ---------------------------------------------------------------------------

HIDDEN_DIM   = 64
HEADS        = 4
DROPOUT      = 0.1
LR           = 1e-3
WEIGHT_DECAY = 1e-4
EPOCHS       = 30
BATCH_SIZE   = 4
PATIENCE     = 5
TRAIN_RATIO  = 0.70
VAL_RATIO    = 0.15
SEED         = 42

EDGE_DIM_PHYSICAL = 19
EDGE_DIM_DUMMY    = 1

# Physical edge relation names (must match config.py)
_PHYSICAL_RELATIONS = {
    "connected_to",
    "attached_to",
    "uplink_to",
    "rev_connected_to_cpu",
    "rev_connected_to_gpu",
    "rev_attached_to_ram",
    "rev_attached_to_hdd",
    "rev_uplink_to",
}

# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

class RCADataset(Dataset):
    """Lazy-loading dataset — reads .pt files on demand, no RAM accumulation."""

    def __init__(self, indices: List[int], raw_dir: str = RAW_DIR) -> None:
        self.indices = indices
        self.raw_dir = raw_dir

    def __len__(self) -> int:
        return len(self.indices)

    def __getitem__(self, idx: int) -> HeteroData:
        file_idx = self.indices[idx]
        path = os.path.join(self.raw_dir, f"data_{file_idx}.pt")
        return torch.load(path, weights_only=False)


def build_dataloaders(
    num_total: int,
    train_ratio: float = TRAIN_RATIO,
    batch_size: int = BATCH_SIZE,
    seed: int = SEED,
    val_ratio: float = VAL_RATIO,
) -> Tuple[DataLoader, DataLoader, DataLoader]:
    """Deterministic 3-way split -> (train, val, test) DataLoaders.

    The seed and permutation are unchanged from the original 80/20 split, so
    the test slice perm[n_train+n_val:] is a strict subset of the original
    validation region perm[800:]. A checkpoint trained on the old 80% split
    has therefore never seen the new test set — no train/test leakage.
    """
    rng = torch.Generator()
    rng.manual_seed(seed)
    perm = torch.randperm(num_total, generator=rng).tolist()

    n_train = int(num_total * train_ratio)
    n_val   = int(num_total * val_ratio)
    train_idx = perm[:n_train]
    val_idx   = perm[n_train:n_train + n_val]
    test_idx  = perm[n_train + n_val:]

    train_loader = DataLoader(RCADataset(train_idx), batch_size=batch_size, shuffle=True)
    val_loader   = DataLoader(RCADataset(val_idx),   batch_size=batch_size, shuffle=False)
    test_loader  = DataLoader(RCADataset(test_idx),  batch_size=batch_size, shuffle=False)
    return train_loader, val_loader, test_loader


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

class GATv2Hetero(nn.Module):
    """Two-layer heterogeneous GATv2 with residual connections and LayerNorm.

    Input dims are read from metadata.json at construction time.
    Output: Dict[node_type, logits]  shape [N] per node type.
    """

    def __init__(
        self,
        node_dims: Dict[str, int],
        edge_types: List[Tuple[str, str, str]],
        hidden_dim: int = HIDDEN_DIM,
        heads: int = HEADS,
        dropout: float = DROPOUT,
    ) -> None:
        super().__init__()

        self.node_types  = list(node_dims.keys())
        self.edge_types  = edge_types
        self.hidden_dim  = hidden_dim
        self.heads       = heads
        self.dropout_p   = dropout
        self.out_dim     = hidden_dim  # heads are concatenated then projected

        # ---- Input projections (per node type) ----------------------------
        self.input_proj = nn.ModuleDict({
            f"proj_{nt}": nn.Linear(node_dims[nt], hidden_dim, bias=False)
            for nt in self.node_types
        })

        # ---- Layer norms after each GATv2 layer ---------------------------
        self.ln1 = nn.ModuleDict({
            f"ln1_{nt}": nn.LayerNorm(hidden_dim)
            for nt in self.node_types
        })
        self.ln2 = nn.ModuleDict({
            f"ln2_{nt}": nn.LayerNorm(hidden_dim)
            for nt in self.node_types
        })

        # ---- GATv2 layer 1 ------------------------------------------------
        conv1_dict: Dict[Tuple[str, str, str], GATv2Conv] = {}
        for et in edge_types:
            src, rel, dst = et
            ea_dim = EDGE_DIM_PHYSICAL if rel in _PHYSICAL_RELATIONS else EDGE_DIM_DUMMY
            conv1_dict[et] = GATv2Conv(
                in_channels=(hidden_dim, hidden_dim),
                out_channels=hidden_dim // heads,
                heads=heads,
                edge_dim=ea_dim,
                dropout=dropout,
                add_self_loops=False,
                concat=True,
            )
        self.conv1 = HeteroConv(conv1_dict, aggr="sum")

        # ---- GATv2 layer 2 ------------------------------------------------
        conv2_dict: Dict[Tuple[str, str, str], GATv2Conv] = {}
        for et in edge_types:
            src, rel, dst = et
            ea_dim = EDGE_DIM_PHYSICAL if rel in _PHYSICAL_RELATIONS else EDGE_DIM_DUMMY
            conv2_dict[et] = GATv2Conv(
                in_channels=(hidden_dim, hidden_dim),
                out_channels=hidden_dim // heads,
                heads=heads,
                edge_dim=ea_dim,
                dropout=dropout,
                add_self_loops=False,
                concat=True,
            )
        self.conv2 = HeteroConv(conv2_dict, aggr="sum")

        # ---- Classifier heads (per node type, excluding rca_context) ------
        self.classifiers = nn.ModuleDict({
            f"cls_{nt}": nn.Linear(hidden_dim, 1)
            for nt in self.node_types
            if nt != "rca_context"
        })

    def forward(
        self,
        x_dict: Dict[str, torch.Tensor],
        edge_index_dict: Dict[Tuple[str, str, str], torch.Tensor],
        edge_attr_dict:  Dict[Tuple[str, str, str], torch.Tensor],
    ) -> Dict[str, torch.Tensor]:

        # ---- Input projection ---------------------------------------------
        h: Dict[str, torch.Tensor] = {}
        for nt in self.node_types:
            h[nt] = F.elu(self.input_proj[f"proj_{nt}"](x_dict[nt].float()))

        # ---- Layer 1 + residual + LayerNorm --------------------------------
        h1 = self.conv1(h, edge_index_dict, edge_attr_dict=edge_attr_dict)
        h1_out: Dict[str, torch.Tensor] = {}
        for nt in self.node_types:
            if nt in h1:
                res = h1[nt] + h[nt]          # residual
                res = F.dropout(res, p=self.dropout_p, training=self.training)
                h1_out[nt] = self.ln1[f"ln1_{nt}"](res)
            else:
                h1_out[nt] = h[nt]

        # ---- Layer 2 + residual + LayerNorm --------------------------------
        h2 = self.conv2(h1_out, edge_index_dict, edge_attr_dict=edge_attr_dict)
        h2_out: Dict[str, torch.Tensor] = {}
        for nt in self.node_types:
            if nt in h2:
                res = h2[nt] + h1_out[nt]
                res = F.dropout(res, p=self.dropout_p, training=self.training)
                h2_out[nt] = self.ln2[f"ln2_{nt}"](res)
            else:
                h2_out[nt] = h1_out[nt]

        # ---- Classifier heads ---------------------------------------------
        logits: Dict[str, torch.Tensor] = {}
        for nt in self.node_types:
            if nt == "rca_context":
                continue
            logits[nt] = self.classifiers[f"cls_{nt}"](h2_out[nt]).squeeze(-1)

        return logits


# ---------------------------------------------------------------------------
# Loss
# ---------------------------------------------------------------------------

def compute_masked_loss(
    logits: Dict[str, torch.Tensor],
    batch: HeteroData,
    pos_weights: Dict[str, torch.Tensor],
    device: torch.device,
) -> torch.Tensor:
    """BCEWithLogitsLoss with soft victim weighting.

    When data[nt].node_weight is present (hardened dataset):
        loss = mean(BCE(logit, y) * node_weight)
        RC nodes  → weight 1.0  (full gradient)
        Healthy   → weight 1.0
        Victims   → weight U(0.2, 0.4)  (soft gradient, not excluded)

    Fallback (legacy dataset without node_weight):
        Victims (loss_mask == False) are excluded entirely — original behaviour.
    """
    total_loss   = torch.tensor(0.0, device=device)
    n_terms      = 0

    for nt, lg in logits.items():
        if not hasattr(batch[nt], "y") or batch[nt].y is None:
            continue
        y  = batch[nt].y.float().to(device)
        pw = pos_weights.get(nt, torch.tensor(1.0)).to(device)
        bce = nn.BCEWithLogitsLoss(pos_weight=pw, reduction="none")
        node_loss = bce(lg, y)   # [N]

        if hasattr(batch[nt], "node_weight") and batch[nt].node_weight is not None:
            # Soft weighting path (hardened dataset)
            w = batch[nt].node_weight.float().to(device)  # [N]
            denom = w.sum().clamp(min=1e-6)
            total_loss = total_loss + (node_loss * w).sum() / denom
            n_terms    += 1
        else:
            # Hard-mask fallback (legacy dataset)
            if not hasattr(batch[nt], "loss_mask"):
                continue
            mask = batch[nt].loss_mask.to(device)
            if mask.sum() == 0:
                continue
            total_loss = total_loss + node_loss[mask].mean()
            n_terms    += 1

    if n_terms == 0:
        return torch.tensor(0.0, device=device, requires_grad=True)

    return total_loss / n_terms


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def compute_metrics(
    all_logits: Dict[str, List[torch.Tensor]],
    all_labels: Dict[str, List[torch.Tensor]],
    all_masks:  Dict[str, List[torch.Tensor]],
) -> Dict[str, float]:
    """Compute precision, recall, F1, ROC-AUC per node type and global.

    Implemented without sklearn to avoid NumPy 2.x compatibility issues.
    """

    def _roc_auc(labels: torch.Tensor, probs: torch.Tensor) -> float:
        """Compute ROC-AUC via trapezoidal rule (pure torch)."""
        n_pos = int(labels.sum().item())
        n_neg = int((1 - labels).sum().item())
        if n_pos == 0 or n_neg == 0:
            return 0.5
        # Sort by descending probability
        order = torch.argsort(probs, descending=True)
        labs_sorted = labels[order].float()
        tps = labs_sorted.cumsum(0)
        fps = (1 - labs_sorted).cumsum(0)
        tpr = tps / n_pos
        fpr = fps / n_neg
        # Prepend (0,0)
        tpr = torch.cat([torch.zeros(1), tpr])
        fpr = torch.cat([torch.zeros(1), fpr])
        auc = float(torch.trapz(tpr, fpr).item())
        return max(0.0, min(1.0, auc))

    def _prf(labels: torch.Tensor, preds: torch.Tensor) -> Tuple[float, float, float]:
        tp = int(((preds == 1) & (labels == 1)).sum().item())
        fp = int(((preds == 1) & (labels == 0)).sum().item())
        fn = int(((preds == 0) & (labels == 1)).sum().item())
        precision = tp / max(tp + fp, 1)
        recall    = tp / max(tp + fn, 1)
        f1        = 2 * precision * recall / max(precision + recall, 1e-8)
        return precision, recall, f1

    per_type: Dict[str, Dict[str, float]] = {}
    global_preds:  List[int]   = []
    global_labels: List[int]   = []
    global_probs:  List[float] = []

    for nt in all_logits:
        if not all_logits[nt]:
            continue
        lg  = torch.cat(all_logits[nt], dim=0).cpu()
        y   = torch.cat(all_labels[nt], dim=0).cpu().long()
        msk = torch.cat(all_masks[nt],  dim=0).cpu()

        lg_m = lg[msk]
        y_m  = y[msk]

        if len(y_m) == 0 or y_m.sum() == 0:
            continue

        probs = torch.sigmoid(lg_m)
        preds = (probs >= 0.5).long()

        p, r, f1 = _prf(y_m, preds)
        auc      = _roc_auc(y_m.float(), probs)

        per_type[nt] = {"precision": p, "recall": r, "f1": f1, "roc_auc": auc}

        global_preds.extend(preds.tolist())
        global_labels.extend(y_m.tolist())
        global_probs.extend(probs.tolist())

    if not global_labels or sum(global_labels) == 0:
        global_metrics = {"precision": 0.0, "recall": 0.0, "f1": 0.0, "roc_auc": 0.5}
    else:
        gl = torch.tensor(global_labels, dtype=torch.long)
        gp = torch.tensor(global_preds,  dtype=torch.long)
        gpr = torch.tensor(global_probs, dtype=torch.float32)
        p, r, f1 = _prf(gl, gp)
        auc = _roc_auc(gl.float(), gpr)
        global_metrics = {"precision": p, "recall": r, "f1": f1, "roc_auc": auc}

    return {"global": global_metrics, "per_type": per_type}


def compute_rca_accuracy(
    logits_list: List[Dict[str, torch.Tensor]],
    graphs_list: List[HeteroData],
) -> float:
    """Top-1 Root Cause Accuracy.

    For each faulted graph: find the node with the highest logit across
    all node types, check if it matches the true root cause (y==1).
    Healthy graphs (no positive label) are excluded.
    """
    correct = 0
    total   = 0

    for logits, g in zip(logits_list, graphs_list):
        # Collect all (logit, node_type, local_idx) for this graph
        best_score: float = -1e9
        best_nt:    str   = ""
        best_idx:   int   = -1

        for nt, lg in logits.items():
            if not hasattr(g[nt], "y"):
                continue
            scores = torch.sigmoid(lg.cpu())
            top_val, top_idx = scores.max(dim=0)
            if top_val.item() > best_score:
                best_score = top_val.item()
                best_nt    = nt
                best_idx   = int(top_idx.item())

        if best_nt == "":
            continue

        # Check if this graph has a positive label at all
        has_pos = any(
            int(g[nt].y.sum().item()) > 0
            for nt in logits
            if hasattr(g[nt], "y")
        )
        if not has_pos:
            continue  # healthy graph — skip

        total += 1
        true_label = int(g[best_nt].y[best_idx].item())
        if true_label == 1:
            correct += 1

    return correct / max(total, 1)


# ---------------------------------------------------------------------------
# Training / validation steps
# ---------------------------------------------------------------------------

def _batch_to_device(batch: HeteroData, device: torch.device) -> HeteroData:
    """Move all tensors in a HeteroData batch to device."""
    return batch.to(device)


def _prepare_inputs(
    batch: HeteroData,
    edge_types: List[Tuple[str, str, str]],
    device: torch.device,
) -> Tuple[
    Dict[str, torch.Tensor],
    Dict[Tuple[str, str, str], torch.Tensor],
    Dict[Tuple[str, str, str], torch.Tensor],
]:
    x_dict: Dict[str, torch.Tensor] = {}
    for nt in batch.node_types:
        x_dict[nt] = batch[nt].x.float().to(device)

    ei_dict: Dict[Tuple[str, str, str], torch.Tensor] = {}
    ea_dict: Dict[Tuple[str, str, str], torch.Tensor] = {}
    for et in edge_types:
        if et in batch.edge_types:
            ei_dict[et] = batch[et].edge_index.to(device)
            ea_dict[et] = batch[et].edge_attr.float().to(device)

    return x_dict, ei_dict, ea_dict


def train_one_epoch(
    model: GATv2Hetero,
    loader: DataLoader,
    optimizer: torch.optim.Optimizer,
    pos_weights: Dict[str, torch.Tensor],
    device: torch.device,
    edge_types: List[Tuple[str, str, str]],
    grad_clip: float = 1.0,
) -> float:
    """Run one training epoch. Returns mean train loss."""
    model.train()
    total_loss = 0.0
    n_batches  = 0

    for batch in loader:
        batch = _batch_to_device(batch, device)
        x_dict, ei_dict, ea_dict = _prepare_inputs(batch, edge_types, device)

        optimizer.zero_grad()
        logits = model(x_dict, ei_dict, ea_dict)

        loss = compute_masked_loss(logits, batch, pos_weights, device)

        if torch.isnan(loss) or torch.isinf(loss):
            print(f"  [WARN] NaN/Inf loss detected — skipping batch")
            continue

        loss.backward()
        nn.utils.clip_grad_norm_(model.parameters(), grad_clip)
        optimizer.step()

        total_loss += loss.item()
        n_batches  += 1

    return total_loss / max(n_batches, 1)


def validate(
    model: GATv2Hetero,
    loader: DataLoader,
    pos_weights: Dict[str, torch.Tensor],
    device: torch.device,
    edge_types: List[Tuple[str, str, str]],
) -> Tuple[float, Dict[str, float], float]:
    """Run validation. Returns (val_loss, metrics_dict, rca_accuracy)."""
    model.eval()

    total_loss = 0.0
    n_batches  = 0

    all_logits: Dict[str, List[torch.Tensor]] = {}
    all_labels: Dict[str, List[torch.Tensor]] = {}
    all_masks:  Dict[str, List[torch.Tensor]] = {}

    # For RCA accuracy we need per-graph logits
    per_graph_logits: List[Dict[str, torch.Tensor]] = []
    per_graph_data:   List[HeteroData]               = []

    with torch.no_grad():
        for batch in loader:
            batch = _batch_to_device(batch, device)
            x_dict, ei_dict, ea_dict = _prepare_inputs(batch, edge_types, device)

            logits = model(x_dict, ei_dict, ea_dict)
            loss   = compute_masked_loss(logits, batch, pos_weights, device)

            if not (torch.isnan(loss) or torch.isinf(loss)):
                total_loss += loss.item()
                n_batches  += 1

            for nt, lg in logits.items():
                if nt not in all_logits:
                    all_logits[nt] = []
                    all_labels[nt] = []
                    all_masks[nt]  = []
                all_logits[nt].append(lg.cpu())
                if hasattr(batch[nt], "y"):
                    all_labels[nt].append(batch[nt].y.cpu())
                if hasattr(batch[nt], "loss_mask"):
                    all_masks[nt].append(batch[nt].loss_mask.cpu())

            # Unbatch for RCA accuracy (approximate: treat batch as one graph)
            per_graph_logits.append({nt: lg.cpu() for nt, lg in logits.items()})
            per_graph_data.append(batch.cpu())

    val_loss = total_loss / max(n_batches, 1)
    metrics  = compute_metrics(all_logits, all_labels, all_masks)
    rca_acc  = compute_rca_accuracy(per_graph_logits, per_graph_data)

    return val_loss, metrics, rca_acc


def evaluate_model(
    model: GATv2Hetero,
    loader: DataLoader,
    pos_weights: Dict[str, torch.Tensor],
    device: torch.device,
    edge_types: List[Tuple[str, str, str]],
) -> None:
    """Load best checkpoint and print final evaluation metrics."""
    if os.path.exists(BEST_MODEL_PATH):
        state = torch.load(BEST_MODEL_PATH, map_location=device, weights_only=False)
        model.load_state_dict(state["model_state_dict"])
        print(f"Loaded best checkpoint from epoch {state.get('epoch', '?')}")

    val_loss, metrics, rca_acc = validate(model, loader, pos_weights, device, edge_types)

    print("\n" + "=" * 60)
    print("FINAL EVALUATION")
    print("=" * 60)
    print(f"  val_loss  : {val_loss:.4f}")
    g = metrics["global"]
    print(f"  precision : {g['precision']:.4f}")
    print(f"  recall    : {g['recall']:.4f}")
    print(f"  F1        : {g['f1']:.4f}")
    print(f"  ROC-AUC   : {g['roc_auc']:.4f}")
    print(f"  RCA Top-1 : {rca_acc:.4f}")
    print()
    print("Per-node-type metrics:")
    for nt, m in metrics.get("per_type", {}).items():
        print(
            f"  {nt:<14}  "
            f"P={m['precision']:.3f}  R={m['recall']:.3f}  "
            f"F1={m['f1']:.3f}  AUC={m['roc_auc']:.3f}"
        )


# ---------------------------------------------------------------------------
# Main training loop
# ---------------------------------------------------------------------------

def main() -> None:
    torch.manual_seed(SEED)
    os.makedirs(CKPT_DIR, exist_ok=True)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Device: {device}")

    # ---- Versioning: load dataset identity --------------------------------
    _dataset_version = "unknown"
    _dataset_hash    = "unknown"
    _dataset_codename = "unknown"
    _exp_id = None

    if _VERSIONING_AVAILABLE and os.path.exists(MANIFEST_DIR):
        try:
            _dataset_hash    = get_dataset_hash_from_manifest(MANIFEST_DIR)
            _dataset_version = get_dataset_version_from_manifest(MANIFEST_DIR)
            print(f"Dataset version : {_dataset_version}")
            print(f"Dataset hash    : {_dataset_hash[:12]}...")
            _exp_id = new_experiment_id()
            print(f"Experiment ID   : {_exp_id}")
        except Exception as e:
            print(f"[versioning] Warning: could not read manifest: {e}")
    elif _VERSIONING_AVAILABLE:
        print("[versioning] No manifest found — running without version tracking")

    # ---- Load metadata and loss config ------------------------------------
    with open(METADATA_PATH, encoding="utf-8") as f:
        metadata = json.load(f)
    with open(LOSS_CFG_PATH, encoding="utf-8") as f:
        loss_cfg = json.load(f)

    node_dims: Dict[str, int] = metadata["feature_dimensions"]
    edge_types: List[Tuple[str, str, str]] = [
        tuple(et) for et in metadata["edge_types"]
    ]
    num_total: int = metadata["dataset_size"]

    pos_weights: Dict[str, torch.Tensor] = {
        nt: torch.tensor([pw], dtype=torch.float32)
        for nt, pw in loss_cfg["pos_weight"].items()
    }

    print(f"Dataset: {num_total} graphs  |  node types: {list(node_dims.keys())}")
    print(f"Edge types: {len(edge_types)}")

    # ---- DataLoaders -------------------------------------------------------
    train_loader, val_loader, test_loader = build_dataloaders(
        num_total=num_total,
        train_ratio=TRAIN_RATIO,
        batch_size=BATCH_SIZE,
        seed=SEED,
    )
    print(
        f"Train: {len(train_loader.dataset)} graphs  |  "
        f"Val: {len(val_loader.dataset)} graphs  |  "
        f"Test: {len(test_loader.dataset)} graphs (held out)"
    )

    # ---- Model -------------------------------------------------------------
    model = GATv2Hetero(
        node_dims=node_dims,
        edge_types=edge_types,
        hidden_dim=HIDDEN_DIM,
        heads=HEADS,
        dropout=DROPOUT,
    ).to(device)

    n_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"Model parameters: {n_params:,}")

    # ---- Optimizer ---------------------------------------------------------
    optimizer = torch.optim.AdamW(
        model.parameters(), lr=LR, weight_decay=WEIGHT_DECAY
    )

    # ---- Training history --------------------------------------------------
    history: Dict[str, List] = {
        "train_loss": [], "val_loss": [],
        "val_f1": [], "val_roc_auc": [], "rca_accuracy": [],
    }

    best_val_f1    = -1.0
    patience_count = 0
    best_epoch     = 0

    print()
    print("=" * 70)
    print(f"{'Epoch':>5}  {'train_loss':>10}  {'val_loss':>9}  "
          f"{'val_f1':>7}  {'rca_acc':>8}  {'time':>6}")
    print("=" * 70)

    for epoch in range(1, EPOCHS + 1):
        t0 = time.time()

        train_loss = train_one_epoch(
            model, train_loader, optimizer, pos_weights, device, edge_types
        )
        val_loss, metrics, rca_acc = validate(
            model, val_loader, pos_weights, device, edge_types
        )

        val_f1   = metrics["global"]["f1"]
        val_auc  = metrics["global"]["roc_auc"]
        elapsed  = time.time() - t0

        history["train_loss"].append(train_loss)
        history["val_loss"].append(val_loss)
        history["val_f1"].append(val_f1)
        history["val_roc_auc"].append(val_auc)
        history["rca_accuracy"].append(rca_acc)

        print(
            f"Epoch {epoch:02d} | "
            f"train_loss={train_loss:.4f} | "
            f"val_loss={val_loss:.4f} | "
            f"val_f1={val_f1:.4f} | "
            f"rca_acc={rca_acc:.4f} | "
            f"{elapsed:.1f}s"
        )

        # ---- Checkpoint best model ----------------------------------------
        if val_f1 > best_val_f1:
            best_val_f1    = val_f1
            best_epoch     = epoch
            patience_count = 0

            # Build versioning metadata block
            _versioning_block: Dict = {}
            if _VERSIONING_AVAILABLE:
                try:
                    _versioning_block = build_checkpoint_versioning_metadata(
                        dataset_version=_dataset_version,
                        dataset_hash=_dataset_hash,
                        manifest_dir=MANIFEST_DIR,
                        feature_schema=FEATURE_SCHEMA,
                        node_dims=node_dims,
                        model_config={
                            "hidden_dim": HIDDEN_DIM,
                            "heads":      HEADS,
                            "dropout":    DROPOUT,
                        },
                        train_py_path=__file__,
                    )
                except Exception as e:
                    print(f"[versioning] Warning: checkpoint metadata error: {e}")

            torch.save(
                {
                    "epoch":            epoch,
                    "model_state_dict": model.state_dict(),
                    "val_f1":           val_f1,
                    "rca_accuracy":     rca_acc,
                    "node_dims":        node_dims,
                    "edge_types":       edge_types,
                    # --- versioning block ---
                    **_versioning_block,
                },
                BEST_MODEL_PATH,
            )
        else:
            patience_count += 1
            if patience_count >= PATIENCE:
                print(f"\nEarly stopping at epoch {epoch} (patience={PATIENCE})")
                break

    # ---- Save training history --------------------------------------------
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)
    print(f"\nTraining history saved -> {HISTORY_PATH}")
    print(f"Best model: epoch {best_epoch}  val_f1={best_val_f1:.4f}")

    # ---- Final evaluation on best checkpoint ------------------------------
    evaluate_model(model, val_loader, pos_weights, device, edge_types)

    # ---- Log experiment to registry ----------------------------------------
    if _VERSIONING_AVAILABLE and _exp_id is not None:
        try:
            _train_end = time.time()
            _final_metrics = history.get("val_roc_auc", [])
            _code_versions: Dict[str, str] = {}
            try:
                _code_versions["train_code_hash"] = compute_behavioral_code_hash(__file__)
                _code_versions["feature_ordering_hash"] = compute_feature_ordering_hash(FEATURE_SCHEMA)
            except Exception:
                pass

            exp_meta = build_experiment_metadata(
                exp_id=_exp_id,
                dataset_version=_dataset_version,
                dataset_hash=_dataset_hash,
                dataset_codename=_dataset_codename,
                model_config={
                    "model_type": "HeteroGATv2",
                    "hidden_dim":  HIDDEN_DIM,
                    "heads":       HEADS,
                    "dropout":     DROPOUT,
                },
                loss_config=loss_cfg.get("pos_weight", {}),
                optimizer_config={"optimizer": "AdamW", "lr": LR, "weight_decay": WEIGHT_DECAY},
                training_config={
                    "epochs":      EPOCHS,
                    "batch_size":  BATCH_SIZE,
                    "patience":    PATIENCE,
                    "train_ratio": TRAIN_RATIO,
                    "seed":        SEED,
                    "device":      str(device),
                },
                metrics={
                    "best_val_f1":          best_val_f1,
                    "best_epoch":           best_epoch,
                    "final_val_roc_auc":    _final_metrics[-1] if _final_metrics else None,
                    "final_rca_accuracy":   history.get("rca_accuracy", [None])[-1],
                },
                runtime={"checkpoint_path": BEST_MODEL_PATH},
                code_versions=_code_versions,
                checkpoint_path=BEST_MODEL_PATH,
                status="completed",
            )
            append_experiment(exp_meta)
            print(f"Experiment logged -> experiments/registry.jsonl  [{_exp_id}]")
        except Exception as e:
            print(f"[versioning] Warning: could not log experiment: {e}")


if __name__ == "__main__":
    # Suppress the pandas/NumPy compatibility warnings from PyG datasets import
    import warnings
    warnings.filterwarnings("ignore")
    main()
