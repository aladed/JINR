# Quick Start: GNN RCA System

## 30-Second Overview

**Problem**: HPC cluster failures create cascading issues → hard to find root cause.

**Solution**: 4-layer pipeline that finds the root cause and generates remediation actions.

```
GNN (78.3% accuracy) 
  ↓
RAG (semantic search) 
  ↓
LLM (Mistral 7B) 
  ↓
Firewall (semantic validation)
  ↓
Remediation Report (JSON + human-readable)
```

**Performance**: 6.4 seconds end-to-end (LLM dominates).

---

## Run the System

### Option 1: Full Integration Test
```bash
cd JINR-rag
pytest tests/test_full_system_integration.py -v

# Expected: 
# [GNN] Loaded: epoch=14, params=311,878
# [RAG] SOPs retrieved: 5
# [LLM] Backend: mistral, generation=6412ms
# [Firewall] Status: PASSED
# INTEGRATION TEST PASSED
```

### Option 2: CLI
```bash
cd JINR-rag
python -m remediation.run

# Reads: artifacts/inference_sample.json
# Writes: artifacts/remediation_report.json
# Prints: Human-readable summary
```

### Option 3: Programmatic
```python
from remediation.pipeline import run_pipeline
import json

inference = json.load(open("artifacts/inference_sample.json"))
playbook, metadata = run_pipeline(inference)

print(f"Actions: {len(playbook.actions)}")
print(f"Firewall: {metadata['firewall_status']}")
print(f"TTR: {metadata['ttr_breakdown']['total_ms']} ms")
```

---

## System Architecture

### Layer 0: Inference (GNN)
- **Module**: `training_pipeline/`
- **Model**: GATv2Hetero (311,878 params)
- **Input**: HPC incident (heterogeneous graph)
- **Output**: Root cause prediction + confidence
- **Speed**: 14 ms
- **Accuracy**: Hit@1=78.3%, Hit@3=82.6%

### Layer 1: Knowledge (RAG)
- **Module**: `rag/`
- **Backend**: Qdrant (vector DB) + Redis (cache)
- **Input**: Root cause + incident context
- **Output**: Top-5 relevant SOPs
- **Speed**: <2 ms

### Layer 2: Reasoning (LLM)
- **Module**: `llm/`
- **Model**: Mistral 7B (via Ollama)
- **Input**: RC + SOPs + context
- **Output**: Remediation actions (JSON)
- **Speed**: 6.4 sec
- **Actions**: CHECK_METRICS, APPLY_QOS, ISOLATE_NODE, MIGRATE_JOB, etc.

### Layer 3: Validation (Firewall)
- **Module**: `remediation/firewall.py`
- **Purpose**: Block dangerous actions
- **LLM constraint**: No direct terminal access
- **Speed**: <1 ms
- **Result**: PASSED or BLOCKED

### Layer 4: Aggregation (Output)
- **Module**: `remediation/pipeline.py` + `remediation/run.py`
- **Purpose**: Combine all data into final report
- **Output**: JSON + human-readable terminal output
- **Speed**: <10 ms

---

## Documentation

### For Understanding the System
1. **LAYERS.md** — Detailed layer diagrams & data flow
2. **ARCHITECTURE_CLEAN.md** — System overview
3. **REFACTORING_SUMMARY.md** — Changes made + rationale

### For Each Layer
1. **training_pipeline/README.md** — Layer 0 (GNN)
2. **rag/README.md** — Layer 1 (RAG)
3. **llm/README.md** — Layer 2 (LLM)
4. **remediation/README.md** — Layers 3-4 (Firewall + Output)

---

## Key Metrics

| Metric | Value |
|--------|-------|
| **RCA Hit@1** | 78.3% |
| **Hit@3** | 82.6% |
| **MRR** | 0.823 |
| **F1-score** | 0.716 |
| **Model params** | 311,878 |
| **TTR** | 6.4 seconds |
| **Test suite** | 14/14 PASSED |

---

## Files to Know

```
JINR-rag/
├── LAYERS.md                          ← System architecture
├── ARCHITECTURE_CLEAN.md              ← Overview
├── REFACTORING_SUMMARY.md             ← Changes & rationale
│
├── training_pipeline/README.md        ← Layer 0
├── rag/README.md                      ← Layer 1
├── llm/README.md                      ← Layer 2
├── remediation/README.md              ← Layers 3-4
│
├── training_pipeline/train.py         ← GNN model
├── remediation/run.py                 ← CLI entry point
├── remediation/pipeline.py            ← Orchestration
│
├── artifacts/inference_sample.json    ← GNN output
├── artifacts/remediation_report.json  ← Final output
├── checkpoints/best_model.pt          ← Model weights
└── tests/test_full_system_integration.py ← Full test
```

---

## Example Output

### Terminal
```
=== REMEDIATION REPORT ===
Incident: INC-2024-123-NETWORK_CONGESTION
Severity: CRITICAL
Summary: Network switch SWITCH-5 has congestion
Root Cause: SWITCH-5 (confidence: 78.3%)
Fault Type: network_congestion

Actions:
  1. [HIGH] CHECK_METRICS -> SWITCH-5
     Parameters: {"metrics": ["packet_drop", "error_rate"]}
     Est. TTR: 30s

  2. [HIGH] APPLY_QOS -> SWITCH-5
     Parameters: {"max_throughput_gbps": 80}
     Est. TTR: 10s

  3. [NORMAL] NOTIFY_OPERATOR -> OPS-TEAM
     Parameters: {"severity": "HIGH"}
     Est. TTR: 0s

Firewall Status: PASSED
Total TTR: 6.4 seconds
```

### JSON Output
```json
{
  "incident": {
    "id": "INC-2024-123-NETWORK_CONGESTION",
    "severity": "CRITICAL",
    "summary": "Network congestion at SWITCH-5"
  },
  "root_cause": {
    "node_type": "switch",
    "node_id": "SWITCH-5",
    "confidence": 0.783
  },
  "actions": [
    {
      "action_id": "CHECK_METRICS",
      "target_node": "SWITCH-5",
      "parameters": {...}
    }
  ],
  "firewall_status": "PASSED",
  "ttr_breakdown": {
    "rag_ms": 1.5,
    "llm_ms": 6400,
    "firewall_ms": 0.8,
    "total_ms": 6402
  }
}
```

---

## Common Tasks

### Retrain the GNN
```bash
python -m training_pipeline.train
# Creates new checkpoint in checkpoints/best_model_v*.pt
```

### Update SOPs (Knowledge Base)
Edit `rag/knowledge_base.py` and add new SOP entries.

### Change LLM Model
Edit `llm/llm_client.py`:
```python
client = LLMClient(
    model="mistral-large",  # or other model
    base_url="..."
)
```

### Disable Firewall (Testing Only)
```python
from remediation.pipeline import run_pipeline
playbook, metadata = run_pipeline(
    inference,
    firewall_validate=False  # Skip validation
)
```

---

## Troubleshooting

### "Ollama connection refused"
- Ensure Ollama is running: `ollama serve`
- Or use remote LLM backend in `llm/llm_client.py`

### "Qdrant not available"
- System gracefully falls back to empty context
- LLM still generates actions

### "Test failed: X != Y"
- Check Python version (3.10+)
- Check dependencies: `pip install -r requirements.txt`
- Check data files exist: `ls dataset/raw/ | wc -l` (should be 1000)

### "File not found: best_model.pt"
- Download from git-lfs or retrain: `python -m training_pipeline.train`

---

## Performance Tips

1. **Cache warmup**: First inference is slow (model loading). Subsequent calls are fast.
2. **Parallel layers**: RAG retrieval runs while LLM is generating (async possible).
3. **Quantization**: Mistral 7B can be quantized to 4-bit for 3x faster inference.
4. **Batching**: Process multiple incidents in a batch for better GPU utilization.

---

## Next Steps

1. **Read LAYERS.md** for detailed architecture
2. **Run the test** to verify everything works
3. **Review REFACTORING_SUMMARY.md** for design decisions
4. **Merge branch** when ready: `git merge refactor/clean-architecture`

---

## Questions?

- Architecture questions → LAYERS.md
- Layer 0 (GNN) → training_pipeline/README.md
- Layer 1 (RAG) → rag/README.md
- Layer 2 (LLM) → llm/README.md
- Layers 3-4 → remediation/README.md
- System design → ARCHITECTURE_CLEAN.md
