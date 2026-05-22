"""
15-second thesis defense animation — v3 (full cluster, large nodes).
3 fault scenarios: network_congestion -> hdd_degradation -> ram_leak
"""
import os, sys, json, math, io, subprocess
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyArrowPatch
from matplotlib.animation import FuncAnimation
from PIL import Image as PILImage

OUTDIR = "artifacts/visualizations"
os.makedirs(OUTDIR, exist_ok=True)

# ── colours ───────────────────────────────────────────────────────────────────
BG      = "#0d1117"
HEALTHY = "#3a3a5c"
RC_RED  = "#ff3333"
VICTIM  = "#ff8c00"
FOUND   = "#00cc44"
SCAN    = "#ffd700"
E_NORM  = "#2a2a4a"
E_FAULT = "#ff4444"
WHITE   = "#e8e8e8"
LTORANGE= "#ffb347"
SPINE_C = "#1e2a3a"   # spine switches — dim blue-gray

# ── load real inference data ──────────────────────────────────────────────────
with open("/tmp/anim_data.json") as f:
    REAL = json.load(f)

# ── animation params ──────────────────────────────────────────────────────────
FPS_MP4 = 30
FPS_GIF = 15
FRAMES  = 150          # per scenario  (5 sec × 30fps)
TOTAL   = FRAMES * 3   # 450 frames
FIGW, FIGH = 16, 9
DPI     = 120

# ── node sizes (markersize in points) ─────────────────────────────────────────
MS_SW_BASE  = 30    # switch hexagon
MS_CPU_BASE = 18    # cpu circle
MS_JOB_BASE = 10    # job small circle

# ── fixed layout ──────────────────────────────────────────────────────────────
# 6 switches: S0-S3 leaf, S4-S5 spine
# Leaves get CPUs; spines are in a slightly raised centre
SW_X  = [0.10, 0.27, 0.44, 0.61, 0.78, 0.93]   # evenly spaced across width
SW_Y  = 0.86

CPU_Y1 = 0.65    # CPU top row (3 nodes per switch)
CPU_Y2 = 0.50    # CPU bottom row (2-3 nodes per switch)
JOB_Y  = 0.24

# Hard-coded CPU assignment (from data inspection):
#   SW0(leaf): cpus [0,13,14,17,18]
#   SW1(leaf): cpus [1,2,3,7,15]
#   SW2(leaf): cpus [78,55,4,5,6,19]  ← includes hdd(55)+ram(78) victims
#   SW3(leaf): cpus [8,9,10,11,12]    ← congestion RC's CPUs
#   SW4(spine): no cpus
#   SW5(spine): no cpus
LEAF_CPU_PLAN = {
    0: [0,  13, 14, 17, 18],
    1: [1,   2,  3,  7, 15],
    2: [78, 55,  4,  5,  6, 19],   # 6 CPUs to fit forced victims
    3: [8,   9, 10, 11, 12],
}
# Jobs shown (under SW3 victim CPUs for congestion scenario)
# cpu8→[12,26], cpu9→[21,120], cpu10→[1,30], cpu11→[88,198], cpu12→[13,66]
JOB_PLAN = [(12,8),(26,8),(21,9),(120,9),(1,10),(30,10),(88,11),(198,11),(13,12),(66,12)]
# Jobs for hdd scenario: cpu55→[376]
HDD_JOBS = [(376, 55)]
# Jobs for ram scenario: cpu78→[123,255,275]
RAM_JOBS = [(123,78),(255,78),(275,78)]

def build_positions():
    pos = {}
    CPU_SPAN = 0.15   # horizontal spread per switch's CPUs

    # Switch positions
    for sw_i in range(6):
        pos[f"sw_{sw_i}"] = (SW_X[sw_i], SW_Y)

    # CPU positions for each leaf switch
    for sw_i, cpus in LEAF_CPU_PLAN.items():
        cx  = SW_X[sw_i]
        top = cpus[:3]
        bot = cpus[3:]
        span = CPU_SPAN

        def spread(nodes, y, span):
            n = len(nodes)
            for j, c in enumerate(nodes):
                if n == 1:
                    x = cx
                else:
                    x = cx - span/2 + j * span/(n-1)
                pos[f"cpu_{c}"] = (x, y)
        spread(top, CPU_Y1, span)
        spread(bot, CPU_Y2, span * 0.7)

    # Job positions (switch scenario, under SW3 CPUs)
    for job_i, parent_cpu in JOB_PLAN:
        if f"cpu_{parent_cpu}" not in pos:
            continue
        px = pos[f"cpu_{parent_cpu}"][0]
        siblings = [j for j,c in JOB_PLAN if c == parent_cpu]
        n  = len(siblings)
        ji = siblings.index(job_i)
        x  = px + (ji - (n-1)/2) * 0.04
        pos[f"job_{job_i}"] = (x, JOB_Y)

    # Job positions for hdd scenario
    for job_i, parent_cpu in HDD_JOBS:
        if f"cpu_{parent_cpu}" in pos:
            px = pos[f"cpu_{parent_cpu}"][0]
            pos[f"job_{job_i}"] = (px, JOB_Y)

    # Job positions for ram scenario
    for job_i, parent_cpu in RAM_JOBS:
        if f"cpu_{parent_cpu}" in pos:
            px = pos[f"cpu_{parent_cpu}"][0]
            siblings = [j for j,c in RAM_JOBS if c == parent_cpu]
            n  = len(siblings)
            ji = siblings.index(job_i) if job_i in siblings else 0
            x  = px + (ji - (n-1)/2) * 0.04
            pos[f"job_{job_i}"] = (x, JOB_Y)

    return pos

POS = build_positions()

# ── helpers ───────────────────────────────────────────────────────────────────
def pulse(f_rel, period=20, base=1.0, amp=0.35):
    return base + amp * math.sin(2 * math.pi * f_rel / period)

def sinramp(f_rel, start, end, lo=0.0, hi=1.0):
    """Smooth 0→1 ramp using sin, clamped."""
    if f_rel <= start: return lo
    if f_rel >= end:   return hi
    t = (f_rel - start) / (end - start)
    return lo + (hi - lo) * (0.5 - 0.5 * math.cos(math.pi * t))

# ── figure setup ──────────────────────────────────────────────────────────────
fig, ax = plt.subplots(figsize=(FIGW, FIGH))
fig.patch.set_facecolor(BG)
ax.set_facecolor(BG)
ax.set_xlim(-0.02, 1.02)
ax.set_ylim(0.08, 1.00)
ax.axis("off")

# ── static topology edges (drawn once, never change) ─────────────────────────
def draw_static_edges():
    # Spine-to-leaf uplinks (both S4 and S5 connect to all leaves)
    for sp in [4, 5]:
        for lf in LEAF_CPU_PLAN:
            x0, y0 = POS[f"sw_{sp}"]
            x1, y1 = POS[f"sw_{lf}"]
            ax.plot([x0, x1], [y0, y1],
                    color="#1a2a3a", lw=1.0, alpha=0.6, zorder=1, solid_capstyle="round")

    # Leaf-to-cpu edges
    for sw_i, cpus in LEAF_CPU_PLAN.items():
        x0, y0 = POS[f"sw_{sw_i}"]
        for c in cpus:
            key = f"cpu_{c}"
            if key in POS:
                x1, y1 = POS[key]
                ax.plot([x0, x1], [y0, y1],
                        color=E_NORM, lw=0.6, alpha=0.55, zorder=1)

    # cpu-to-job edges (switch scenario, all pairs)
    for job_i, parent_cpu in JOB_PLAN:
        jk = f"job_{job_i}"; ck = f"cpu_{parent_cpu}"
        if jk in POS and ck in POS:
            x0,y0 = POS[ck]; x1,y1 = POS[jk]
            ax.plot([x0, x1], [y0, y1], color=E_NORM, lw=0.4, alpha=0.3, zorder=1)

draw_static_edges()

# ── row labels ────────────────────────────────────────────────────────────────
row_kw = dict(ha="right", va="center", fontsize=11, color="#4a4a6a",
              style="italic", transform=ax.transAxes)
ax.text(0.005, (SW_Y  - 0.08)/0.92, "Switches", **row_kw)
ax.text(0.005, (CPU_Y1- 0.08)/0.92, "Servers",  **row_kw)
ax.text(0.005, (JOB_Y - 0.08)/0.92, "Jobs",     **row_kw)

# ── persistent legend ─────────────────────────────────────────────────────────
legend_elems = [
    mpatches.Patch(color=FOUND,    label="AI-identified root cause"),
    mpatches.Patch(color=VICTIM,   label="Affected systems"),
    mpatches.Patch(color=RC_RED,   label="Fault origin"),
    mpatches.Patch(color=E_FAULT,  label="Fault propagation"),
    mpatches.Patch(color=HEALTHY,  label="Healthy systems"),
]
ax.legend(handles=legend_elems, loc="lower left", fontsize=11,
          framealpha=0.80, facecolor="#0a0a18", edgecolor="#2a2a4a",
          labelcolor=WHITE, borderpad=0.8, handlelength=1.4)

# ── node handles (ax.plot Line2D) ─────────────────────────────────────────────
node_handles = {}   # key -> Line2D

for key, (x, y) in sorted(POS.items()):
    if key.startswith("sw_"):
        sw_i = int(key.split("_")[1])
        is_spine = sw_i in (4, 5)
        mk = "h"
        ms = MS_SW_BASE
        c  = SPINE_C if is_spine else HEALTHY
    elif key.startswith("cpu_"):
        mk = "o"
        ms = MS_CPU_BASE
        c  = HEALTHY
    else:
        mk = "o"
        ms = MS_JOB_BASE
        c  = HEALTHY

    h, = ax.plot(x, y,
                 marker=mk, markersize=ms,
                 color=c,
                 markeredgecolor="none", markeredgewidth=0,
                 linestyle="none", zorder=6)
    node_handles[key] = h

# ── static switch labels (always visible) ─────────────────────────────────────
switch_label_texts = {}
for sw_i in range(6):
    x, y = POS[f"sw_{sw_i}"]
    t = ax.text(x, y, f"S{sw_i}",
                ha="center", va="center", fontsize=10, fontweight="bold",
                color=WHITE, zorder=8)
    switch_label_texts[sw_i] = t

# ── dynamic elements (cleared and redrawn every frame) ────────────────────────
dyn_arrows = []     # FancyArrowPatch objects
dyn_texts  = []     # annotation Text objects

# ── title, status, metrics text ───────────────────────────────────────────────
title_txt = ax.text(0.5, 0.972, "", ha="center", va="top",
                    fontsize=18, fontweight="bold", color=WHITE,
                    transform=ax.transAxes, zorder=10)
status_txt = ax.text(0.5, 0.05, "", ha="center", va="bottom",
                     fontsize=14, color="#aaaacc", transform=ax.transAxes, zorder=10,
                     bbox=dict(fc=BG, ec="#2a2a4a", boxstyle="round,pad=0.45", alpha=0.88))
metrics_txt = ax.text(0.5, 0.015, "", ha="center", va="bottom",
                      fontsize=11, color="#5a5a8a", transform=ax.transAxes, zorder=10)

# ── scenario state functions ──────────────────────────────────────────────────
# Each returns:
#   node_states:  {key: {'color', 'ms', 'alpha', 'edge_c', 'edge_w'}}
#   arrows:       [(src_key, dst_key, alpha, lw, rad)]
#   title:        str
#   status:       str
#   status_col:   str
#   annots:       [(key, text, color)]   max 2

def _default_states():
    states = {}
    for key in POS:
        sw_i_val = int(key.split("_")[1]) if "_" in key else -1
        is_spine = key.startswith("sw_") and sw_i_val in (4,5)
        states[key] = {
            "color":  SPINE_C if is_spine else HEALTHY,
            "ms":     MS_SW_BASE if key.startswith("sw_") else (MS_CPU_BASE if key.startswith("cpu_") else MS_JOB_BASE),
            "alpha":  0.55 if is_spine else 0.5,
            "edge_c": "none",
            "edge_w": 0,
        }
    return states

def state_congestion(f):
    """network_congestion: RC=SW3, victim CPUs=all 5 CPUs under SW3."""
    rc_sw = REAL["switch"]["rc_id"]          # 3
    rc_key = f"sw_{rc_sw}"
    victim_cpu_keys = [f"cpu_{c}" for c in [8, 9, 10, 11, 12] if f"cpu_{c}" in POS]
    job_keys = [f"job_{j}" for j, _ in JOB_PLAN if f"job_{j}" in POS]

    ns = _default_states()
    arrows = []
    title  = "Scenario 1: Network Congestion"
    status = "All systems operational"
    scol   = "#8888aa"
    annots = []

    if f < 30:
        # Phase 1: healthy
        pass

    elif f < 60:
        # Phase 2: RC switch pulses red (1 sec)
        fi = f - 30
        p  = pulse(fi, period=18, base=1.0, amp=0.40)
        ns[rc_key]["color"]  = RC_RED
        ns[rc_key]["alpha"]  = 0.9
        ns[rc_key]["ms"]     = MS_SW_BASE * p
        ns[rc_key]["edge_c"] = "#ff9999"
        ns[rc_key]["edge_w"] = 2.5
        status = "Warning  Anomaly detected: Switch S3"
        scol   = "#ff8888"
        annots.append((rc_key,
                       "S3 — Port saturation\nHigh traffic detected", RC_RED))

    elif f < 110:
        # Phase 3: gradual propagation
        fi = f - 60
        # RC stays red (gentle pulse)
        p_slow = pulse(fi, period=25, base=1.0, amp=0.2)
        ns[rc_key]["color"]  = RC_RED
        ns[rc_key]["alpha"]  = 0.95
        ns[rc_key]["ms"]     = MS_SW_BASE * p_slow
        ns[rc_key]["edge_c"] = "#ff6666"
        ns[rc_key]["edge_w"] = 2.0

        status = "Warning  Cascade spreading to compute nodes..."
        scol   = "#ffbb44"

        # Reveal victim CPUs gradually
        n_show = 0
        if fi >= 5:  n_show = 2
        if fi >= 15: n_show = 4
        if fi >= 28: n_show = len(victim_cpu_keys)

        for cpu_k in victim_cpu_keys[:n_show]:
            reveal = sinramp(fi, max(0, (victim_cpu_keys.index(cpu_k) * 8) - 5),
                             (victim_cpu_keys.index(cpu_k) * 8) + 12)
            ns[cpu_k]["color"]  = VICTIM
            ns[cpu_k]["alpha"]  = 0.5 + reveal * 0.45
            ns[cpu_k]["ms"]     = MS_CPU_BASE * (0.8 + reveal * 0.4)
            ns[cpu_k]["edge_c"] = "#ffcc66"
            ns[cpu_k]["edge_w"] = 1.5 * reveal
            arrows.append((rc_key, cpu_k,
                           0.25 + reveal * 0.5,
                           2.0 + reveal * 2.0, 0.20))

        # Jobs appear after most CPUs lit
        if fi >= 40:
            for jk in job_keys:
                t_job = sinramp(fi, 40, 55)
                ns[jk]["color"] = LTORANGE
                ns[jk]["alpha"] = 0.3 + t_job * 0.55
                ns[jk]["ms"]    = MS_JOB_BASE * (0.7 + t_job * 0.5)

        if n_show > 0:
            annots.append((rc_key,
                           f"{len(victim_cpu_keys)} compute nodes\naffected", "#ff9944"))

    else:
        # Phase 4: gold scan → identification
        fi   = f - 110
        dur  = 40   # frames for scan
        x_sc = sinramp(fi, 0, dur, 0.0, 1.2)

        # Apply gold tint as scan passes
        for key, (px, py) in POS.items():
            dist = abs(px - x_sc)
            if dist < 0.12:
                t = max(0, 1 - dist / 0.12)
                # Mix current color with SCAN
                ns[key]["color"] = SCAN
                ns[key]["alpha"] = max(ns[key]["alpha"], 0.4 + t * 0.5)
                ns[key]["ms"]    = ns[key]["ms"] * (1.0 + t * 0.15)

        # Victims stay orange under scan
        for cpu_k in victim_cpu_keys:
            ns[cpu_k]["color"] = VICTIM
            ns[cpu_k]["alpha"] = 0.9
            ns[cpu_k]["ms"]    = MS_CPU_BASE * 1.2
            arrows.append((rc_key, cpu_k, 0.55, 3.0, 0.20))
        for jk in job_keys:
            ns[jk]["color"] = LTORANGE
            ns[jk]["alpha"] = 0.75

        if fi >= 22:
            # Identified
            sc_val = REAL["switch"]["rc_score"]
            ns[rc_key]["color"]  = FOUND
            ns[rc_key]["alpha"]  = 1.0
            ns[rc_key]["ms"]     = MS_SW_BASE * 1.5
            ns[rc_key]["edge_c"] = WHITE
            ns[rc_key]["edge_w"] = 3.0
            annots.append((rc_key,
                           f"ROOT CAUSE: Switch S3\nConfidence: {sc_val*100:.1f}%",
                           FOUND))
            status = "Root cause identified"
            scol   = "#44ff88"
        else:
            ns[rc_key]["color"]  = RC_RED
            ns[rc_key]["alpha"]  = 0.95
            status = "Scanning cluster..."
            scol   = "#ddcc44"

    return ns, arrows, title, status, scol, annots


def state_hdd(f):
    """hdd_degradation: RC=HDD 55 attached to CPU 55 on SW2."""
    host_key = "cpu_55"
    rc_id    = REAL["hdd"]["rc_id"]   # 55
    score    = REAL["hdd"]["rc_score"]
    job_key  = "job_376"

    ns = _default_states()
    arrows = []
    title  = "Scenario 2: HDD Degradation"
    status = "All systems operational"
    scol   = "#8888aa"
    annots = []

    if f < 30:
        pass

    elif f < 60:
        fi = f - 30
        p  = pulse(fi, period=18, base=1.0, amp=0.40)
        if host_key in ns:
            ns[host_key]["color"]  = RC_RED
            ns[host_key]["alpha"]  = 0.9
            ns[host_key]["ms"]     = MS_CPU_BASE * p
            ns[host_key]["edge_c"] = "#ff9999"
            ns[host_key]["edge_w"] = 2.5
        status = "Warning  Storage anomaly: HDD on node C55"
        scol   = "#ff8888"
        annots.append((host_key, f"C55 — HDD failure\nI/O latency spike", RC_RED))

    elif f < 110:
        fi = f - 60
        p_slow = pulse(fi, period=25, base=1.0, amp=0.18)
        if host_key in ns:
            ns[host_key]["color"]  = RC_RED
            ns[host_key]["alpha"]  = 0.9
            ns[host_key]["ms"]     = MS_CPU_BASE * p_slow
            ns[host_key]["edge_c"] = "#ff6666"
            ns[host_key]["edge_w"] = 2.0
        status = "Warning  I/O bottleneck spreading to jobs"
        scol   = "#ffbb44"

        # Job appears at fi >= 12
        if fi >= 12 and job_key in ns:
            t_j = sinramp(fi, 12, 28)
            ns[job_key]["color"]  = VICTIM
            ns[job_key]["alpha"]  = 0.3 + t_j * 0.65
            ns[job_key]["ms"]     = MS_JOB_BASE * (0.7 + t_j * 0.6)
            ns[job_key]["edge_c"] = "#ffcc66"
            ns[job_key]["edge_w"] = 1.0 * t_j
            if host_key in POS and job_key in POS:
                arrows.append((host_key, job_key, 0.3 + t_j * 0.5, 2.0 + t_j * 2.5, 0.3))

        if host_key in ns:
            annots.append((host_key, "C55 — Jobs waiting\nfor I/O completion", "#ff9944"))

    else:
        fi = f - 110
        x_sc = sinramp(fi, 0, 40, 0.0, 1.2)
        for key, (px, py) in POS.items():
            dist = abs(px - x_sc)
            if dist < 0.12:
                t = max(0, 1 - dist/0.12)
                ns[key]["color"] = SCAN
                ns[key]["alpha"] = max(ns[key]["alpha"], 0.4 + t*0.5)
        if host_key in ns:
            ns[host_key]["color"] = VICTIM
            ns[host_key]["alpha"] = 0.9
        if job_key in ns:
            ns[job_key]["color"] = VICTIM
            ns[job_key]["alpha"] = 0.8
            arrows.append((host_key, job_key, 0.6, 3.5, 0.3))

        if fi >= 22:
            if host_key in ns:
                ns[host_key]["color"]  = FOUND
                ns[host_key]["alpha"]  = 1.0
                ns[host_key]["ms"]     = MS_CPU_BASE * 1.6
                ns[host_key]["edge_c"] = WHITE
                ns[host_key]["edge_w"] = 3.0
            annots.append((host_key,
                           f"ROOT CAUSE: HDD on C55\nConfidence: {score*100:.1f}%",
                           FOUND))
            status = "Root cause identified"
            scol   = "#44ff88"
        else:
            status = "Scanning cluster..."
            scol   = "#ddcc44"

    return ns, arrows, title, status, scol, annots


def state_ram(f):
    """ram_leak: RC=RAM 78 attached to CPU 78 on SW2."""
    host_key = "cpu_78"
    rc_id    = REAL["ram"]["rc_id"]   # 78
    score    = REAL["ram"]["rc_score"]
    job_keys = [f"job_{j}" for j,_ in RAM_JOBS if f"job_{j}" in POS]

    ns = _default_states()
    arrows = []
    title  = "Scenario 3: RAM Leak"
    status = "All systems operational"
    scol   = "#8888aa"
    annots = []

    if f < 30:
        pass

    elif f < 60:
        fi = f - 30
        # RAM leak is slow and gradual — smaller pulse amplitude
        p  = pulse(fi, period=28, base=1.0, amp=0.22)
        if host_key in ns:
            ns[host_key]["color"]  = RC_RED
            ns[host_key]["alpha"]  = 0.85
            ns[host_key]["ms"]     = MS_CPU_BASE * p
            ns[host_key]["edge_c"] = "#ff9999"
            ns[host_key]["edge_w"] = 2.0
        status = "Warning  Memory pressure rising: node C78"
        scol   = "#ff8888"
        annots.append((host_key, "C78 — RAM leak\nMemory pressure rising", RC_RED))

    elif f < 110:
        fi = f - 60
        p_slow = pulse(fi, period=30, base=1.0, amp=0.15)
        if host_key in ns:
            ns[host_key]["color"]  = RC_RED
            ns[host_key]["alpha"]  = 0.88
            ns[host_key]["ms"]     = MS_CPU_BASE * p_slow
        status = "Warning  Memory pressure cascading to jobs"
        scol   = "#ffbb44"

        # Phase-based job propagation (ram leak is sequential)
        phases = [(10, "C78 — Cache eviction\nbegins"),
                  (25, "C78 — CPU reclaim\noverhead"),
                  (40, "C78 — Jobs degrading")]
        active_annot = None
        for fi_thr, msg in phases:
            if fi >= fi_thr:
                active_annot = (host_key, msg, "#ff9944")
        if active_annot:
            annots.append(active_annot)

        for idx, jk in enumerate(job_keys):
            thr = 20 + idx * 12
            if fi >= thr:
                t_j = sinramp(fi, thr, thr + 15)
                ns[jk]["color"]  = VICTIM
                ns[jk]["alpha"]  = 0.3 + t_j * 0.60
                ns[jk]["ms"]     = MS_JOB_BASE * (0.7 + t_j * 0.55)
                if host_key in POS:
                    arrows.append((host_key, jk, 0.25 + t_j * 0.45,
                                   1.5 + t_j * 2.5, 0.25))

    else:
        fi = f - 110
        x_sc = sinramp(fi, 0, 40, 0.0, 1.2)
        for key, (px, py) in POS.items():
            dist = abs(px - x_sc)
            if dist < 0.12:
                t = max(0, 1 - dist/0.12)
                ns[key]["color"] = SCAN
                ns[key]["alpha"] = max(ns[key]["alpha"], 0.4 + t*0.5)
        if host_key in ns:
            ns[host_key]["color"] = VICTIM
            ns[host_key]["alpha"] = 0.9
        for jk in job_keys:
            if jk in ns:
                ns[jk]["color"] = VICTIM
                ns[jk]["alpha"] = 0.75
                arrows.append((host_key, jk, 0.55, 3.0, 0.25))

        if fi >= 22:
            if host_key in ns:
                ns[host_key]["color"]  = FOUND
                ns[host_key]["alpha"]  = 1.0
                ns[host_key]["ms"]     = MS_CPU_BASE * 1.6
                ns[host_key]["edge_c"] = WHITE
                ns[host_key]["edge_w"] = 3.0
            annots.append((host_key,
                           f"ROOT CAUSE: RAM on C78\nConfidence: {score*100:.1f}%",
                           FOUND))
            status = "Root cause identified"
            scol   = "#44ff88"
        else:
            status = "Scanning cluster..."
            scol   = "#ddcc44"

    return ns, arrows, title, status, scol, annots


def blend_states(s0, s1, t):
    """Linearly interpolate two node_state dicts."""
    merged = {}
    all_keys = set(s0) | set(s1)
    for k in all_keys:
        a = s0.get(k, _default_states().get(k, {}))
        b = s1.get(k, _default_states().get(k, {}))
        merged[k] = {
            "color":  b["color"] if t > 0.5 else a["color"],
            "ms":     a["ms"] * (1-t) + b["ms"] * t,
            "alpha":  a["alpha"] * (1-t) + b["alpha"] * t,
            "edge_c": b["edge_c"] if t > 0.5 else a["edge_c"],
            "edge_w": a["edge_w"] * (1-t) + b["edge_w"] * t,
        }
    return merged


def get_frame_state(frame):
    sc = frame // FRAMES
    f  = frame %  FRAMES
    FADE = 22

    if sc == 0:
        ns, ar, ti, st, sc_, an = state_congestion(f)
    elif sc == 1:
        if f < FADE:
            ns0, *_ = state_congestion(FRAMES - 1)
            ns1, ar, ti, st, sc_, an = state_hdd(0)
            t  = f / FADE
            ns = blend_states(ns0, ns1, t)
            ar = []
        else:
            ns, ar, ti, st, sc_, an = state_hdd(f)
    else:
        if f < FADE:
            ns0, *_ = state_hdd(FRAMES - 1)
            ns1, ar, ti, st, sc_, an = state_ram(0)
            t  = f / FADE
            ns = blend_states(ns0, ns1, t)
            ar = []
        else:
            ns, ar, ti, st, sc_, an = state_ram(f)

    return ns, ar, ti, st, sc_, an


# ── update function ───────────────────────────────────────────────────────────
def update(frame):
    global dyn_arrows, dyn_texts

    # Remove previous dynamic elements
    for a in dyn_arrows: a.remove()
    dyn_arrows = []
    for tx in dyn_texts: tx.remove()
    dyn_texts = []

    ns, arrows, title, status, scol, annots = get_frame_state(frame)

    # Update node handles
    for key, h in node_handles.items():
        st = ns.get(key, {})
        if not st:
            continue
        h.set_color(st["color"])
        h.set_markersize(max(1, st["ms"]))
        h.set_alpha(min(1.0, max(0.05, st["alpha"])))
        h.set_markeredgecolor(st["edge_c"])
        h.set_markeredgewidth(st["edge_w"])

    # Draw propagation arrows
    for (src_k, dst_k, alpha, lw, rad) in arrows:
        if src_k not in POS or dst_k not in POS:
            continue
        x0, y0 = POS[src_k]
        x1, y1 = POS[dst_k]
        ar = FancyArrowPatch(
            (x0, y0), (x1, y1),
            connectionstyle=f"arc3,rad={rad}",
            arrowstyle="->", mutation_scale=18,
            color=E_FAULT,
            alpha=min(1.0, max(0, alpha)),
            linewidth=min(5.0, lw),
            zorder=4,
        )
        ax.add_patch(ar)
        dyn_arrows.append(ar)

    # Annotation boxes (max 2)
    placed = 0
    for (key, text, color) in annots:
        if placed >= 2 or key is None or key not in POS:
            break
        x, y = POS[key]
        off_x = 0.14 if x < 0.52 else -0.14
        off_y = 0.10 if y > 0.35 else -0.10
        tx = ax.annotate(
            text, xy=(x, y),
            xytext=(x + off_x, y + off_y),
            fontsize=11, color=color, fontweight="bold",
            bbox=dict(boxstyle="round,pad=0.4", fc="#08080f",
                      ec=color, alpha=0.92, lw=1.8),
            arrowprops=dict(arrowstyle="-", color=color, lw=0.9),
            zorder=9, va="center",
        )
        dyn_texts.append(tx)
        placed += 1

    # Titles
    title_txt.set_text(title)
    status_txt.set_text(status)
    status_txt.set_color(scol)

    # Final frames: show summary metrics
    if frame >= TOTAL - FPS_MP4:
        metrics_txt.set_text(
            "Hit@1: 78%   |   Hit@3: 93%   |   MRR: 0.82   |   AUC: 0.983"
            "   |   GATv2 RCA System — JINR Govorun Cluster"
        )
    else:
        metrics_txt.set_text("")

    return [h for h in node_handles.values()] + dyn_arrows + dyn_texts


# ── render all frames to buffer ───────────────────────────────────────────────
print(f"Rendering {TOTAL} frames at DPI={DPI} ({int(FIGW*DPI)}x{int(FIGH*DPI)}px)...")
rendered = []
for fi in range(TOTAL):
    update(fi)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI, facecolor=BG, bbox_inches=None)
    buf.seek(0)
    rendered.append(np.array(PILImage.open(buf).convert("RGB")))
    if fi % 45 == 0:
        print(f"  {fi}/{TOTAL}...", end="\r")
print(f"  {TOTAL}/{TOTAL} done.   ")
plt.close(fig)

# ── save MP4 ─────────────────────────────────────────────────────────────────
mp4_path = os.path.join(OUTDIR, "cluster_animation.mp4")
try:
    import imageio, imageio_ffmpeg
    print("Saving MP4...")
    w = imageio.get_writer(mp4_path, fps=FPS_MP4, quality=8,
                           ffmpeg_log_level="quiet",
                           ffmpeg_params=["-vf","scale=trunc(iw/2)*2:trunc(ih/2)*2"])
    for img in rendered:
        w.append_data(img)
    w.close()
    print(f"Animation saved: cluster_animation.mp4 ({os.path.getsize(mp4_path)/1e6:.1f} MB)")
except Exception as e:
    print(f"WARNING: MP4 failed ({e})")
    imageio_ffmpeg = None

# ── save GIF via ffmpeg (mp4 → gif, two-pass palette) ────────────────────────
gif_path = os.path.join(OUTDIR, "cluster_animation.gif")
try:
    import imageio_ffmpeg as iff
    ffmpeg = iff.get_ffmpeg_exe()
    palette = "/tmp/anim_palette.png"
    r1 = subprocess.run([ffmpeg, "-y", "-i", mp4_path,
        "-vf", "fps=15,scale=960:540:flags=lanczos,"
               "palettegen=max_colors=128:stats_mode=diff",
        palette], capture_output=True)
    r2 = subprocess.run([ffmpeg, "-y", "-i", mp4_path, "-i", palette,
        "-filter_complex",
        "fps=15,scale=960:540:flags=lanczos[x];[x][1:v]"
        "paletteuse=dither=bayer:bayer_scale=5",
        "-loop", "0", gif_path], capture_output=True)
    from PIL import Image as _PI
    _g = _PI.open(gif_path)
    dur = _g.info.get("duration", 0)
    print(f"Animation saved: cluster_animation.gif "
          f"({os.path.getsize(gif_path)/1e6:.1f} MB, "
          f"{_g.n_frames} frames, {_g.n_frames*dur/1000:.1f}s)")
except Exception as e:
    print(f"WARNING: GIF failed ({e})")

print(f"\nDuration: ~15 sec")
print(f"Scenarios: network_congestion -> hdd_degradation -> ram_leak")
