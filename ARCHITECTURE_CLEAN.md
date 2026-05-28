# Clean Architecture: GNN RCA System

## Overview

Single, unified pipeline for Root Cause Analysis in HPC infrastructure:

```
┌─────────────────────────────────────────────────────────────┐
│                    LAYER 0: INFERENCE                       │
│  Input: GNN predictions (RC node, confidence, fault type)   │
│  File: artifacts/inference_sample.json                      │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│                    LAYER 1: KNOWLEDGE                       │
│  RAG: Retrieve relevant SOPs from vector database          │
│  Components:                                                │
│    - rag/retriever.py → semantic search (Qdrant)           │
│    - rag/redis_context.py → caching                        │
│    - rag/knowledge_base.py → SOP storage                   │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│                    LAYER 2: REASONING                       │
│  LLM: Generate remediation actions from context            │
│  Components:                                                │
│    - llm/llm_client.py → Mistral 7B (Ollama)               │
│    - llm/prompt_builder.py → prompt engineering            │
│    - llm/response_parser.py → structured output            │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│                    LAYER 3: VALIDATION                      │
│  Firewall: Block dangerous/invalid actions                 │
│  Components:                                                │
│    - remediation/firewall.py → semantic validation         │
│    - remediation/models.py → Pydantic schemas              │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│                    LAYER 4: AGGREGATION                     │
│  Output: Structured remediation playbook + metadata        │
│  Components:                                                │
│    - remediation/incident_aggregator.py → metadata         │
│    - remediation/pipeline.py → orchestration               │
│    - remediation/run.py → CLI interface                    │
└──────────────────────────┬──────────────────────────────────┘
                           │
                    artifacts/remediation_report.json


## Directory Structure

```
JINR-rag/
├── training_pipeline/        (GNN model training & inference)
│   ├── train.py              → GATv2Hetero architecture
│   ├── dataset_generator.py   → Synthetic dataset creation
│   ├── diagnostics.py         → RCA metrics (Hit@K, MRR)
│   └── versioning.py          → Model versioning
│
├── llm/                       (LAYER 2: Reasoning)
│   ├── llm_client.py          → Ollama/Mistral interface
│   ├── prompt_builder.py      → Prompt construction
│   └── response_parser.py     → Action extraction
│
├── rag/                       (LAYER 1: Knowledge)
│   ├── retriever.py           → Semantic search (Qdrant)
│   ├── redis_context.py       → Context caching
│   ├── knowledge_base.py      → SOP storage
│   ├── embedder.py            → Embedding generation
│   └── history_tickets.py     → Historical reference
│
├── remediation/               (LAYERS 3-4: Validation & Output)
│   ├── firewall.py            → Semantic validation
│   ├── pipeline.py            → Orchestration
│   ├── incident_aggregator.py → Metadata assembly
│   ├── models.py              → Pydantic schemas
│   └── run.py                 → CLI entry point
│
├── tests/                     (Test suite)
│   └── test_full_system_integration.py → E2E test
│
├── artifacts/                 (Generated outputs)
│   ├── inference_sample.json  ← GNN output
│   ├── remediation_report.json → Final report
│   └── visualizations/        → Charts & animations
│
├── checkpoints/               (Model weights)
│   └── best_model.pt          → v3.0.0 GATv2Hetero
│
└── dataset/                   (Synthetic training data)
    └── raw/                   → PyTorch graph files
```

## Data Flow

### 1. LAYER 0: INFERENCE
```
GNN Forward Pass
└─ Input: Heterogeneous graph
└─ Output: inference_sample.json
   {
     "graph_id": 123,
     "fault_type": "network_congestion",
     "rc_node": {"type": "switch", "id": "SWITCH-5"},
     "confidence": 0.783,
     "top5_candidates": [...],
     "victim_nodes": [...]
   }
```

### 2. LAYER 1: KNOWLEDGE
```
Retrieval Query
└─ "network_congestion root cause SWITCH-5 affected 27 nodes"
└─ Qdrant search → top-5 relevant SOPs
└─ Redis cache → context enrichment
```

### 3. LAYER 2: REASONING
```
Mistral 7B Generation
└─ Prompt: incident + SOP context + fault type
└─ Output: JSON playbook
   {
     "actions": [
       {"action_id": "CHECK_METRICS", ...},
       {"action_id": "APPLY_QOS", ...}
     ]
   }
```

### 4. LAYER 3: VALIDATION
```
Firewall Check
└─ Validate each action against ActionDSL
└─ Block destructive commands
└─ Output: PASSED / BLOCKED
```

### 5. LAYER 4: AGGREGATION
```
Final Report
└─ Combine: incident + RC + actions + metadata
└─ artifacts/remediation_report.json (JSON)
└─ Terminal output (human-readable)
```

## Performance Targets

| Component | Latency | Target |
|-----------|---------|--------|
| GNN Inference | 14 ms | < 20 ms |
| RAG Retrieval | < 2 ms | < 5 ms |
| LLM Generation | 6.4 s | < 10 s |
| Firewall Check | < 1 ms | < 5 ms |
| **Total TTR** | **6.4 s** | **< 10 s** |

## Testing

```bash
# Full integration test
pytest tests/test_full_system_integration.py -v

# Expected: 26/26 tests PASSED
```

## Metrics (v3.0.0)

| Metric | Value |
|--------|-------|
| RCA Hit@1 | 78.3% |
| Hit@3 | 82.6% |
| MRR | 0.823 |
| F1-score | 0.716 |
| Model params | 311,878 |

## Key Design Principles

1. **Single Responsibility**: Each layer handles one task
2. **Loose Coupling**: Layers communicate via JSON contracts
3. **High Cohesion**: Related components grouped by function
4. **No Legacy Code**: Removed L4-L6 Kafka pipelines
5. **Testable**: E2E test validates all layers

## Future Extensions

- **Real data validation**: Connect to Говорун telemetry API
- **MLOps cycle**: Experience Replay + retraining
- **Multi-model ensemble**: Stack GNN + XGBoost predictions
- **Streaming inference**: Replace file I/O with Kafka topics
