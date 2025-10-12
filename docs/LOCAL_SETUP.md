# Local Development Setup

Complete guide to run the Temporal task system locally for testing before deploying to GCP.

## Prerequisites

- **Python 3.11+**
- **Docker Desktop** (for Mac/Windows) or **Docker Engine** (for Linux)
- **uv** (Python package manager)

## Step 1: Install Dependencies

### 1.1 Install uv (if not already installed)

```bash
# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Or with pip
pip install uv
```

### 1.2 Install Python dependencies

```bash
# Install project dependencies
uv pip install -e .
```

## Step 2: Start Temporal Server (Local)

### 2.1 Start services with Docker Compose

```bash
cd deployment
docker-compose up -d
```

This will start:
- **PostgreSQL** - Database for Temporal state (port 5432)
- **Temporal Server** - Workflow orchestration (port 7233)
- **Temporal Web UI** - Monitoring dashboard (port 8080)
- **FastAPI** - REST API (port 8000)
- **2 Base Workers** - Processing tasks

### 2.2 Verify services are running

```bash
# Check all services
docker-compose ps

# Wait for services to be ready (~30 seconds)
sleep 30

# Check Temporal health
docker-compose logs temporal | grep "Started"
```

### 2.3 Access Temporal Web UI

Open in browser: http://localhost:8080

You should see:
- Namespaces: `default`
- Task Queues: `default-task-queue`
- Workers: 2 connected

## Step 3: Test the API

### 3.1 Check API health

```bash
curl http://localhost:8000/
```

Expected response:
```json
{
  "app": "Temporal Task System",
  "version": "0.1.0",
  "status": "healthy"
}
```

### 3.2 View API documentation

Open in browser: http://localhost:8000/docs

You'll see interactive Swagger UI with all API endpoints.

## Step 4: Run Your First Task

### 4.1 Start a GDT Invoice Import task

```bash
curl -X POST http://localhost:8000/api/tasks/start \
  -H "Content-Type: application/json" \
  -d '{
    "task_type": "gdt_invoice_import",
    "task_params": {
      "company_id": "TEST_COMPANY",
      "company_name": "Test Company Ltd",
      "credentials": {
        "username": "test_user",
        "password": "test_pass"
      },
      "date_range_start": "2024-01-01",
      "date_range_end": "2024-03-31"
    }
  }'
```

Expected response:
```json
{
  "workflow_id": "gdt_invoice_import-TEST_COMPANY-2024-01-01-2024-03-31",
  "task_type": "gdt_invoice_import",
  "status": "running",
  "message": "Task gdt_invoice_import started successfully"
}
```

### 4.2 Check task status

```bash
curl http://localhost:8000/api/tasks/gdt_invoice_import-TEST_COMPANY-2024-01-01-2024-03-31/status
```

Expected response:
```json
{
  "workflow_id": "gdt_invoice_import-TEST_COMPANY-2024-01-01-2024-03-31",
  "task_type": "gdt_invoice_import",
  "status": "running",
  "progress": {
    "total_invoices": 142,
    "completed_invoices": 45,
    "failed_invoices": 2,
    "percentage": 31.69
  },
  "result": null,
  "error": null,
  "start_time": "2024-01-15T10:30:00Z",
  "end_time": null
}
```

### 4.3 Monitor in Temporal Web UI

1. Open http://localhost:8080
2. Click "Workflows" in the left sidebar
3. Find your workflow: `gdt_invoice_import-TEST_COMPANY-2024-01-01-2024-03-31`
4. Click to see detailed execution:
   - Event history
   - Activity progress
   - Input/output
   - Retry attempts
   - Real-time updates

### 4.4 View worker logs

```bash
# View all worker logs
docker-compose logs -f temporal-worker

# You'll see:
# - Login activity
# - Discovery of invoices (mock: 50-200 invoices)
# - Parallel fetching (10 concurrent)
# - Rate limiting in action
# - Final results
```

## Step 5: Test Rate Limiting

The mock implementation simulates rate limiting (429 errors) to demonstrate the smart retry strategy.

### 5.1 Start a large task

```bash
curl -X POST http://localhost:8000/api/tasks/start \
  -H "Content-Type: application/json" \
  -d '{
    "task_type": "gdt_invoice_import",
    "task_params": {
      "company_id": "LARGE_COMPANY",
      "company_name": "Large Company Corp",
      "credentials": {
        "username": "user",
        "password": "pass"
      },
      "date_range_start": "2024-01-01",
      "date_range_end": "2024-12-31"
    }
  }'
```

### 5.2 Watch rate limiting in action

```bash
docker-compose logs -f temporal-worker | grep -E "(429|Rate|backoff)"
```

You'll see:
```
⚠️  429 Rate Limit for invoice INV-LARGE_COMPANY-00023
⚠️  Setting rate limit backoff for LARGE_COMPANY: 30s
⚠️  Rate limited for LARGE_COMPANY, backing off for 28.3s
✅ Fetched invoice INV-LARGE_COMPANY-00023 (after retry)
```

## Step 6: Development Workflow

### 6.1 Run FastAPI separately (for hot reload)

If you want to modify the API code and see changes immediately:

```bash
# Stop the API container
cd deployment
docker-compose stop api

# Run FastAPI locally with hot reload
cd ..
uvicorn app.main:app --reload --port 8000
```

### 6.2 Run worker separately (for debugging)

If you want to modify worker code:

```bash
# Stop worker containers
cd deployment
docker-compose stop temporal-worker

# Run worker locally
cd ..
python -m temporal_app.worker
```

### 6.3 Make code changes

Edit files in:
- `app/` - FastAPI code
- `temporal_app/workflows/` - Workflow logic
- `temporal_app/activities/` - Activity implementations

Changes will reload automatically!

## Step 7: Useful Commands

### View logs

```bash
# All services
docker-compose logs -f

# Specific service
docker-compose logs -f temporal-worker
docker-compose logs -f api
docker-compose logs -f temporal

# Follow specific workflow in logs
docker-compose logs -f temporal-worker | grep "TEST_COMPANY"
```

### Restart services

```bash
# Restart all
docker-compose restart

# Restart specific service
docker-compose restart temporal-worker
```

### Scale workers

```bash
# Scale to 5 workers
docker-compose up -d --scale temporal-worker=5

# Back to 2
docker-compose up -d --scale temporal-worker=2
```

### Stop all services

```bash
docker-compose down

# Stop and remove volumes (clean slate)
docker-compose down -v
```

## Step 8: Add a New Task Type

The system is designed to be extensible. Here's how to add a new task type:

### 8.1 Add task type to models

Edit `app/models.py`:

```python
class TaskType(str, Enum):
    GDT_INVOICE_IMPORT = "gdt_invoice_import"
    # Add your new task
    DOCUMENT_PROCESSOR = "document_processor"
```

### 8.2 Create workflow

Create `temporal_app/workflows/document_processor.py`:

```python
from temporalio import workflow
from datetime import timedelta

@workflow.defn
class DocumentProcessorWorkflow:
    @workflow.run
    async def run(self, params: dict) -> dict:
        # Your workflow logic
        return {"status": "completed"}
```

### 8.3 Create activities

Create `temporal_app/activities/document_processor.py`:

```python
from temporalio import activity

@activity.defn
async def process_document(doc_url: str) -> dict:
    # Your activity logic
    return {"processed": True}
```

### 8.4 Register in worker

Edit `temporal_app/worker.py`:

```python
from temporal_app.workflows.document_processor import DocumentProcessorWorkflow
from temporal_app.activities.document_processor import process_document

worker = Worker(
    client,
    task_queue=settings.temporal_task_queue,
    workflows=[
        GdtInvoiceImportWorkflow,
        DocumentProcessorWorkflow,  # Add here
    ],
    activities=[
        login_to_gdt,
        discover_invoices,
        fetch_invoice,
        process_document,  # Add here
    ],
)
```

### 8.5 Register in API

Edit `app/main.py`:

```python
def _get_workflow_class(task_type: TaskType) -> Any:
    workflow_mapping = {
        TaskType.GDT_INVOICE_IMPORT: GdtInvoiceImportWorkflow,
        TaskType.DOCUMENT_PROCESSOR: DocumentProcessorWorkflow,  # Add here
    }
    return workflow_mapping.get(task_type)
```

### 8.6 Test new task type

```bash
curl -X POST http://localhost:8000/api/tasks/start \
  -H "Content-Type: application/json" \
  -d '{
    "task_type": "document_processor",
    "task_params": {
      "document_url": "https://example.com/doc.pdf"
    }
  }'
```

## Troubleshooting

### Issue: Port already in use

```bash
# Find and kill process using port
lsof -ti:8000 | xargs kill -9
lsof -ti:8080 | xargs kill -9
lsof -ti:7233 | xargs kill -9
```

### Issue: Temporal server not starting

```bash
# Check logs
docker-compose logs temporal

# Common fix: recreate containers
docker-compose down -v
docker-compose up -d
```

### Issue: Workers not connecting

```bash
# Check worker logs
docker-compose logs temporal-worker

# Should see: "✅ Connected to Temporal: temporal:7233"

# If not, restart workers
docker-compose restart temporal-worker
```

### Issue: API returning 503 errors

```bash
# Check if Temporal is accessible
docker-compose exec api curl temporal:7233

# Restart API
docker-compose restart api
```

### Issue: uv command not found

```bash
# Install uv
pip install uv

# Or use curl
curl -LsSf https://astral.sh/uv/install.sh | sh

# Then restart terminal
```

## Quick Reference

### URLs

- **API**: http://localhost:8000
- **API Docs**: http://localhost:8000/docs
- **Temporal UI**: http://localhost:8080

### Commands

```bash
# Start everything
docker-compose up -d

# View logs
docker-compose logs -f temporal-worker

# Stop everything
docker-compose down

# Clean restart
docker-compose down -v && docker-compose up -d
```

### Example API Calls

```bash
# Start task
curl -X POST http://localhost:8000/api/tasks/start \
  -H "Content-Type: application/json" \
  -d '{"task_type": "gdt_invoice_import", "task_params": {...}}'

# Check status
curl http://localhost:8000/api/tasks/{workflow_id}/status

# Cancel task
curl -X POST http://localhost:8000/api/tasks/{workflow_id}/cancel
```

## Next Steps

Once everything works locally:

1. ✅ Test all API endpoints
2. ✅ Verify workflows complete successfully
3. ✅ Check Temporal Web UI for observability
4. ✅ Test rate limiting behavior
5. ✅ Try multiple concurrent tasks
6. 🚀 Deploy to GCP (see [docs/DEPLOYMENT.md](DEPLOYMENT.md))

Happy coding! 🚀
