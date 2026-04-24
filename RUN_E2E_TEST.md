# End-to-End Test (L3 -> L4 -> L5 -> L6)

## 1) Start infrastructure (Kafka + Redis)

- Start Redis:
  `docker run -d --name e2e-redis -p 6379:6379 redis:7-alpine`
- Start Kafka (single-node local):
  `docker run -d --name e2e-kafka -p 9092:9092 -e KAFKA_NODE_ID=1 -e KAFKA_PROCESS_ROLES=broker,controller -e KAFKA_LISTENERS=PLAINTEXT://:9092,CONTROLLER://:9093 -e KAFKA_ADVERTISED_LISTENERS=PLAINTEXT://localhost:9092 -e KAFKA_CONTROLLER_LISTENER_NAMES=CONTROLLER -e KAFKA_LISTENER_SECURITY_PROTOCOL_MAP=CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT -e KAFKA_CONTROLLER_QUORUM_VOTERS=1@localhost:9093 -e KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR=1 -e KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR=1 -e KAFKA_TRANSACTION_STATE_LOG_MIN_ISR=1 -e KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS=0 confluentinc/cp-kafka:7.6.1`
- Create topics:
  - `docker exec e2e-kafka kafka-topics --create --topic L3_IN --bootstrap-server localhost:9092 --partitions 1 --replication-factor 1`
  - `docker exec e2e-kafka kafka-topics --create --topic L5_IN --bootstrap-server localhost:9092 --partitions 1 --replication-factor 1`
  - `docker exec e2e-kafka kafka-topics --create --topic L6_OUT --bootstrap-server localhost:9092 --partitions 1 --replication-factor 1`

## 2) Configure environment files

- Create `l4_gnn_inference/.env` from `l4_gnn_inference/.env.example`.
- Create `l5_rag_llm/.env` from `l5_rag_llm/.env.example`.
- IMPORTANT: In `l4_gnn_inference/.env`, temporarily set `MIN_THRESHOLD=0.0`, so a non-trained random GNN is more likely to trigger alerts for L5 during testing.

## 3) Open three terminals and start services

- Terminal #1 (L4):
  `python -m l4_gnn_inference.main`
- Terminal #2 (L5):
  `python -m l5_rag_llm.main`
- Terminal #3 (Mock L3 simulator):
  `python e2e_simulator/mock_l3_producer.py`

## 4) Observe final output in a fourth terminal

- Run consumer:
  `docker exec -it e2e-kafka kafka-console-consumer --bootstrap-server localhost:9092 --topic L6_OUT --from-beginning`
- Verify that messages contain original anomaly fields and generated `playbook`.
