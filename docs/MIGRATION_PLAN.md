# Migration Plan: Legacy GDT Service → Temporal.io Architecture

## Executive Summary

This plan outlines the migration from the legacy `TaxCrawlerServiceV2` to the new Temporal.io architecture, enabling **maximum scalability** for processing hundreds of companies with multiple invoice flows concurrently.

**Key Benefits:**
- **Horizontal Scalability**: Scale to 100+ concurrent companies (vs. single-threaded legacy)
- **Fault Tolerance**: Automatic retries, state preservation, failure recovery
- **Rate Limiting**: Intelligent backoff across all companies/flows
- **Progress Tracking**: Real-time visibility into all running tasks
- **Cost Efficiency**: Burst workers scale automatically with demand

---

## 1. Architecture Comparison

### Legacy Architecture (gdt_invoices_service.py)
```
FastAPI Endpoint
    ↓
TaxCrawlerServiceV2.extract_invoices_v2()
    ↓
For each company:
    ↓ (Sequential)
    APIGDTAuthService.login()
    ↓
    For each flow:
        ↓ (Sequential)
        DirectAPICrawler.list_invoices_v2()
        ↓
        Upload to GCS
```

**Limitations:**
- Sequential processing (one company at a time)
- No automatic retry on failures
- No rate limit coordination
- State lost on restart
- Hard to monitor progress

### New Temporal Architecture
```
FastAPI Endpoint
    ↓
Temporal Workflow (per company)
    ↓
GdtInvoiceImportWorkflow.run()
    ↓
    ├─ Login Activity (retry automatically)
    ↓
    ├─ Discover Invoices (all flows parallel)
    │   ├─ Flow: ban_ra_dien_tu
    │   ├─ Flow: ban_ra_may_tinh_tien
    │   ├─ Flow: mua_vao_dien_tu
    │   └─ Flow: mua_vao_may_tinh_tien
    ↓
    └─ Fetch Invoices (max 10 concurrent per company)
        ├─ Invoice 1 (retry on 429)
        ├─ Invoice 2 (retry on 429)
        └─ Invoice N
    ↓
    Upload to GCS Activity
```

**Advantages:**
- 100+ companies processed in parallel
- Automatic retry with exponential backoff
- Shared rate limit state across all activities
- Durable state (survives restarts)
- Real-time progress queries
- Horizontal scaling with burst workers

---

## 2. Component Mapping

### Legacy → Temporal Activities

| Legacy Component | Temporal Activity | File Path |
|-----------------|-------------------|-----------|
| `APIGDTAuthService.login()` | `login_to_gdt()` | `temporal_app/activities/gdt_auth.py` |
| `APIGDTAuthService.get_session()` | Returned by login activity | (GdtSession model) |
| `DirectAPICrawler.list_invoices_v2()` | `discover_invoices()` | `temporal_app/activities/gdt_discovery.py` |
| `DirectAPICrawler._get_invoice_detail()` | `fetch_invoice()` | `temporal_app/activities/gdt_fetch.py` |
| GCS upload logic | `upload_to_gcs()` | `temporal_app/activities/gcs_upload.py` (NEW) |
| `FlowsConverter` | Workflow input params | (Config in workflow) |

### Legacy → Temporal Models

| Legacy Model | Temporal Model | Changes |
|-------------|----------------|---------|
| `GdtLoginRequest` | `GdtLoginRequest` | ✅ Already exists |
| `GdtSession` | `GdtSession` | ✅ Already exists |
| Invoice dict | `GdtInvoice` | ✅ Already exists |
| N/A | `InvoiceFetchResult` | 🆕 New (activity result) |

### Legacy → Temporal Workflow

| Legacy Method | Temporal Workflow | Changes |
|--------------|-------------------|---------|
| `extract_invoices_v2()` | `GdtInvoiceImportWorkflow.run()` | Split into activities |
| Sequential flow loop | Parallel flow discovery | Use `asyncio.gather()` |
| Sequential company loop | Multiple workflows | Start one workflow per company |

---

## 3. Migration Strategy

### Phase 1: Enhance Activities (Week 1)

#### 3.1 Replace Mock Activities with Real Implementation

**File: `temporal_app/activities/gdt_auth.py`**

Replace mock with real `APIGDTAuthService` logic:

```python
from temporal_app.services.gdt_api_service import APIGDTAuthService

@activity.defn
async def login_to_gdt(request: GdtLoginRequest) -> GdtSession:
    """Real GDT login using APIGDTAuthService."""
    activity.logger.info(f"Logging in to GDT for company {request.company_id}")

    auth_service = APIGDTAuthService(
        base_url=settings.gdt_base_url,
        username=request.username,
        password=request.password,
    )

    try:
        session_data = await auth_service.login()

        return GdtSession(
            company_id=request.company_id,
            session_id=session_data["session_id"],
            access_token=session_data["access_token"],
            cookies=session_data["cookies"],
            expires_at=session_data["expires_at"],
        )
    except Exception as e:
        activity.logger.error(f"Login failed: {str(e)}")
        raise
```

**File: `temporal_app/activities/gdt_discovery.py`**

Replace mock with real `DirectAPICrawler` logic:

```python
from temporal_app.services.direct_crawler import DirectAPICrawler

# Flow configuration from legacy FlowsConverter
FLOW_CONFIGS = {
    "ban_ra_dien_tu": {
        "code": "ban_ra_dien_tu",
        "name": "Bán ra điện tử",
        "direction": "OUT",
        "type": "ELECTRONIC",
    },
    "ban_ra_may_tinh_tien": {
        "code": "ban_ra_may_tinh_tien",
        "name": "Bán ra máy tính tiền",
        "direction": "OUT",
        "type": "CASH_REGISTER",
    },
    "mua_vao_dien_tu": {
        "code": "mua_vao_dien_tu",
        "name": "Mua vào điện tử",
        "direction": "IN",
        "type": "ELECTRONIC",
    },
    "mua_vao_may_tinh_tien": {
        "code": "mua_vao_may_tinh_tien",
        "name": "Mua vào máy tính tiền",
        "direction": "IN",
        "type": "CASH_REGISTER",
    },
}

@activity.defn
async def discover_invoices(
    session: GdtSession,
    date_range_start: str,
    date_range_end: str,
    flows: list[str],  # NEW: list of flow codes to process
) -> list[GdtInvoice]:
    """Discover invoices from GDT for all specified flows."""
    activity.logger.info(
        f"Discovering invoices for company {session.company_id} "
        f"from {date_range_start} to {date_range_end} "
        f"for flows: {flows}"
    )

    crawler = DirectAPICrawler(session=session)
    all_invoices = []

    # Process all flows in parallel (like legacy code but concurrent)
    async def discover_flow(flow_code: str) -> list[GdtInvoice]:
        flow_config = FLOW_CONFIGS[flow_code]
        activity.logger.info(f"Processing flow: {flow_config['name']}")

        invoices = await crawler.list_invoices_v2(
            date_start=date_range_start,
            date_end=date_range_end,
            direction=flow_config["direction"],
            invoice_type=flow_config["type"],
        )

        activity.logger.info(f"Found {len(invoices)} invoices for flow {flow_code}")
        return invoices

    # Execute all flows in parallel
    flow_results = await asyncio.gather(
        *[discover_flow(flow) for flow in flows],
        return_exceptions=True,
    )

    # Combine results
    for result in flow_results:
        if isinstance(result, list):
            all_invoices.extend(result)
        else:
            activity.logger.error(f"Flow failed: {str(result)}")

    activity.logger.info(f"Total invoices discovered: {len(all_invoices)}")
    return all_invoices
```

**File: `temporal_app/activities/gdt_fetch.py`**

Replace mock with real invoice fetching:

```python
from temporal_app.services.direct_crawler import DirectAPICrawler

@activity.defn
async def fetch_invoice(invoice: GdtInvoice, session: GdtSession) -> InvoiceFetchResult:
    """Fetch full invoice details from GDT with rate limiting."""

    # Check rate limit before making request
    if not await check_rate_limit(session.company_id):
        backoff_until = _rate_limit_state.get(session.company_id)
        remaining = (backoff_until - datetime.now()).total_seconds()
        raise RateLimitError(
            f"Rate limited for company {session.company_id}, "
            f"backing off for {remaining:.1f}s"
        )

    crawler = DirectAPICrawler(session=session)

    try:
        # Real API call to get invoice details
        invoice_data = await crawler.get_invoice_detail(invoice.invoice_id)

        return InvoiceFetchResult(
            invoice_id=invoice.invoice_id,
            success=True,
            data=invoice_data,
        )

    except RateLimitError as e:
        # Set backoff for this company (30-60s depending on header)
        await set_rate_limit_backoff(session.company_id, backoff_seconds=30)
        raise

    except Exception as e:
        activity.logger.error(f"Failed to fetch invoice {invoice.invoice_id}: {str(e)}")
        return InvoiceFetchResult(
            invoice_id=invoice.invoice_id,
            success=False,
            error=str(e),
        )
```

#### 3.2 Add GCS Upload Activity

**File: `temporal_app/activities/gcs_upload.py` (NEW)**

```python
"""GCS upload activity for invoice data."""

from datetime import timedelta

from google.cloud import storage
from temporalio import activity

from temporal_app.models import InvoiceFetchResult


@activity.defn
async def upload_invoices_to_gcs(
    company_id: str,
    invoices: list[InvoiceFetchResult],
    bucket_name: str,
    date_range_start: str,
    date_range_end: str,
) -> dict:
    """
    Upload fetched invoices to GCS.

    Args:
        company_id: Company identifier
        invoices: List of successfully fetched invoices
        bucket_name: GCS bucket name
        date_range_start: Start date (for file path)
        date_range_end: End date (for file path)

    Returns:
        dict: Upload result with GCS path and count
    """
    activity.logger.info(f"Uploading {len(invoices)} invoices to GCS for {company_id}")

    try:
        # Filter successful invoices
        successful_invoices = [inv for inv in invoices if inv.success]

        if not successful_invoices:
            activity.logger.warning("No successful invoices to upload")
            return {"success": True, "count": 0, "gcs_path": None}

        # Initialize GCS client
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)

        # Generate file path (similar to legacy code)
        file_path = (
            f"gdt_invoices/{company_id}/"
            f"{date_range_start}_to_{date_range_end}/"
            f"invoices_{len(successful_invoices)}.json"
        )

        # Prepare data for upload
        invoice_data = {
            "company_id": company_id,
            "date_range": {
                "start": date_range_start,
                "end": date_range_end,
            },
            "total_count": len(successful_invoices),
            "invoices": [inv.data for inv in successful_invoices],
        }

        # Upload to GCS
        blob = bucket.blob(file_path)
        blob.upload_from_string(
            data=json.dumps(invoice_data, ensure_ascii=False, indent=2),
            content_type="application/json",
        )

        gcs_uri = f"gs://{bucket_name}/{file_path}"
        activity.logger.info(f"✅ Uploaded to {gcs_uri}")

        return {
            "success": True,
            "count": len(successful_invoices),
            "gcs_path": gcs_uri,
        }

    except Exception as e:
        activity.logger.error(f"GCS upload failed: {str(e)}")
        raise
```

### Phase 2: Enhance Workflow (Week 2)

#### 2.1 Add Flow Support to Workflow

**File: `temporal_app/workflows/gdt_invoice_import.py`**

Update workflow to support multiple flows:

```python
@workflow.run
async def run(self, params: dict) -> dict:
    """
    Main workflow execution with multi-flow support.

    Args:
        params: {
            "company_id": str,
            "company_name": str,
            "credentials": {"username": str, "password": str},
            "date_range_start": str,
            "date_range_end": str,
            "flows": list[str],  # NEW: ["ban_ra_dien_tu", "mua_vao_dien_tu", ...]
            "gcs_bucket": str,  # NEW: GCS bucket for upload
        }

    Returns:
        dict: Task result with statistics and GCS path
    """
    workflow.logger.info(
        f"Starting GDT invoice import for {params['company_id']} "
        f"from {params['date_range_start']} to {params['date_range_end']} "
        f"for flows: {params['flows']}"
    )

    try:
        # Step 1: Login to GDT portal
        self.session = await self._login(params)

        # Step 2: Discover all invoices (all flows in parallel)
        self.invoices = await self._discover(params)
        self.total_invoices = len(self.invoices)

        workflow.logger.info(f"Found {self.total_invoices} invoices to import")

        # Step 3: Fetch all invoices in parallel (with concurrency limit)
        await self._fetch_all_invoices()

        # Step 4: Upload to GCS
        upload_result = await self._upload_to_gcs(params)

        # Step 5: Return result
        return {
            "status": "completed",
            "company_id": params["company_id"],
            "total_invoices": self.total_invoices,
            "completed_invoices": self.completed_invoices,
            "failed_invoices": self.failed_invoices,
            "success_rate": (
                round(self.completed_invoices / self.total_invoices * 100, 2)
                if self.total_invoices > 0
                else 0.0
            ),
            "gcs_path": upload_result["gcs_path"],
            "gcs_count": upload_result["count"],
        }

    except Exception as e:
        workflow.logger.error(f"Workflow failed: {str(e)}")
        return {
            "status": "failed",
            "error": str(e),
            "completed_invoices": self.completed_invoices,
            "total_invoices": self.total_invoices,
        }

async def _discover(self, params: dict) -> list[GdtInvoice]:
    """Discover all invoices in date range for all flows."""
    invoices = await workflow.execute_activity(
        discover_invoices,
        args=[
            self.session,
            params["date_range_start"],
            params["date_range_end"],
            params["flows"],  # NEW: pass flows to activity
        ],
        start_to_close_timeout=timedelta(minutes=15),
        heartbeat_timeout=timedelta(minutes=2),
        retry_policy=RetryPolicy(
            initial_interval=timedelta(seconds=10),
            maximum_interval=timedelta(minutes=2),
            maximum_attempts=3,
        ),
    )

    workflow.logger.info(f"✅ Discovered {len(invoices)} invoices")
    return invoices

async def _upload_to_gcs(self, params: dict) -> dict:
    """Upload fetched invoices to GCS."""
    upload_result = await workflow.execute_activity(
        upload_invoices_to_gcs,
        args=[
            params["company_id"],
            self.results,
            params["gcs_bucket"],
            params["date_range_start"],
            params["date_range_end"],
        ],
        start_to_close_timeout=timedelta(minutes=10),
        retry_policy=RetryPolicy(
            initial_interval=timedelta(seconds=5),
            maximum_interval=timedelta(minutes=2),
            maximum_attempts=3,
        ),
    )

    workflow.logger.info(f"✅ Uploaded {upload_result['count']} invoices to GCS")
    return upload_result
```

#### 2.2 Update API Models

**File: `app/models.py`**

Update parameters to support flows:

```python
class GdtInvoiceImportParams(BaseModel):
    """Parameters for GDT invoice import task."""

    company_id: str = Field(..., description="Company identifier")
    company_name: str = Field(..., description="Company name")
    credentials: dict[str, str] = Field(..., description="GDT credentials (username/password)")
    date_range_start: str = Field(..., description="Start date (YYYY-MM-DD)")
    date_range_end: str = Field(..., description="End date (YYYY-MM-DD)")

    # NEW: Flow configuration
    flows: list[str] = Field(
        default=["ban_ra_dien_tu", "mua_vao_dien_tu"],
        description="Invoice flows to process",
    )

    # NEW: GCS configuration
    gcs_bucket: str = Field(
        default="your-gcs-bucket",
        description="GCS bucket for invoice upload",
    )

    @field_validator("flows")
    def validate_flows(cls, v):
        """Validate flow codes."""
        valid_flows = {
            "ban_ra_dien_tu",
            "ban_ra_may_tinh_tien",
            "mua_vao_dien_tu",
            "mua_vao_may_tinh_tien",
        }
        invalid_flows = set(v) - valid_flows
        if invalid_flows:
            raise ValueError(f"Invalid flows: {invalid_flows}")
        return v
```

### Phase 3: Migration Script (Week 2)

Create a script to migrate from legacy endpoint to Temporal:

**File: `scripts/migrate_legacy_crawls.py` (NEW)**

```python
"""
Migration script to convert legacy crawl jobs to Temporal workflows.

Usage:
    python scripts/migrate_legacy_crawls.py --companies companies.json
"""

import argparse
import asyncio
import json
from datetime import datetime

import httpx


async def start_temporal_workflow(company: dict, api_base_url: str) -> dict:
    """Start a Temporal workflow for a company."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{api_base_url}/api/tasks/start",
            json={
                "task_type": "gdt_invoice_import",
                "task_params": {
                    "company_id": company["company_id"],
                    "company_name": company["company_name"],
                    "credentials": {
                        "username": company["username"],
                        "password": company["password"],
                    },
                    "date_range_start": company["date_range_start"],
                    "date_range_end": company["date_range_end"],
                    "flows": company.get("flows", [
                        "ban_ra_dien_tu",
                        "mua_vao_dien_tu",
                    ]),
                    "gcs_bucket": company.get("gcs_bucket", "your-gcs-bucket"),
                },
            },
        )
        response.raise_for_status()
        return response.json()


async def migrate_companies(companies_file: str, api_base_url: str):
    """Migrate all companies to Temporal workflows."""
    with open(companies_file, "r") as f:
        companies = json.load(f)

    print(f"Migrating {len(companies)} companies to Temporal...")

    # Start workflows for all companies (in parallel)
    tasks = [
        start_temporal_workflow(company, api_base_url)
        for company in companies
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Print results
    successful = 0
    failed = 0

    for i, result in enumerate(results):
        company_id = companies[i]["company_id"]
        if isinstance(result, Exception):
            print(f"❌ {company_id}: {str(result)}")
            failed += 1
        else:
            print(f"✅ {company_id}: Workflow {result['workflow_id']} started")
            successful += 1

    print(f"\nMigration complete: {successful} succeeded, {failed} failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate legacy crawls to Temporal")
    parser.add_argument(
        "--companies",
        required=True,
        help="JSON file with company credentials",
    )
    parser.add_argument(
        "--api-url",
        default="http://localhost:8000",
        help="FastAPI base URL",
    )

    args = parser.parse_args()
    asyncio.run(migrate_companies(args.companies, args.api_url))
```

**Example companies.json:**

```json
[
  {
    "company_id": "COMPANY_001",
    "company_name": "ABC Corporation",
    "username": "user1",
    "password": "pass1",
    "date_range_start": "2024-01-01",
    "date_range_end": "2024-01-31",
    "flows": ["ban_ra_dien_tu", "mua_vao_dien_tu"],
    "gcs_bucket": "prod-gdt-invoices"
  },
  {
    "company_id": "COMPANY_002",
    "company_name": "XYZ Ltd",
    "username": "user2",
    "password": "pass2",
    "date_range_start": "2024-01-01",
    "date_range_end": "2024-01-31",
    "flows": ["ban_ra_dien_tu", "ban_ra_may_tinh_tien"],
    "gcs_bucket": "prod-gdt-invoices"
  }
]
```

---

## 4. Scalability Configuration

### 4.1 Worker Concurrency Settings

**File: `temporal_app/worker.py`**

Update for maximum scalability:

```python
# Calculate concurrency based on worker type
if self.is_burst_mode:
    # Burst workers: High concurrency (short-lived)
    concurrency_settings = {
        "workflows": 50,  # 50 concurrent workflows per burst worker
        "activities": 200,  # 200 concurrent activities per burst worker
    }
else:
    # Base workers: Moderate concurrency (always-on)
    concurrency_settings = {
        "workflows": 20,  # 20 concurrent workflows per base worker
        "activities": 100,  # 100 concurrent activities per base worker
    }
```

**Scalability Math:**
- **Base workers (2)**: 2 × 20 workflows = 40 concurrent companies
- **Burst workers (100)**: 100 × 50 workflows = 5000 concurrent companies
- **Total capacity**: 5040 concurrent companies processing simultaneously

### 4.2 Rate Limiting Configuration

**File: `temporal_app/activities/gdt_fetch.py`**

Upgrade to Redis for shared rate limiting across workers:

```python
import redis.asyncio as redis

# Use Redis instead of in-memory dict
_redis_client = None

async def get_redis_client() -> redis.Redis:
    """Get shared Redis client."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379"),
            decode_responses=True,
        )
    return _redis_client

async def check_rate_limit(company_id: str) -> bool:
    """Check if company is rate limited (Redis-backed)."""
    client = await get_redis_client()

    backoff_key = f"rate_limit:{company_id}"
    backoff_until = await client.get(backoff_key)

    if backoff_until:
        backoff_time = datetime.fromisoformat(backoff_until)
        if datetime.now() < backoff_time:
            return False

    return True

async def set_rate_limit_backoff(company_id: str, backoff_seconds: int):
    """Set rate limit backoff for company (Redis-backed)."""
    client = await get_redis_client()

    backoff_until = datetime.now() + timedelta(seconds=backoff_seconds)
    backoff_key = f"rate_limit:{company_id}"

    await client.setex(
        backoff_key,
        backoff_seconds,
        backoff_until.isoformat(),
    )
```

**Add Redis to docker-compose.yml:**

```yaml
redis:
  image: redis:7-alpine
  ports:
    - "6379:6379"
  healthcheck:
    test: ["CMD", "redis-cli", "ping"]
    interval: 5s
    timeout: 3s
    retries: 5
```

### 4.3 Deployment Configuration

**File: `deployment/deploy-burst.sh`**

Update for maximum burst capacity:

```bash
# Deploy up to 100 burst workers
MAX_BURST_WORKERS=100

# Calculate based on queue depth
QUEUE_DEPTH=$(get_queue_depth)
REQUIRED_WORKERS=$((QUEUE_DEPTH / 50))  # 50 workflows per worker

# Cap at max
BURST_WORKERS=$((REQUIRED_WORKERS > MAX_BURST_WORKERS ? MAX_BURST_WORKERS : REQUIRED_WORKERS))

echo "Deploying $BURST_WORKERS burst workers..."
```

---

## 5. Testing Strategy

### 5.1 Unit Tests

Create tests for each activity:

```bash
# Test login activity
pytest tests/activities/test_gdt_auth.py

# Test discovery activity
pytest tests/activities/test_gdt_discovery.py

# Test fetch activity with rate limiting
pytest tests/activities/test_gdt_fetch.py

# Test GCS upload activity
pytest tests/activities/test_gcs_upload.py
```

### 5.2 Integration Tests

Test full workflow with real GDT API:

```python
# tests/workflows/test_gdt_invoice_import_integration.py

async def test_full_workflow_single_company():
    """Test full workflow for one company."""
    result = await start_workflow(
        company_id="TEST_COMPANY_001",
        flows=["ban_ra_dien_tu"],
        date_range_start="2024-01-01",
        date_range_end="2024-01-31",
    )

    assert result["status"] == "completed"
    assert result["total_invoices"] > 0
    assert result["gcs_path"] is not None

async def test_full_workflow_100_companies():
    """Test scalability with 100 concurrent companies."""
    workflow_ids = []

    # Start 100 workflows
    for i in range(100):
        result = await start_workflow(
            company_id=f"TEST_COMPANY_{i:03d}",
            flows=["ban_ra_dien_tu", "mua_vao_dien_tu"],
            date_range_start="2024-01-01",
            date_range_end="2024-01-31",
        )
        workflow_ids.append(result["workflow_id"])

    # Wait for all to complete
    results = await asyncio.gather(
        *[wait_for_workflow(wf_id) for wf_id in workflow_ids]
    )

    # Verify all succeeded
    successful = sum(1 for r in results if r["status"] == "completed")
    assert successful >= 95  # 95%+ success rate
```

### 5.3 Load Testing

Use Locust or k6 to test scalability:

```python
# tests/load/locustfile.py

from locust import HttpUser, task, between

class TemporalLoadTest(HttpUser):
    wait_time = between(1, 3)

    @task
    def start_invoice_import(self):
        self.client.post("/api/tasks/start", json={
            "task_type": "gdt_invoice_import",
            "task_params": {
                "company_id": f"LOAD_TEST_{self.user_id}",
                "company_name": "Load Test Company",
                "credentials": {"username": "test", "password": "test"},
                "date_range_start": "2024-01-01",
                "date_range_end": "2024-01-31",
                "flows": ["ban_ra_dien_tu"],
                "gcs_bucket": "load-test-bucket",
            },
        })
```

Run load test:

```bash
# Test with 100 concurrent users
locust -f tests/load/locustfile.py --users 100 --spawn-rate 10
```

---

## 6. Migration Timeline

| Phase | Duration | Tasks | Deliverables |
|-------|----------|-------|--------------|
| **Phase 1: Setup** | 2 days | - Copy legacy code to `temporal_app/services/`<br>- Set up Redis<br>- Update dependencies | - Working services layer<br>- Redis running |
| **Phase 2: Activities** | 3 days | - Implement real `login_to_gdt()`<br>- Implement real `discover_invoices()`<br>- Implement real `fetch_invoice()`<br>- Implement `upload_to_gcs()` | - All activities working<br>- Unit tests passing |
| **Phase 3: Workflow** | 2 days | - Update workflow for flows<br>- Add GCS upload step<br>- Update API models | - Workflow working end-to-end |
| **Phase 4: Testing** | 3 days | - Unit tests<br>- Integration tests<br>- Load testing | - Test suite passing<br>- Load test results |
| **Phase 5: Deploy** | 2 days | - Deploy to GCP<br>- Configure burst workers<br>- Monitor production | - Production deployment<br>- Monitoring dashboard |
| **Phase 6: Migration** | 3 days | - Migrate 10 companies (pilot)<br>- Migrate 100 companies<br>- Migrate all companies | - All companies migrated |

**Total Duration: 15 days (3 weeks)**

---

## 7. Rollback Plan

If migration fails, rollback to legacy system:

1. **Keep legacy endpoint active** during migration (parallel run)
2. **Monitor both systems** for 1 week
3. **Compare results** (invoice counts, GCS uploads)
4. **Rollback if needed:**
   ```bash
   # Stop Temporal workers
   kubectl scale deployment temporal-worker --replicas=0

   # Route traffic back to legacy endpoint
   kubectl patch service api --type='json' -p='[{"op": "replace", "path": "/spec/selector/app", "value":"legacy-api"}]'
   ```

---

## 8. Success Metrics

Track these metrics to measure migration success:

| Metric | Target | Current (Legacy) |
|--------|--------|------------------|
| **Throughput** | 100 companies/hour | 10 companies/hour |
| **Latency (per company)** | <5 minutes | 10-30 minutes |
| **Success Rate** | >95% | ~80% |
| **Cost** | $100/month | $200/month |
| **Concurrent Companies** | 100+ | 1 (sequential) |
| **Worker Utilization** | 70-80% | 20-30% |

---

## 9. Next Steps

1. **Review this plan** with your team
2. **Copy legacy services** to `temporal_app/services/`
3. **Set up Redis** in docker-compose.yml
4. **Start Phase 1** (implement real activities)
5. **Run tests** continuously
6. **Deploy to staging** for pilot testing

Let me know when you're ready to start implementing!
