# Temporal Task System - Extensible Workflow Orchestration

A production-ready, extensible task orchestration system using Temporal.io with FastAPI, designed for scalable task execution on GCP.

## Features

- ✅ **Extensible Task Types** - Easy to add new task types beyond GDT invoice import
- ✅ **Hybrid Architecture** - Base workers (Compute Engine) + Burst workers (Cloud Run Jobs)
- ✅ **Stateless API** - No database required, all state managed by Temporal
- ✅ **Smart Rate Limiting** - Prevents cascade failures with shared backoff state
- ✅ **Cost-Effective** - $36-65/mo for scalable infrastructure
- ✅ **Observable** - Temporal Web UI for real-time monitoring
- ✅ **Durable** - Workflows survive crashes and restarts

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Compute Engine (ai-core-instance)                           │
│  ├── Temporal Server (self-hosted)                           │
│  ├── PostgreSQL (Temporal state)                             │
│  ├── FastAPI (stateless API)                                 │
│  └── 2 Base Workers (always running)                         │
│  Cost: ~$50/mo                                                │
└──────────────────────────────────────────────────────────────┘
                    ↓ (when queue is busy)
┌──────────────────────────────────────────────────────────────┐
│  Cloud Run Jobs (Burst Workers)                              │
│  ├── 0-100 workers on demand                                 │
│  ├── Process backlog quickly                                 │
│  └── Exit when done → $0 cost                                │
│  Cost: ~$1-5 per batch execution                             │
└──────────────────────────────────────────────────────────────┘
```

## Quick Start (Local)

**Prerequisites:** Python 3.11+, Docker, uv

```bash
# 1. Install dependencies
uv pip install -e .

# 2. Start all services (Temporal + API + Workers)
cd deployment && docker-compose up -d

# 3. Test the API
curl http://localhost:8000/

# 4. Start a task
curl -X POST http://localhost:8000/api/tasks/start \
  -H "Content-Type: application/json" \
  -d '{
    "task_type": "gdt_invoice_import",
    "task_params": {
      "company_id": "TEST_COMPANY",
      "company_name": "Test Company",
      "credentials": {"username": "user", "password": "pass"},
      "date_range_start": "2024-01-01",
      "date_range_end": "2024-03-31"
    }
  }'

# 5. Monitor in Temporal Web UI
open http://localhost:8080
```

**Access Points:**
- API: http://localhost:8000
- API Docs: http://localhost:8000/docs
- Temporal UI: http://localhost:8080

📖 **Detailed Setup Guide:** [docs/LOCAL_SETUP.md](docs/LOCAL_SETUP.md)

## Deployment to GCP

```bash
# 1. Configure environment
cp .env.example .env
# Edit .env with your GCP project details

# 2. Deploy to Compute Engine
cd deployment
./deploy-to-gcp.sh

# 3. Deploy Cloud Run burst workers
./deploy-burst.sh
```

📖 **Full Deployment Guide:** [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)

## Project Structure

```
.
├── app/                          # FastAPI application
│   ├── main.py                   # Stateless API
│   ├── config.py                 # Configuration
│   └── models.py                 # Pydantic models
├── temporal_app/                 # Temporal workflows & activities
│   ├── workflows/
│   │   └── gdt_invoice_import.py # GDT invoice import workflow
│   ├── activities/
│   │   ├── gdt_auth.py           # Login activities
│   │   ├── gdt_discovery.py      # Invoice discovery
│   │   └── gdt_fetch.py          # Invoice fetching (with rate limiting)
│   ├── models.py                 # Data models
│   └── worker.py                 # Worker (base + burst modes)
├── deployment/                   # Deployment files
│   ├── docker-compose.yml        # Local/Compute Engine setup
│   ├── Dockerfile.api            # FastAPI container
│   ├── Dockerfile.worker         # Worker container
│   ├── setup-compute-engine.sh   # Compute Engine setup script
│   └── deploy-burst.sh           # Cloud Run Jobs deployment
├── docs/                         # Documentation
│   ├── LOCAL_SETUP.md            # Detailed local setup guide
│   └── DEPLOYMENT.md             # Production deployment guide
└── pyproject.toml                # Python dependencies (uv)
```

## Adding New Task Types

The system is designed to be extensible. Example: Adding a document processor task.

```python
# 1. Add to app/models.py
class TaskType(str, Enum):
    GDT_INVOICE_IMPORT = "gdt_invoice_import"
    DOCUMENT_PROCESSOR = "document_processor"  # New task type

# 2. Create workflow in temporal_app/workflows/document_processor.py
@workflow.defn
class DocumentProcessorWorkflow:
    @workflow.run
    async def run(self, params: dict) -> dict:
        # Your workflow logic here
        pass

# 3. Create activities in temporal_app/activities/document_processor.py
@activity.defn
async def process_document(doc_url: str) -> dict:
    # Your activity logic here
    pass

# 4. Register in worker.py and main.py
# (See docs/LOCAL_SETUP.md for details)
```

## Cost Breakdown

| Component | Monthly Cost |
|-----------|-------------|
| Compute Engine (e2-medium, base) | $25-40 |
| Cloud Run Jobs (burst, on-demand) | $10-20 |
| Cloud Storage (minimal) | $1-5 |
| **Total** | **$36-65** |

### Scaling Costs

For high throughput (100 companies × 1000 invoices):
- Compute Engine: e2-standard-2 ($50/mo)
- Burst executions: $2/batch × 10/day ($600/mo)
- **Total: ~$650/mo** for high-scale workload

## Documentation

- [Local Setup Guide](docs/LOCAL_SETUP.md) - Complete local development guide
- [Deployment Guide](docs/DEPLOYMENT.md) - Production deployment on GCP
- [Master Plan](MASTER_PLAN.md) - Original architecture and design decisions

## License

MIT
