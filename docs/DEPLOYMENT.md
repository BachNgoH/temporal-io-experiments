# Deployment Guide - Hybrid Temporal System on GCP

Complete guide to deploy the Temporal task system with **Option 3: Hybrid Architecture** (Compute Engine + Cloud Run Jobs).

## Architecture Overview

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

## Prerequisites

1. **GCP Account** with billing enabled
2. **gcloud CLI** installed and authenticated
   ```bash
   gcloud auth login
   gcloud config set project YOUR_PROJECT_ID
   ```
3. **Docker** installed locally (for building images)
4. **Git** (optional, for version control)

## Step 1: Configure Environment

### 1.1 Create `.env` file

```bash
cp .env.example .env
```

### 1.2 Edit `.env` with your GCP settings

```bash
# Required: Update these values
GCP_PROJECT_ID=your-actual-project-id
GCP_REGION=us-central1
GCP_INSTANCE_NAME=ai-core-instance
GCP_ZONE=us-central1-a

# Other settings (defaults are fine)
TEMPORAL_HOST=localhost:7233
TEMPORAL_NAMESPACE=default
TEMPORAL_TASK_QUEUE=default-task-queue
```

**Important:** Never commit `.env` to git (already in `.gitignore`).

## Step 2: Deploy to Compute Engine

### Option A: Automated Deployment (Recommended)

Run the deployment script from your local machine:

```bash
cd deployment
./deploy-to-gcp.sh
```

This will:
1. Create `ai-core-instance` (if doesn't exist)
2. Upload application code
3. Install Docker & Docker Compose
4. Start all services (Temporal, FastAPI, Workers)
5. Configure firewall rules

### Option B: Manual Deployment

If you prefer manual deployment:

#### 2.1 Create Compute Engine Instance

```bash
gcloud compute instances create ai-core-instance \
  --project=YOUR_PROJECT_ID \
  --zone=us-central1-a \
  --machine-type=e2-medium \
  --boot-disk-size=30GB \
  --boot-disk-type=pd-balanced \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --tags=temporal-server \
  --metadata=enable-oslogin=TRUE
```

#### 2.2 Upload Code to Instance

```bash
# From project root
gcloud compute scp --recurse \
  app/ \
  temporal_app/ \
  deployment/ \
  pyproject.toml \
  .python-version \
  ai-core-instance:~/temporal-deployment/ \
  --zone=us-central1-a
```

#### 2.3 SSH into Instance and Run Setup

```bash
# SSH to instance
gcloud compute ssh ai-core-instance --zone=us-central1-a

# Run setup script
cd ~/temporal-deployment/deployment
chmod +x setup-compute-engine.sh
./setup-compute-engine.sh
```

## Step 3: Verify Deployment

After deployment completes, you'll see output like:

```
✅✅✅ Temporal setup complete! ✅✅✅

📍 Access Points:
  🌐 Temporal Server:  34.123.45.67:7233
  🖥️  Temporal Web UI:  http://34.123.45.67:8080
  🚀 FastAPI:          http://34.123.45.67:8000
  📊 API Docs:         http://34.123.45.67:8000/docs
```

### 3.1 Test FastAPI

```bash
curl http://YOUR_EXTERNAL_IP:8000/
```

Expected response:
```json
{
  "app": "Temporal Task System",
  "version": "0.1.0",
  "status": "healthy"
}
```

### 3.2 Access Temporal Web UI

Open in browser: `http://YOUR_EXTERNAL_IP:8080`

You should see the Temporal Web UI showing workflows, workers, and task queues.

### 3.3 Check Base Workers

```bash
# SSH to instance
gcloud compute ssh ai-core-instance --zone=us-central1-a

# View worker logs
cd ~/temporal-deployment
docker-compose -f deployment/docker-compose.yml logs -f temporal-worker
```

Expected logs:
```
✅ Connected to Temporal: temporal:7233
🔄 Starting to poll for tasks...
```

## Step 4: Deploy Cloud Run Burst Workers

### 4.1 Update `.env` with External IP

After Compute Engine is deployed, update your local `.env`:

```bash
COMPUTE_ENGINE_EXTERNAL_IP=34.123.45.67:7233
```

### 4.2 Deploy Burst Workers

```bash
cd deployment
./deploy-burst.sh
```

This will:
1. Build worker Docker image
2. Push to Google Container Registry
3. Deploy Cloud Run Job

### 4.3 Test Burst Workers

Execute burst workers manually:

```bash
# Run 10 burst workers
gcloud run jobs execute temporal-worker-burst \
  --region us-central1 \
  --tasks 10
```

View execution status:
```bash
gcloud run jobs executions list \
  --job temporal-worker-burst \
  --region us-central1
```

## Step 5: Test End-to-End

### 5.1 Start a Task via API

```bash
curl -X POST http://YOUR_EXTERNAL_IP:8000/api/tasks/start \
  -H "Content-Type: application/json" \
  -d '{
    "task_type": "gdt_invoice_import",
    "task_params": {
      "company_id": "ACME",
      "company_name": "ACME Corp",
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
  "workflow_id": "gdt_invoice_import-ACME-2024-01-01-2024-03-31",
  "task_type": "gdt_invoice_import",
  "status": "running",
  "message": "Task gdt_invoice_import started successfully"
}
```

### 5.2 Check Task Status

```bash
curl http://YOUR_EXTERNAL_IP:8000/api/tasks/gdt_invoice_import-ACME-2024-01-01-2024-03-31/status
```

### 5.3 View in Temporal UI

1. Open `http://YOUR_EXTERNAL_IP:8080`
2. Click on "Workflows"
3. Find workflow: `gdt_invoice_import-ACME-2024-01-01-2024-03-31`
4. See real-time progress, activities, and logs

## Step 6: Production Considerations

### 6.1 Secure API with Authentication

Add authentication to FastAPI (not included in mock):

```python
# In app/main.py
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

security = HTTPBearer()

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if credentials.credentials != "your-secret-token":
        raise HTTPException(status_code=403, detail="Invalid token")
    return credentials
```

### 6.2 Enable HTTPS

Use Cloud Load Balancer or Nginx reverse proxy with SSL certificate.

### 6.3 Setup Monitoring

```bash
# View logs in Cloud Logging
gcloud logging read "resource.type=gce_instance AND resource.labels.instance_id=ai-core-instance" --limit 50

# Set up alerts for worker failures
# (Configure in Cloud Monitoring console)
```

### 6.4 Backup PostgreSQL Data

```bash
# SSH to instance
gcloud compute ssh ai-core-instance --zone=us-central1-a

# Backup Temporal database
docker-compose -f deployment/docker-compose.yml exec postgresql \
  pg_dump -U temporal temporal > temporal_backup.sql
```

### 6.5 Auto-trigger Burst Workers

Create a Cloud Function or Cloud Scheduler job to automatically trigger burst workers when queue depth is high:

```bash
# Example: Trigger burst workers every hour during peak times
gcloud scheduler jobs create http trigger-burst-workers \
  --schedule="0 9-17 * * 1-5" \
  --uri="https://run.googleapis.com/v1/namespaces/YOUR_PROJECT/jobs/temporal-worker-burst:run" \
  --http-method=POST
```

## Troubleshooting

### Issue: Can't access Temporal Web UI

**Solution:** Check firewall rules

```bash
# Create firewall rule
gcloud compute firewall-rules create temporal-server \
  --direction=INGRESS \
  --priority=1000 \
  --network=default \
  --action=ALLOW \
  --rules=tcp:7233,tcp:8080,tcp:8000 \
  --source-ranges=0.0.0.0/0

# Add tag to instance
gcloud compute instances add-tags ai-core-instance \
  --zone=us-central1-a \
  --tags=temporal-server
```

### Issue: Workers not connecting to Temporal

**Solution:** Check TEMPORAL_HOST environment variable

```bash
# On Compute Engine instance
docker-compose -f deployment/docker-compose.yml logs temporal-worker

# Should see: "Connected to Temporal: temporal:7233"
```

### Issue: Burst workers can't reach Temporal Server

**Solution:** Verify external IP and firewall

```bash
# Test from local machine
telnet YOUR_EXTERNAL_IP 7233

# Should connect successfully
```

### Issue: Out of memory

**Solution:** Upgrade instance or reduce worker concurrency

```bash
# Upgrade to larger instance
gcloud compute instances stop ai-core-instance --zone=us-central1-a
gcloud compute instances set-machine-type ai-core-instance \
  --machine-type=e2-standard-2 \
  --zone=us-central1-a
gcloud compute instances start ai-core-instance --zone=us-central1-a
```

## Cost Optimization

### Current Setup Costs

| Component | Monthly Cost |
|-----------|-------------|
| Compute Engine (e2-medium) | $25-40 |
| Cloud Run Jobs (10 executions/day) | $10-20 |
| Cloud Storage (minimal) | $1-5 |
| **Total** | **$36-65/mo** |

### Cost Reduction Tips

1. **Use Preemptible/Spot Instance** (80% cheaper)
   ```bash
   --provisioning-model=SPOT
   ```

2. **Schedule Instance Downtime** (stop at night)
   ```bash
   gcloud compute instances stop ai-core-instance --zone=us-central1-a
   ```

3. **Reduce Base Workers** (if low traffic)
   ```bash
   docker-compose up -d --scale temporal-worker=1
   ```

## Scaling Guide

### Scenario: 100 concurrent companies × 1000 invoices each

**Setup:**
- Compute Engine: e2-standard-2 (2 vCPU, 8GB) - $50/mo
- Base Workers: 3 instances
- Burst Workers: Trigger 50 Cloud Run Jobs when queue depth > 1000

**Capacity:**
- 3 base workers × 10 concurrent = 30 activities baseline
- 50 burst workers × 20 concurrent = 1000 activities during peak
- **Total: 1030 concurrent activities**

**Cost:**
- Base: $50/mo
- Burst: $2 per batch × 10 batches/day = $600/mo
- **Total: $650/mo for high throughput**

## Next Steps

1. ✅ Deploy to Compute Engine
2. ✅ Test basic workflow execution
3. ✅ Deploy Cloud Run burst workers
4. 🔲 Add authentication to API
5. 🔲 Setup monitoring and alerts
6. 🔲 Configure automated burst worker triggers
7. 🔲 Implement additional task types (beyond gdt_invoice_import)

## Support

For issues or questions:
- Check Temporal Web UI for workflow errors
- View logs: `docker-compose logs -f`
- Temporal docs: https://docs.temporal.io
