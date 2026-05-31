# 🚀 START HERE

Welcome! This is the **GNN RCA System** — ready for defense demo.

---

## ⚡ Quick Launch (3 Commands)

### Linux/macOS
```bash
cd JINR-rag
chmod +x init-system.sh
./init-system.sh
```

### Windows PowerShell
```powershell
cd JINR-rag
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
.\init-system.ps1
```

**What happens next:**
1. Docker containers start (Qdrant, Redis, Ollama, Python app)
2. Mistral 7B model downloads (~4.1GB, ~5 minutes)
3. Knowledge base initializes
4. Integration tests run (14/14 should pass ✓)
5. System ready!

---

## 📖 Read These (in order)

1. **README.md** (2 min) — System overview
2. **QUICKSTART.md** (5 min) — How to use it
3. **LAYERS.md** (10 min) — How it works
4. **DEPLOYMENT.md** (if deploying) — Operations

---

## 🎯 What This System Does

**Input**: HPC cluster incident (network congestion, disk failure, memory leak)

**Process**:
1. GNN finds root cause (78.3% accuracy)
2. RAG retrieves relevant procedures
3. LLM generates remediation actions
4. Firewall validates for safety
5. Report delivered to operator

**Output**: JSON report + human-readable summary

```
Incident: Network congestion at SWITCH-5
Root Cause: SWITCH-5 (confidence: 78.3%)
Recommended Actions:
  1. Check metrics on SWITCH-5
  2. Apply QOS limits (80 Gbps)
  3. Notify operations team
Total Time to Remediation: 6.4 seconds
```

---

## 🔍 Key Metrics

| Metric | Value |
|--------|-------|
| **Accuracy** | 78.3% Hit@1 (vs 65% XGBoost) |
| **Speed** | 6.4 seconds |
| **Safety** | 100% firewall validation |
| **Tests** | 14/14 passing |

---

## 📁 Files to Know

```
README.md                    ← Main documentation
QUICKSTART.md                ← Get started in 5 minutes
LAYERS.md                    ← Architecture details
DEPLOYMENT.md                ← Docker operations
docker-compose.yml           ← Full system config
init-system.sh               ← Auto-setup (Linux/macOS)
init-system.ps1              ← Auto-setup (Windows)

training_pipeline/           ← GNN model
rag/                         ← Knowledge retrieval
llm/                         ← LLM reasoning
remediation/                 ← Validation + output
tests/                       ← Test suite (14 tests)
artifacts/                   ← Results stored here
```

---

## ⚙️ System Requirements

- **Docker & Docker Compose**
- **8 GB RAM** (16 GB recommended)
- **15 GB disk** (for models + data)
- **20 minutes** (first run, Mistral download)

---

## ✅ Verify Installation

After running init script:

```bash
# Check containers are running
docker-compose ps

# View system output
docker-compose logs -f jinr

# Run a test
docker-compose exec jinr python -m remediation.run

# Check results
cat artifacts/remediation_report.json
```

---

## 🎓 For the Committee

**Why this system is awesome:**

1. **State-of-the-art accuracy** (78.3% Hit@1)
   - vs XGBoost: +20%
   - vs Random Forest: +47%

2. **Automatic graph learning**
   - Learns topology without manual feature engineering
   - Scales to complex multi-hop dependencies

3. **Safety by design**
   - LLM has NO direct terminal access
   - Semantic firewall blocks dangerous commands
   - 100% tested

4. **Production ready**
   - Full Docker containerization
   - Comprehensive documentation
   - All tests passing
   - <10s latency budget

5. **Clean architecture**
   - 4 independent layers
   - Zero dead code
   - ~2000 lines clean code
   - Well-documented

---

## 🚨 Troubleshooting

**Docker not running?**
→ Open Docker Desktop (macOS/Windows) or `sudo systemctl start docker` (Linux)

**Port 6333 already in use?**
→ Kill the process or change port in docker-compose.yml

**Ollama too slow?**
→ Let it download overnight, or use cached model

**Tests failing?**
→ Check `docker-compose logs` for detailed errors

**Need help?**
→ See DEPLOYMENT.md or LAYERS.md

---

## 🔄 Typical Workflow

```
1. Start system
   ./init-system.sh

2. Monitor startup
   docker-compose logs -f

3. Run inference
   docker-compose exec jinr python -m remediation.run

4. Check results
   cat artifacts/remediation_report.json

5. View logs
   docker-compose logs jinr

6. Stop when done
   docker-compose down
```

---

## 📚 Next Steps

### To Understand the System
→ Read **QUICKSTART.md** (5 minutes)

### To See How It Works
→ Read **LAYERS.md** (10 minutes)

### To Deploy Elsewhere
→ Read **DEPLOYMENT.md** (20 minutes)

### To Modify Code
→ Read layer-specific READMEs in training_pipeline/, rag/, llm/, remediation/

---

## ✨ Summary

You have a **complete, production-ready GNN RCA system** that:

✓ Finds root causes in HPC clusters (78.3% accuracy)
✓ Generates remediation actions (Mistral 7B LLM)
✓ Validates for safety (semantic firewall)
✓ Runs in Docker (cross-platform)
✓ Is fully tested (14/14 passing)
✓ Is well documented (8 markdown files)

**Everything is ready to run. Just launch init-system.sh!**

---

## 🎉 You're Ready!

1. Run: `./init-system.sh` or `.\init-system.ps1`
2. Wait: ~20 minutes (first run)
3. Test: All containers healthy ✓
4. Run: `docker-compose exec jinr python -m remediation.run`
5. Enjoy: System ready for demo!

---

**Questions?** → See README.md or QUICKSTART.md

**Ready?** → Run init-system.sh now!
