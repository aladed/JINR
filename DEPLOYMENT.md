# Deployment Guide: GNN RCA System

## Overview

Complete Docker-based deployment with all dependencies (Qdrant, Redis, Ollama, Python app).

**Features**:
- ✓ Full containerization
- ✓ Automated health checks
- ✓ Volume persistence
- ✓ Network isolation
- ✓ One-command initialization
- ✓ Cross-platform (Linux, macOS, Windows)

---

## Prerequisites

### System Requirements
- **Docker Desktop** 4.0+
- **Docker Compose** 2.0+
- **Disk space**: 15 GB minimum (Ollama models ~4GB, Qdrant vectors ~2GB, misc ~2GB)
- **RAM**: 8 GB minimum (16 GB recommended for Mistral 7B)
- **CPU**: 4+ cores recommended

### Installation

#### Linux (Ubuntu/Debian)
```bash
# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Install Docker Compose (built into Docker Desktop, or)
sudo apt-get install docker-compose-plugin

# Add your user to docker group (optional)
sudo usermod -aG docker $USER
newgrp docker
```

#### macOS
```bash
# Install Docker Desktop
brew install --cask docker

# Start Docker Desktop from Applications menu
```

#### Windows
```powershell
# Install Docker Desktop
choco install docker-desktop

# Or download from: https://www.docker.com/products/docker-desktop
```

---

## Quick Start (3 Steps)

### 1. Clone and Navigate
```bash
cd JINR-rag
```

### 2. Run Initialization Script

**Linux/macOS**:
```bash
chmod +x init-system.sh
./init-system.sh
```

**Windows PowerShell**:
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
.\init-system.ps1
```

**Manual (Any OS)**:
```bash
docker-compose up -d
# Wait 30 seconds for services to start
docker-compose exec jinr ollama pull mistral
docker-compose exec jinr pytest tests/ -v
```

### 3. Run the System
```bash
docker-compose exec jinr python -m remediation.run
# Output: artifacts/remediation_report.json
```

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│ Docker Network: jinr_network                        │
└─────────────────────────────────────────────────────┘
           │
    ┌──────┼──────┬──────────────┐
    │      │      │              │
    ▼      ▼      ▼              ▼
  Redis  Qdrant Ollama    Python App
  :6379  :6333  :11434     (flask/cli)
    │      │      │              │
    └──────┴──────┴──────────────┘
           │
    ┌──────▼──────┐
    │  Shared     │
    │  Volumes:   │
    │  - artifacts│
    │  - dataset  │
    │  - checkpts │
    └─────────────┘
```

### Services

| Service | Image | Port | Purpose |
|---------|-------|------|---------|
| **redis** | redis:7-alpine | 6379 | Context caching |
| **qdrant** | qdrant/qdrant | 6333 | Vector database |
| **ollama** | ollama/ollama | 11434 | LLM inference |
| **jinr** | custom (Dockerfile) | 8000 | Python app |

---

## Configuration

### Using Environment Variables

Create `.env` file from template:
```bash
cp .env.example .env
```

Edit `.env` to customize:
```bash
# Example modifications:
OLLAMA_HOST=http://ollama:11434
LLM_MODEL=mistral
LLM_TEMPERATURE=0.3
REDIS_HOST=redis
QDRANT_HOST=qdrant
```

Docker Compose will automatically load from `.env`.

### Configuration Files

- **docker-compose.yml** — Service definitions
- **.env.example** — Environment template
- **Dockerfile** — Python application image

---

## Operations

### Start System
```bash
# Start all containers
docker-compose up -d

# Follow logs
docker-compose logs -f
```

### Stop System
```bash
# Stop containers (preserve volumes)
docker-compose down

# Stop and remove volumes (full cleanup)
docker-compose down -v
```

### Run Commands in Container
```bash
# Run single command
docker-compose exec jinr python -m remediation.run

# Interactive shell
docker-compose exec jinr /bin/bash

# Run tests
docker-compose exec jinr pytest tests/ -v

# View logs
docker-compose logs jinr        # Current logs
docker-compose logs -f jinr     # Follow logs
docker-compose logs --tail=100  # Last 100 lines
```

### Health Checks
```bash
# Check if all services are healthy
docker-compose ps

# Manually check service status
curl http://localhost:6333/health      # Qdrant
curl http://localhost:11434/api/tags   # Ollama
redis-cli -p 6379 ping                 # Redis (via docker)
docker exec jinr_redis redis-cli ping
```

### Rebuild Images
```bash
# Rebuild Python app image after code changes
docker-compose build jinr

# Rebuild all images
docker-compose build

# Rebuild without cache
docker-compose build --no-cache
```

---

## Troubleshooting

### Docker Daemon Not Running
**Error**: `Cannot connect to Docker daemon`

**Fix**:
- Windows/macOS: Open Docker Desktop application
- Linux: `sudo systemctl start docker`

### Port Already in Use
**Error**: `Bind for 0.0.0.0:6333 failed: port is already allocated`

**Fix**:
```bash
# Find what's using the port
lsof -i :6333  # macOS/Linux
netstat -ano | findstr :6333  # Windows

# Kill the process or change port in docker-compose.yml
```

### Ollama Model Download Too Slow
**Tip**: Let it run overnight, or pull manually:
```bash
docker exec jinr_ollama ollama pull mistral:latest
```

### Out of Memory
**Error**: `OOMKilled` container

**Fix**:
1. Allocate more RAM to Docker:
   - Windows/macOS: Docker Desktop → Settings → Resources
   - Linux: Check system RAM with `free -h`
2. Or use quantized model:
   ```bash
   docker exec jinr_ollama ollama pull mistral:4bit
   ```

### Redis Data Loss
**Issue**: Data disappears after restart

**Fix**:
- Ensure volume persistence:
  ```bash
  docker volume ls  # Check volumes exist
  docker-compose exec redis redis-cli BGSAVE  # Manual save
  ```

### Qdrant Vector Search Failing
**Error**: `UNAVAILABLE_COLLECTION` or `NOT_FOUND_COLLECTION`

**Fix**:
```bash
# Reinitialize knowledge base
docker-compose exec jinr python -c \
  "from rag.knowledge_base import load_sops; load_sops()"
```

---

## Performance Optimization

### 1. GPU Acceleration (Ollama)
For faster Mistral inference on NVIDIA GPUs:

```bash
# Install nvidia-docker
distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
curl -s -L https://nvidia.github.io/nvidia-docker/gpgkey | sudo apt-key add -
curl -s -L https://nvidia.github.io/nvidia-docker/$distribution/nvidia-docker.list | \
  sudo tee /etc/apt/sources.list.d/nvidia-docker.list

sudo apt-get update && sudo apt-get install -y nvidia-docker2
sudo systemctl restart docker

# Modify docker-compose.yml:
# ollama:
#   runtime: nvidia
#   environment:
#     - NVIDIA_VISIBLE_DEVICES=all
```

### 2. Quantized Models
Use 4-bit quantized Mistral for faster inference:

```bash
docker exec jinr_ollama ollama pull mistral:4bit
# Update .env: LLM_MODEL=mistral:4bit
```

### 3. Caching
Enable Redis caching for identical queries:

```bash
# In .env
DISABLE_CACHE=false
REDIS_TTL_SECONDS=3600
```

### 4. Batch Processing
Process multiple incidents in parallel:

```python
from remediation.pipeline import run_pipeline
from concurrent.futures import ThreadPoolExecutor

inferences = [...]  # List of incident JSONs

with ThreadPoolExecutor(max_workers=4) as executor:
    results = executor.map(run_pipeline, inferences)
```

---

## Monitoring & Logging

### View Logs
```bash
# All services
docker-compose logs

# Specific service
docker-compose logs jinr
docker-compose logs ollama
docker-compose logs qdrant
docker-compose logs redis

# Real-time follow
docker-compose logs -f

# Last N lines
docker-compose logs --tail=50
```

### Metrics
```bash
# Check resource usage
docker stats

# Container inspection
docker inspect jinr_app
```

### Log Files
Logs are available at:
- **Application**: `artifacts/app.log`
- **Qdrant**: Docker logs
- **Redis**: Docker logs
- **Ollama**: Docker logs

---

## Deployment Scenarios

### Development (Local)
```bash
docker-compose up -d
docker-compose exec jinr python -m remediation.run
```

### Testing (CI/CD)
```bash
docker-compose up -d
docker-compose exec -T jinr pytest tests/ -v
docker-compose down -v
```

### Production (Linux Server)
```bash
# Run in background with log rotation
docker-compose up -d
docker-compose logs -f > /var/log/jinr.log 2>&1 &

# Monitor with systemd
# Create: /etc/systemd/system/docker-jinr.service
[Unit]
Description=GNN RCA System
After=docker.service
Requires=docker.service

[Service]
Type=simple
WorkingDirectory=/app/JINR-rag
ExecStart=/usr/bin/docker-compose up
ExecStop=/usr/bin/docker-compose down
Restart=always

[Install]
WantedBy=multi-user.target
```

### Kubernetes Deployment
```bash
# Convert docker-compose to Kubernetes manifests
kompose convert -f docker-compose.yml

# Deploy
kubectl apply -f *.yaml
```

---

## Backup & Restore

### Backup Volumes
```bash
# Backup Qdrant data
docker run --rm -v jinr-rag_qdrant_storage:/data \
  -v $(pwd):/backup \
  alpine tar czf /backup/qdrant-backup.tar.gz -C /data .

# Backup Redis data
docker run --rm -v jinr-rag_redis_storage:/data \
  -v $(pwd):/backup \
  alpine tar czf /backup/redis-backup.tar.gz -C /data .

# Backup Ollama models
docker run --rm -v jinr-rag_ollama_models:/data \
  -v $(pwd):/backup \
  alpine tar czf /backup/ollama-backup.tar.gz -C /data .
```

### Restore from Backup
```bash
# Remove old volumes
docker-compose down -v

# Restore
docker volume create jinr-rag_qdrant_storage
docker volume create jinr-rag_redis_storage
docker volume create jinr-rag_ollama_models

docker run --rm -v jinr-rag_qdrant_storage:/data \
  -v $(pwd):/backup \
  alpine tar xzf /backup/qdrant-backup.tar.gz -C /data

# Restart
docker-compose up -d
```

---

## Cleanup

### Remove Everything
```bash
# Stop containers and remove volumes
docker-compose down -v

# Remove images (optional)
docker rmi jinr-app qdrant/qdrant redis:7-alpine ollama/ollama
```

### Prune Unused Resources
```bash
# Remove dangling images
docker image prune

# Remove unused volumes
docker volume prune

# Remove unused networks
docker network prune

# Full cleanup
docker system prune -a --volumes
```

---

## Debugging

### Execute Python Commands
```bash
docker-compose exec jinr python << 'EOF'
from remediation.pipeline import run_pipeline
import json

inference = json.load(open("artifacts/inference_sample.json"))
playbook, metadata = run_pipeline(inference)
print(f"Actions: {len(playbook.actions)}")
print(f"Status: {metadata['firewall_status']}")
EOF
```

### Check Service Connectivity
```bash
docker-compose exec jinr python << 'EOF'
import os
from qdrant_client import QdrantClient
from redis import Redis

# Test Qdrant
qdrant = QdrantClient(host=os.getenv("QDRANT_HOST"), port=6333)
print(f"Qdrant: {qdrant.get_collections()}")

# Test Redis
redis = Redis(host=os.getenv("REDIS_HOST"), port=6379)
print(f"Redis: {redis.ping()}")
EOF
```

---

## Support

For issues:

1. Check troubleshooting section above
2. Review logs: `docker-compose logs`
3. Verify all services are healthy: `docker-compose ps`
4. Check documentation: QUICKSTART.md, LAYERS.md
5. Run tests: `docker-compose exec jinr pytest tests/ -v`

---

## References

- **Docker Compose**: https://docs.docker.com/compose
- **Docker CLI**: https://docs.docker.com/engine/reference/commandline
- **Qdrant**: https://qdrant.tech/documentation
- **Ollama**: https://github.com/ollama/ollama
- **Redis**: https://redis.io/documentation
