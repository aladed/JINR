# L4 Smoke Test Checklist

## 1) Prepare environment

- [ ] Create and activate a Python 3.10+ virtual environment.
- [ ] Install dependencies:
      `pip install -r l4_gnn_inference/requirements.txt`
- [ ] Create runtime env file:
      `copy l4_gnn_inference\.env.example l4_gnn_inference\.env` (Windows)
      or `cp l4_gnn_inference/.env.example l4_gnn_inference/.env` (Linux/macOS)

## 2) Start Kafka and Redis locally (Docker)

- [ ] Make sure Docker is running.
- [ ] Start Redis:
      `docker run -d --name l4-redis -p 6379:6379 redis:7-alpine`
- [ ] Start Kafka (single-node KRaft):
      `docker run -d --name l4-kafka -p 9092:9092 -e KAFKA_NODE_ID=1 -e KAFKA_PROCESS_ROLES=broker,controller -e KAFKA_LISTENERS=PLAINTEXT://:9092,CONTROLLER://:9093 -e KAFKA_ADVERTISED_LISTENERS=PLAINTEXT://localhost:9092 -e KAFKA_CONTROLLER_LISTENER_NAMES=CONTROLLER -e KAFKA_LISTENER_SECURITY_PROTOCOL_MAP=CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT -e KAFKA_CONTROLLER_QUORUM_VOTERS=1@localhost:9093 -e KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR=1 -e KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR=1 -e KAFKA_TRANSACTION_STATE_LOG_MIN_ISR=1 -e KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS=0 confluentinc/cp-kafka:7.6.1`

## 3) Create Kafka topics

- [ ] Create input topic:
      `docker exec l4-kafka kafka-topics --create --topic L3_IN --bootstrap-server localhost:9092 --partitions 1 --replication-factor 1`
- [ ] Create output topic:
      `docker exec l4-kafka kafka-topics --create --topic L5_OUT --bootstrap-server localhost:9092 --partitions 1 --replication-factor 1`
- [ ] Verify topics:
      `docker exec l4-kafka kafka-topics --list --bootstrap-server localhost:9092`

## 4) Run L4 service

- [ ] Start service from repo root:
      `python -m l4_gnn_inference.main`
- [ ] Confirm logs contain:
  - startup complete messages,
  - Redis connectivity success,
  - signal handlers registration.

## 5) Validate output path (before Mock L3 exists)

- [ ] Open Kafka console consumer for L5 output:
      `docker exec -it l4-kafka kafka-console-consumer --bootstrap-server localhost:9092 --topic L5_OUT --from-beginning`
- [ ] Keep this terminal open for upcoming Mock L3 producer test.

## 6) Graceful shutdown check

- [ ] Stop service with `Ctrl+C`.
- [ ] Confirm logs show:
  - shutdown signal received,
  - resource cleanup (`consumer.close`, `producer.flush`, `redis.close`),
  - safe stop message.
