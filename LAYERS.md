# System Layers: GNN RCA Pipeline

## Layer Model

```
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃                LAYER 0: INPUT (GNN)                    ┃
┃  ────────────────────────────────────────────────────  ┃
┃  File: artifacts/inference_sample.json                 ┃
┃  Size: ~1 KB per graph                                 ┃
┃  Rate: 150 graphs/test                                 ┃
┃                                                        ┃
┃  Schema: {                                             ┃
┃    graph_id, fault_type, rc_node,                     ┃
┃    confidence, top5_candidates, victim_nodes          ┃
┃  }                                                     ┃
┃                                                        ┃
┃  Entry Point: remediation/run.py                      ┃
┃  Load: remediation/pipeline.py::load_inference()      ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
                          │
                          ▼
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃           LAYER 1: KNOWLEDGE RETRIEVAL (RAG)            ┃
┃  ────────────────────────────────────────────────────  ┃
┃  Purpose: Find relevant SOP (Standard Operating Proc)  ┃
┃  Latency: < 2 ms (vector DB cache hit)                ┃
┃                                                        ┃
┃  Components:                                           ┃
┃  • rag/retriever.py                                    ┃
┃    └─ semantic_search(query, top_k=5)                 ┃
┃    └─ uses: Qdrant vector database                    ┃
┃                                                        ┃
┃  • rag/redis_context.py                                ┃
┃    └─ get_context(incident_id)                        ┃
┃    └─ cache: fault type, topology info                ┃
┃                                                        ┃
┃  • rag/knowledge_base.py                               ┃
┃    └─ load_sops()                                      ┃
┃    └─ 7 SOP types: CHECK_METRICS, APPLY_QOS, ...      ┃
┃                                                        ┃
┃  Output: {                                             ┃
┃    sop_chunks_retrieved: 5,                           ┃
┃    retrieval_method: "qdrant_semantic",               ┃
┃    sop_content: [...]                                 ┃
┃  }                                                     ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
                          │
                          ▼
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃         LAYER 2: REASONING & GENERATION (LLM)           ┃
┃  ────────────────────────────────────────────────────  ┃
┃  Purpose: Generate remediation actions from context    ┃
┃  Latency: 6.4 sec (Mistral 7B inference)              ┃
┃  Backend: Ollama (local) or remote API                ┃
┃                                                        ┃
┃  Components:                                           ┃
┃  • llm/llm_client.py                                   ┃
┃    └─ LLMClient(backend="ollama", model="mistral")    ┃
┃    └─ generate(prompt) → JSON response                ┃
┃                                                        ┃
┃  • llm/prompt_builder.py                               ┃
┃    └─ build_messages(inference, sops, context)        ┃
┃    └─ includes: incident summary, SOP guidance        ┃
┃                                                        ┃
┃  • llm/response_parser.py                              ┃
┃    └─ normalize_playbook_dict(raw_response)           ┃
┃    └─ extract: action_id, target, parameters          ┃
┃                                                        ┃
┃  Prompt Structure:                                     ┃
┃  ┌─────────────────────────────────────┐              ┃
┃  │ System: You are RCA remediation AI  │              ┃
┃  │ Context: {fault_type, rc_node, ...} │              ┃
┃  │ SOPs: {5 relevant procedures}       │              ┃
┃  │ Task: Generate remediation playbook │              ┃
┃  └─────────────────────────────────────┘              ┃
┃                                                        ┃
┃  Output: {                                             ┃
┃    actions: [                                          ┃
┃      {action_id, target_node, parameters}             ┃
┃    ]                                                   ┃
┃  }                                                     ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
                          │
                          ▼
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃      LAYER 3: VALIDATION & SECURITY (FIREWALL)          ┃
┃  ────────────────────────────────────────────────────  ┃
┃  Purpose: Block dangerous/invalid actions              ┃
┃  Latency: < 1 ms (semantic validation)                ┃
┃  Design: LLM has NO direct terminal access             ┃
┃                                                        ┃
┃  Components:                                           ┃
┃  • remediation/firewall.py                             ┃
┃    └─ validate_playbook(actions)                      ┃
┃    └─ allowed: CHECK_METRICS, APPLY_QOS, ...          ┃
┃    └─ blocked: rm -rf, kill *, dd if=/dev/zero        ┃
┃                                                        ┃
┃  • remediation/models.py                               ┃
┃    └─ Action(BaseModel)  [Pydantic]                   ┃
┃    └─ RemediationPlaybook(BaseModel)                  ┃
┃    └─ validates: type, target, parameters             ┃
┃                                                        ┃
┃  Validation Rules:                                     ┃
┃  ✓ action_id in ALLOWED_ACTIONS                       ┃
┃  ✓ target_node matches topology                       ┃
┃  ✓ parameters type match schema                       ┃
┃  ✗ keyword blacklist: rm, dd, kill, fork, ...        ┃
┃                                                        ┃
┃  Output: {                                             ┃
┃    firewall_status: "PASSED" | "BLOCKED",             ┃
┃    firewall_error: null | "error message"             ┃
┃  }                                                     ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
                          │
                          ▼
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃         LAYER 4: AGGREGATION & OUTPUT (REPORT)          ┃
┃  ────────────────────────────────────────────────────  ┃
┃  Purpose: Combine all pipeline data into report        ┃
┃  Latency: < 10 ms (aggregation + formatting)          ┃
┃                                                        ┃
┃  Components:                                           ┃
┃  • remediation/incident_aggregator.py                  ┃
┃    └─ aggregate(inference) → incident metadata        ┃
┃    └─ determines: severity, summary, category         ┃
┃                                                        ┃
┃  • remediation/pipeline.py                             ┃
┃    └─ run_pipeline(inference) → (playbook, metadata)  ┃
┃    └─ orchestrates: layers 1-3                        ┃
┃    └─ remediation_report_payload() → JSON             ┃
┃                                                        ┃
┃  • remediation/run.py                                  ┃
┃    └─ format_report(playbook, metadata)               ┃
┃    └─ human-readable terminal output                  ┃
┃    └─ saves: artifacts/remediation_report.json        ┃
┃                                                        ┃
┃  Final Output: {                                       ┃
┃    incident: {id, severity, summary, fault_type},    ┃
┃    rc_node: {type, id, host_id},                     ┃
┃    context: {hostname, os, labels},                  ┃
┃    knowledge: {sop_chunks, retrieval_method},        ┃
┃    playbook: {actions: [...]},                       ┃
┃    firewall_status: "PASSED",                         ┃
┃    ttr_breakdown: {                                   ┃
┃      gnn_ms, rag_ms, llm_ms, firewall_ms              ┃
┃    }                                                   ┃
┃  }                                                     ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
                          │
                          ▼
                   artifacts/remediation_report.json
                   (JSON output for integration)
                   
                   Terminal output
                   (Human-readable summary)
```

## Responsibilities by Layer

| Layer | Responsibility | Failure Mode | Recovery |
|-------|-----------------|--------------|----------|
| 0 | GNN inference | No RC found | Use null RC |
| 1 | Knowledge retrieval | No SOPs found | Use empty context |
| 2 | LLM generation | Invalid JSON | Parse with fallback |
| 3 | Validation | Action blocked | Use rule-based fallback |
| 4 | Aggregation | Never fails | Always produces report |

## Entry Points

### CLI
```bash
cd JINR-rag
python -m remediation.run
```

### Programmatic
```python
from remediation.pipeline import run_pipeline
playbook, metadata = run_pipeline(inference_dict)
```

### Testing
```bash
pytest tests/test_full_system_integration.py -v
```

## Performance Budget

```
Total TTR = 6.4 seconds

[GNN]      14 ms  (3.5% overhead)
[RAG]      <2 ms  (<0.1% overhead)
[LLM]      6.4s   (96% dominant)
[Firewall] <1 ms  (<0.1% overhead)
[Output]   <10ms  (<0.2% overhead)
```

LLM generation dominates. Optimization: parallel RAG retrieval or cached responses.

## Dependencies

### Python Packages
- **torch**: GNN inference
- **torch_geometric**: Heterogeneous graph operations
- **pydantic**: Schema validation
- **qdrant-client**: Vector database
- **redis**: Context caching
- **requests/httpx**: LLM API calls
- **pytest**: Testing

### External Services (Optional)
- **Ollama**: Local Mistral 7B inference
- **Qdrant**: Vector database (can run local Docker)
- **Redis**: Context cache (can run local Docker)

### Offline Mode
All layers gracefully degrade:
- RAG: Uses empty context if Qdrant unavailable
- LLM: Uses rule-based fallback if Ollama unavailable
- Firewall: Always validates locally
