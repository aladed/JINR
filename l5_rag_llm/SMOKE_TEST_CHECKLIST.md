# L5 Smoke Test Checklist

1. Create and activate a Python 3.10+ virtual environment, then install deps:
   `pip install -r l5_rag_llm/requirements.txt`
2. Start Kafka and Redis in Docker (single-node local setup is enough).
3. Create `.env` from template and update secrets:
   - copy `l5_rag_llm/.env.example` -> `l5_rag_llm/.env`
   - set real `LLM_API_KEY` if you want to test live LLM calls
4. Run service from repo root:
   `python -m l5_rag_llm.main`
5. Simulate an alert into `L5_IN` (example with `kafka-console-producer`):
   `{"root_cause_node_type":"host","root_cause_node_id":42,"anomaly_score":0.97,"attention_weights":{"host__connected_to__switch":{"edge_index":[[0],[1]],"alpha":[[0.87]]}}}`
