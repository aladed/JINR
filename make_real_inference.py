"""
Real inference visualization for thesis defense.
All scores come from actual model.forward() output — no fabricated values.
"""
import os, sys, json
import numpy as np
import torch
import torch.nn.functional as F

sys.path.insert(0, ".")

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyArrowPatch
import matplotlib.patheffects as pe

OUTDIR = "artifacts/visualizations"
os.makedirs(OUTDIR, exist_ok=True)

BG      = "#0d1117"
GREEN   = "#00cc44"
GOLD    = "#ffd700"
ORANGE  = "#ff8c00"
GRAY    = "#4a4a6a"
RED_ARR = "#ff4444"
WHITE   = "#e8e8e8"

plt.rcParams.update({
    "font.size": 14,
    "axes.titlesize": 16,
    "axes.labelsize": 14,
    "xtick.labelsize": 13,
    "ytick.labelsize": 13,
    "legend.fontsize": 13,
    "figure.dpi": 150,
})

# ============================================================
# STEP 1 — Real inference
# ============================================================

def run_inference():
    from evaluate_pipeline import load_model

    ckpt = "checkpoints/best_model.pt"
    if not os.path.exists(ckpt):
        print(f"ERROR: {ckpt} not found — cannot fabricate results, stopping.")
        sys.exit(1)

    device = torch.device("cpu")
    model, edge_types, _ = load_model(ckpt, device)
    model.eval()

    # Scan test split for network_congestion rank-1 sample
    rng_g = torch.Generator(); rng_g.manual_seed(42)
    perm  = torch.randperm(1000, generator=rng_g).tolist()
    test_idx = perm[850:]

    result = None
    for graph_id in test_idx:
        path = f"dataset/raw/data_{graph_id}.pt"
        if not os.path.exists(path):
            continue
        data = torch.load(path, weights_only=False)
        if data["switch"].y.sum() == 0:
            continue  # not network_congestion

        with torch.no_grad():
            out = model(data.x_dict, data.edge_index_dict, data.edge_attr_dict)

        sw_logits = out["switch"].squeeze()          # shape [6]
        sw_scores = torch.sigmoid(sw_logits)
        rc_id     = int(data["switch"].y.argmax())
        ranked    = sw_logits.argsort(descending=True).tolist()
        rc_rank   = ranked.index(rc_id) + 1          # 1-indexed

        if rc_rank == 1:
            result = {
                "graph_id":  graph_id,
                "data":      data,
                "out":       out,
                "sw_logits": sw_logits,
                "sw_scores": sw_scores,
                "rc_id":     rc_id,
                "rc_rank":   rc_rank,
                "ranked":    ranked,
            }
            break

    if result is None:
        print("ERROR: No rank-1 network_congestion sample found in test split.")
        sys.exit(1)

    graph_id  = result["graph_id"]
    data      = result["data"]
    sw_logits = result["sw_logits"]
    sw_scores = result["sw_scores"]
    rc_id     = result["rc_id"]
    ranked    = result["ranked"]

    # Victim nodes: cpu/gpu connected to RC switch via edge structure
    et_cpu = ("cpu", "connected_to", "switch")
    et_gpu = ("gpu", "connected_to", "switch")
    ei_cpu = data[et_cpu].edge_index
    ei_gpu = data[et_gpu].edge_index
    cpu_victims = ei_cpu[0][ei_cpu[1] == rc_id].tolist()
    gpu_victims = ei_gpu[0][ei_gpu[1] == rc_id].tolist()

    # Top-5 switch candidates with real scores
    top5_ids    = ranked[:5]
    top5_scores = [float(sw_scores[i]) for i in top5_ids]
    top5_labels = [f"S{i}" for i in top5_ids]
    runner_up_id = ranked[1]

    # Print summary to console
    print(f"Sample selected: graph_id={graph_id}")
    print(f"True RC: Switch S{rc_id} (node_id={rc_id})")
    print(f"Model rank of RC: {result['rc_rank']}")
    top5_str = ", ".join(f"S{i}={sw_scores[i]:.4f}" for i in top5_ids)
    print(f"Top-5 scores: {top5_str}")
    print(f"CPU victims ({len(cpu_victims)}): {cpu_victims[:10]}{'...' if len(cpu_victims)>10 else ''}")

    # Save JSON
    inference_json = {
        "graph_id":     graph_id,
        "fault_type":   "network_congestion",
        "true_rc": {
            "node_type": "switch",
            "node_id":   rc_id,
            "label":     f"S{rc_id}",
        },
        "rc_rank":      result["rc_rank"],
        "rc_logit":     float(sw_logits[rc_id]),
        "rc_score":     float(sw_scores[rc_id]),
        "margin_over_rank2": float(sw_logits[rc_id]) - float(sw_logits[runner_up_id]),
        "all_switch_logits": [float(x) for x in sw_logits],
        "all_switch_scores": [float(x) for x in sw_scores],
        "top5": [
            {"node_id": i, "label": f"S{i}",
             "logit": float(sw_logits[i]),
             "score": float(sw_scores[i])}
            for i in top5_ids
        ],
        "cpu_victim_ids": cpu_victims,
        "gpu_victim_ids": gpu_victims,
    }
    json_path = os.path.join(OUTDIR, "inference_sample.json")
    with open(json_path, "w") as f:
        json.dump(inference_json, f, indent=2)
    print(f"Inference data saved: {json_path}")

    return inference_json, data


# ============================================================
# STEP 2 — Graph visualization (full datacenter topology)
# ============================================================

def _build_topology(data, rc_id, cpu_victims):
    """
    Extract spine-leaf topology from edge structure.
    Returns:
      switch_to_cpus: dict sw_id -> sorted list of cpu_ids (all 100 CPUs)
      spine_ids:      list of switch ids that are pure uplinks (no leaf CPUs)
      leaf_ids:       list of switch ids that connect to CPUs
      cpu_to_switch:  dict cpu_id -> sw_id
      cpu_to_jobs:    dict cpu_id -> list of job_ids
    """
    from collections import defaultdict

    # cpu->switch mapping
    et_cpu = ("cpu", "connected_to", "switch")
    ei_cpu = data[et_cpu].edge_index
    cpu_to_switch = {}
    for idx in range(ei_cpu.shape[1]):
        cpu_to_switch[int(ei_cpu[0, idx])] = int(ei_cpu[1, idx])

    switch_to_cpus = defaultdict(list)
    for cpu_i, sw_i in cpu_to_switch.items():
        switch_to_cpus[sw_i].append(cpu_i)

    # Classify switches: leaf = has CPUs, spine = no CPUs
    all_sw = list(range(data["switch"].x.shape[0]))
    leaf_ids  = sorted([s for s in all_sw if len(switch_to_cpus[s]) > 0])
    spine_ids = sorted([s for s in all_sw if len(switch_to_cpus[s]) == 0])

    # job->cpu mapping
    et_job = ("job", "executes_on", "cpu")
    ei_job = data[et_job].edge_index
    cpu_to_jobs = defaultdict(list)
    for idx in range(ei_job.shape[1]):
        cpu_to_jobs[int(ei_job[0, idx])].append(int(ei_job[1, idx]))
    # invert: cpu -> jobs
    job_cpu_map = defaultdict(list)
    for idx in range(ei_job.shape[1]):
        job_i = int(ei_job[0, idx])
        cpu_i = int(ei_job[1, idx])
        job_cpu_map[cpu_i].append(job_i)

    return (
        {k: sorted(v) for k, v in switch_to_cpus.items()},
        spine_ids,
        leaf_ids,
        cpu_to_switch,
        job_cpu_map,
    )


def make_graph_panel(ax, info, data):
    """
    Full datacenter topology layout:
      Row 0 (top):    spine switches
      Row 1:          leaf switches
      Row 2:          CPU nodes grouped by their leaf switch
      Row 3 (bottom): job nodes under affected CPUs only
    """
    rc_id        = info["true_rc"]["node_id"]
    runner_up_id = info["top5"][1]["node_id"]
    sw_scores    = info["all_switch_scores"]
    sw_logits    = info["all_switch_logits"]
    cpu_victims  = set(info["cpu_victim_ids"])

    (switch_to_cpus, spine_ids, leaf_ids,
     cpu_to_switch, job_cpu_map) = _build_topology(data, rc_id, cpu_victims)

    # ---- Decide which nodes to show ----
    # All 6 switches shown always.
    # CPUs: all under RC leaf, 4 representative per other leaf (sorted).
    # Jobs: 2 per victim CPU, capped at 14 total.
    CPU_SAMPLE_OTHER = 4
    JOBS_PER_CPU     = 2
    MAX_JOBS         = 14

    shown_cpus    = {}  # cpu_id -> leaf_sw_id
    for leaf in leaf_ids:
        cpus = switch_to_cpus[leaf]
        if leaf == rc_id:
            for c in cpus:
                shown_cpus[c] = leaf
        else:
            for c in cpus[:CPU_SAMPLE_OTHER]:
                shown_cpus[c] = leaf

    victim_cpus_shown = [c for c in sorted(shown_cpus) if c in cpu_victims]
    shown_jobs = []
    for cpu_i in victim_cpus_shown:
        jobs = job_cpu_map[cpu_i][:JOBS_PER_CPU]
        shown_jobs.extend(jobs)
        if len(shown_jobs) >= MAX_JOBS:
            break
    shown_jobs = shown_jobs[:MAX_JOBS]

    # ---- Compute fixed positions (no spring layout — explicit grid) ----
    # Canvas: x in [0,1], y in [0,1]
    # Row y positions
    Y_SPINE = 0.90
    Y_LEAF  = 0.68
    Y_CPU   = 0.38
    Y_JOB   = 0.10

    pos = {}  # key -> (x, y)

    # Spine switches: evenly spaced
    n_spine = len(spine_ids)
    for i, sw in enumerate(spine_ids):
        x = (i + 1) / (n_spine + 1)
        pos[f"sw_{sw}"] = (x, Y_SPINE)

    # Leaf switches: evenly spaced
    n_leaf = len(leaf_ids)
    leaf_x = {}  # sw_id -> x
    for i, sw in enumerate(leaf_ids):
        x = (i + 1) / (n_leaf + 1)
        pos[f"sw_{sw}"] = (x, Y_LEAF)
        leaf_x[sw] = x

    # CPU nodes: grouped under their leaf switch
    # For each leaf, spread CPUs horizontally around the leaf x
    leaf_cpu_lists = {}
    for leaf in leaf_ids:
        cpus_under = sorted([c for c, lf in shown_cpus.items() if lf == leaf])
        leaf_cpu_lists[leaf] = cpus_under

    cpu_width = 0.18   # total horizontal span per leaf column
    for leaf in leaf_ids:
        cpus = leaf_cpu_lists[leaf]
        cx   = leaf_x[leaf]
        n    = len(cpus)
        for j, cpu_i in enumerate(cpus):
            if n == 1:
                x = cx
            else:
                x = cx - cpu_width/2 + j * cpu_width / (n - 1)
            pos[f"cpu_{cpu_i}"] = (x, Y_CPU)

    # Job nodes: spread under their victim CPUs
    job_width = 0.10
    for k, job_i in enumerate(shown_jobs):
        # Find which cpu this job belongs to
        parent_cpu = None
        for cpu_i in victim_cpus_shown:
            if job_i in job_cpu_map[cpu_i]:
                parent_cpu = cpu_i
                break
        if parent_cpu is None:
            continue
        cpu_jobs = [j for j in job_cpu_map[parent_cpu] if j in shown_jobs]
        j_idx    = cpu_jobs.index(job_i)
        n_j      = len(cpu_jobs)
        cx       = pos[f"cpu_{parent_cpu}"][0]
        if n_j == 1:
            x = cx
        else:
            x = cx - job_width/2 + j_idx * job_width / (n_j - 1)
        pos[f"job_{job_i}"] = (x, Y_JOB)

    # ---- Node metadata ----
    node_meta = {}

    for sw in spine_ids + leaf_ids:
        score  = sw_scores[sw]
        logit  = sw_logits[sw]
        alpha  = 0.25 + score * 0.75
        size   = 350 + score * 1200
        is_rc  = (sw == rc_id)
        is_run = (sw == runner_up_id)
        is_leaf = sw in leaf_ids

        if is_rc:
            color = GREEN; alpha = 1.0
        elif is_run:
            color = GOLD
        elif is_leaf:
            color = GRAY
        else:
            color = "#2a2a4a"   # spine: very dark, not candidates

        # Spine label: "SPINE" prefix, leaf label: "SW{n}"
        if sw in spine_ids:
            label = f"SP{sw}"
        else:
            # Number leaf switches 0..3 for display clarity
            label = f"SW{leaf_ids.index(sw)}"
            if is_rc:
                label = f"SW{leaf_ids.index(sw)}"

        node_meta[f"sw_{sw}"] = {
            "color": color, "alpha": alpha, "size": size,
            "label": label, "score": score, "logit": logit,
            "is_rc": is_rc, "is_runner": is_run,
            "shape": "h",   # hexagon via matplotlib marker
        }

    for cpu_i, leaf in shown_cpus.items():
        is_victim = cpu_i in cpu_victims
        color = ORANGE if is_victim else "#2d2d50"
        alpha = 0.85 if is_victim else 0.45
        node_meta[f"cpu_{cpu_i}"] = {
            "color": color, "alpha": alpha, "size": 120,
            "label": f"C{cpu_i}", "score": 0.0, "logit": 0.0,
            "is_rc": False, "is_runner": False, "shape": "s",
        }

    LIGHT_ORANGE = "#ffb347"
    for job_i in shown_jobs:
        node_meta[f"job_{job_i}"] = {
            "color": LIGHT_ORANGE, "alpha": 0.7, "size": 70,
            "label": f"J{job_i}", "score": 0.0, "logit": 0.0,
            "is_rc": False, "is_runner": False, "shape": "o",
        }

    # ---- Draw background ----
    ax.set_facecolor(BG)
    ax.set_xlim(-0.05, 1.05)
    ax.set_ylim(-0.02, 1.02)
    ax.axis("off")

    # ---- Row labels (left margin) ----
    label_kw = dict(ha="right", va="center", fontsize=11, color="#7777aa",
                    style="italic", transform=ax.transAxes)
    ax.text(-0.01, Y_SPINE, "Spine", **label_kw)
    ax.text(-0.01, Y_LEAF,  "Leaf", **label_kw)
    ax.text(-0.01, Y_CPU,   "Servers", **label_kw)
    ax.text(-0.01, Y_JOB,   "Jobs", **label_kw)

    # ---- Draw edges ----
    # 1. Spine <-> leaf uplinks (thin gray lines)
    et_up = ("switch", "uplink_to", "switch")
    ei_up = data[et_up].edge_index
    drawn_uplinks = set()
    for idx in range(ei_up.shape[1]):
        s, t = int(ei_up[0, idx]), int(ei_up[1, idx])
        pair = (min(s,t), max(s,t))
        if pair in drawn_uplinks:
            continue
        drawn_uplinks.add(pair)
        if f"sw_{s}" not in pos or f"sw_{t}" not in pos:
            continue
        x0, y0 = pos[f"sw_{s}"]
        x1, y1 = pos[f"sw_{t}"]
        ax.plot([x0, x1], [y0, y1], color="#334455", linewidth=1.0,
                alpha=0.6, zorder=1)

    # 2. Leaf -> cpu edges (thin lines, colored by victim status)
    for cpu_i, leaf in shown_cpus.items():
        x0, y0 = pos[f"sw_{leaf}"]
        x1, y1 = pos[f"cpu_{cpu_i}"]
        is_victim = cpu_i in cpu_victims
        if is_victim:
            ax.plot([x0, x1], [y0, y1], color="#884400", linewidth=0.8,
                    alpha=0.5, zorder=1)
        else:
            ax.plot([x0, x1], [y0, y1], color="#2a2a40", linewidth=0.5,
                    alpha=0.4, zorder=1)

    # 3. Propagation arrows: RC switch -> victim CPUs (FancyArrow, curved, red)
    rc_xy = pos[f"sw_{rc_id}"]
    for cpu_i in victim_cpus_shown:
        if f"cpu_{cpu_i}" not in pos:
            continue
        cpu_xy = pos[f"cpu_{cpu_i}"]
        arrow = FancyArrowPatch(
            rc_xy, cpu_xy,
            connectionstyle="arc3,rad=0.0",
            arrowstyle="-|>",
            mutation_scale=10,
            color=RED_ARR,
            alpha=0.35,
            linewidth=1.2,
            zorder=3,
        )
        ax.add_patch(arrow)

    # 4. CPU -> job edges (very thin, light)
    for job_i in shown_jobs:
        if f"job_{job_i}" not in pos:
            continue
        for cpu_i in victim_cpus_shown:
            if job_i in job_cpu_map[cpu_i] and f"cpu_{cpu_i}" in pos:
                x0, y0 = pos[f"cpu_{cpu_i}"]
                x1, y1 = pos[f"job_{job_i}"]
                ax.plot([x0, x1], [y0, y1], color="#664422", linewidth=0.6,
                        alpha=0.4, zorder=1)
                break

    # ---- Draw nodes (Z order: background first, RC on top) ----
    MARKER_MAP = {"h": "h", "s": "s", "o": "o"}

    for color_order in ["#2a2a4a", "#2d2d50", GRAY, LIGHT_ORANGE, ORANGE, GOLD, GREEN]:
        for key, m in node_meta.items():
            if m["color"] != color_order:
                continue
            if key not in pos:
                continue
            x, y = pos[key]
            mk   = MARKER_MAP.get(m["shape"], "o")
            sz   = m["size"]

            if m["is_rc"]:
                # Glow ring
                ax.scatter(x, y, s=sz * 2.2, color=GREEN, alpha=0.18,
                           marker=mk, zorder=5)
                ax.scatter(x, y, s=sz * 1.5, color=GREEN, alpha=0.30,
                           marker=mk, zorder=5)

            ax.scatter(x, y, s=sz, c=m["color"], alpha=m["alpha"],
                       marker=mk,
                       edgecolors="white" if (m["is_rc"] or m["is_runner"]) else "none",
                       linewidths=2.0 if m["is_rc"] else (1.2 if m["is_runner"] else 0),
                       zorder=6)

    # ---- Node labels ----
    # Only label switches and a few CPUs (to avoid clutter)
    for key, m in node_meta.items():
        if key not in pos:
            continue
        x, y = pos[key]
        # Always label switches
        if key.startswith("sw_"):
            fsize = 10 if not m["is_rc"] else 11
            fw    = "bold" if (m["is_rc"] or m["is_runner"]) else "normal"
            col   = WHITE if (m["is_rc"] or m["is_runner"]) else "#aaaacc"
            ax.text(x, y, m["label"], ha="center", va="center",
                    fontsize=fsize, fontweight=fw, color=col, zorder=7)

    # ---- Annotation boxes: RC and runner-up ----
    rc_x, rc_y   = pos[f"sw_{rc_id}"]
    rc_score      = sw_scores[rc_id]
    rc_text       = f"ROOT CAUSE\nSwitch SW{leaf_ids.index(rc_id)}\nScore: {rc_score:.2f}\nRank: #1"
    ann_x = min(rc_x + 0.16, 0.92)
    ax.annotate(rc_text,
        xy=(rc_x, rc_y), xytext=(ann_x, rc_y + 0.09),
        fontsize=11, color=GREEN, fontweight="bold",
        bbox=dict(boxstyle="round,pad=0.4", fc="#0a1a0d", ec=GREEN,
                  alpha=0.93, lw=1.8),
        arrowprops=dict(arrowstyle="-", color=GREEN, lw=1.2),
        zorder=9, va="center",
    )

    ru_x, ru_y  = pos[f"sw_{runner_up_id}"]
    ru_score     = sw_scores[runner_up_id]
    ru_label     = f"SW{leaf_ids.index(runner_up_id)}" if runner_up_id in leaf_ids else f"SP{runner_up_id}"
    ru_text      = f"Runner-up\n{ru_label}\nScore: {ru_score:.3f}\nRank: #2"
    ann_rx = max(ru_x - 0.16, 0.05)
    ax.annotate(ru_text,
        xy=(ru_x, ru_y), xytext=(ann_rx, ru_y - 0.12),
        fontsize=10, color=GOLD,
        bbox=dict(boxstyle="round,pad=0.3", fc="#1a1500", ec=GOLD,
                  alpha=0.90, lw=1.5),
        arrowprops=dict(arrowstyle="-", color=GOLD, lw=1.0),
        zorder=9, va="center",
    )

    # ---- Legend ----
    legend_elements = [
        mpatches.Patch(color=GREEN,       label="AI-identified root cause"),
        mpatches.Patch(color=ORANGE,      label="Affected servers"),
        mpatches.Patch(color=LIGHT_ORANGE, label="Impacted jobs"),
        mpatches.Patch(color=GOLD,        label="Closest competitor"),
        mpatches.Patch(color=GRAY,        label="Healthy switches"),
        mpatches.Patch(color=RED_ARR,     label="Fault propagation"),
    ]
    legend = ax.legend(handles=legend_elements, loc="lower left",
                       fontsize=11, framealpha=0.75,
                       facecolor="#12121e", edgecolor="#444466",
                       labelcolor=WHITE, borderpad=0.8)

    # ---- Bottom info bar ----
    bottom_txt = (
        f"Real inference  |  graph_id {info['graph_id']}  |  "
        f"Fault: network_congestion  |  Root cause identified at Rank 1"
    )
    ax.text(0.5, -0.01, bottom_txt, ha="center", va="top",
            fontsize=10, color="#8888aa", transform=ax.transAxes)

    return pos


# ============================================================
# STEP 3 — Score bar chart panel
# ============================================================

def make_bar_panel(ax, info):
    sw_logits = np.array(info["all_switch_logits"])
    sw_scores = np.array(info["all_switch_scores"])
    rc_id     = info["true_rc"]["node_id"]
    n         = len(sw_logits)

    # Sort descending by logit
    order  = np.argsort(sw_logits)[::-1]
    labels = [f"S{i}" for i in order]
    logits = sw_logits[order]

    colors = []
    for i in order:
        if i == rc_id:
            colors.append(GREEN)
        elif i == info["top5"][1]["node_id"]:
            colors.append(GOLD)
        else:
            colors.append(GRAY)

    x = np.arange(n)
    bars = ax.bar(x, logits, color=colors, edgecolor="#222244", linewidth=0.8, width=0.6)

    # Labels on bars
    for bar, val, node_i in zip(bars, logits, order):
        ypos = val + 0.3 if val >= 0 else val - 0.8
        va   = "bottom" if val >= 0 else "top"
        tag  = " TRUE RC" if node_i == rc_id else ""
        ax.text(bar.get_x() + bar.get_width()/2, ypos,
                f"{val:.2f}{tag}", ha="center", va=va,
                fontsize=10, color=WHITE if node_i != rc_id else GREEN,
                fontweight="bold" if node_i == rc_id else "normal")

    # Zero line
    ax.axhline(0, color="#ff4444", linewidth=1.2, linestyle="--", alpha=0.7)
    ax.text(n - 0.5, 0.3, "Decision boundary", fontsize=10, color="#ff4444", ha="right")

    # Margin annotation
    rc_logit  = info["rc_logit"]
    ru_logit  = info["top5"][1]["logit"]
    margin    = info["margin_over_rank2"]
    rc_x      = list(order).index(rc_id)
    ru_x      = list(order).index(info["top5"][1]["node_id"])
    # Double-headed arrow between rank-1 and rank-2 bars (top of rank-2 to top of rank-1)
    mid_x     = (rc_x + ru_x) / 2
    ax.annotate(
        "", xy=(rc_x, rc_logit), xytext=(ru_x, ru_logit),
        arrowprops=dict(arrowstyle="<->", color=WHITE, lw=1.5),
        zorder=5,
    )
    ax.text(mid_x, (rc_logit + ru_logit) / 2 + 0.6,
            f"Margin: {margin:.2f}", ha="center", fontsize=11,
            color=WHITE, fontweight="bold",
            bbox=dict(fc="#0d1117", ec="#888899", boxstyle="round,pad=0.25", alpha=0.85))

    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=13, color=WHITE)
    ax.set_facecolor(BG)
    ax.tick_params(colors=WHITE)
    for spine in ax.spines.values():
        spine.set_edgecolor("#333355")
    ax.yaxis.label.set_color(WHITE)
    ax.set_ylabel("Confidence score (raw)", fontsize=13, color=WHITE)
    ax.set_title("Model confidence — all candidates\n(real inference, sorted by score)",
                 fontsize=14, fontweight="bold", color=WHITE, pad=8)
    ax.yaxis.grid(True, alpha=0.2, linestyle="--", color="#555577")
    ax.set_axisbelow(True)


# ============================================================
# STEP 2 standalone figure
# ============================================================

def save_graph_figure(info, data):
    fig, ax = plt.subplots(figsize=(12, 9))
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    make_graph_panel(ax, info, data)
    fig.suptitle("Root Cause Analysis — Network Graph View",
                 fontsize=17, fontweight="bold", color=WHITE, y=0.98)
    plt.tight_layout()
    out = os.path.join(OUTDIR, "real_inference_graph.png")
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    print(f"Saved: {out}")


# ============================================================
# STEP 3 standalone figure
# ============================================================

def save_bar_figure(info):
    fig, ax = plt.subplots(figsize=(9, 6))
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    make_bar_panel(ax, info)
    plt.tight_layout()
    out = os.path.join(OUTDIR, "real_inference_scores.png")
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    print(f"Saved: {out}")


# ============================================================
# STEP 4 — Combined figure (70/30 split)
# ============================================================

def save_combined_figure(info, data):
    fig = plt.figure(figsize=(18, 9), facecolor=BG)
    # 70% left for graph, 30% right for bar chart
    gs  = fig.add_gridspec(1, 2, width_ratios=[7, 3], wspace=0.04,
                           left=0.02, right=0.98, top=0.91, bottom=0.06)
    ax_graph = fig.add_subplot(gs[0])
    ax_bar   = fig.add_subplot(gs[1])

    ax_graph.set_facecolor(BG)
    ax_bar.set_facecolor(BG)

    make_graph_panel(ax_graph, info, data)
    make_bar_panel(ax_bar, info)

    fig.suptitle("Root Cause Analysis — Real Inference Result",
                 fontsize=18, fontweight="bold", color=WHITE, y=0.97)

    out = os.path.join(OUTDIR, "real_inference_combined.png")
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    print(f"Saved: {out}")


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    print("=== Running real inference ===")
    info, data = run_inference()

    print()
    print("=== Generating figures ===")
    save_graph_figure(info, data)
    save_bar_figure(info)
    save_combined_figure(info, data)

    print()
    print("=== REAL INFERENCE SUMMARY ===")
    print(f"Graph ID:              {info['graph_id']}")
    print(f"Fault type:            network_congestion")
    print(f"True RC node:          Switch S{info['true_rc']['node_id']}")
    print(f"Model predicted rank:  {info['rc_rank']} [CORRECT]")
    print(f"RC confidence score:   {info['rc_score']:.4f}")
    print(f"Margin over rank-2:    {info['margin_over_rank2']:.2f}")
    print(f"Figures saved to:      {OUTDIR}/")
