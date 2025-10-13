# AI Core Polling to Webhook Migration Analysis

**Date**: 2025-10-13
**Target System**: b4b-api → finizi-ai-core integration
**Current Status**: Active polling implementation
**Proposed Target**: Webhook-based status updates

---

## Executive Summary

The current implementation uses **client-side polling** to check AI Core task status, resulting in increased network traffic, delayed status updates, and inefficient resource usage. This analysis identifies the gaps and proposes a migration plan to implement **server-side webhooks** for real-time status updates from AI Core.

---

## 1. Current Polling Implementation

### 1.1 Architecture Overview

```
┌─────────────┐                    ┌──────────────┐
│   B4B API   │                    │  AI Core     │
│             │                    │  Service     │
│  Celery     │  1. Start Task     │  (Port 8051) │
│  Worker     │──────────────────▶│              │
│             │                    │  Processing  │
│             │  2. Poll Status    │     ...      │
│             │◀─────────────────▶│              │
│             │  (Every 2-30s)     │              │
│             │                    │              │
│             │  3. Get Result     │              │
│             │◀─────────────────▶│              │
└─────────────┘                    └──────────────┘
    Polling Loop with Exponential Backoff
```

### 1.2 Key Implementation Files

#### **AI Core Client** (`app/services/ai_core_client.py`)
- **Line 101-206**: `start_gdtvn_extraction()` - Initiates AI Core tasks
- **Line 208-253**: `get_extraction_status()` - Polls for task status with cache busting
- **Line 255-303**: `get_extraction_result()` - Fetches final results
- **Key Pattern**: Synchronous HTTP requests with 30-second timeout

#### **Polling Configuration** (`app/core/polling_config.py`)
```python
POLLING_CONFIGS = {
    'ai_core': PollingConfig(
        initial_interval=2.0,      # Start with 2s
        max_interval=30.0,         # Cap at 30s
        backoff_factor=1.5,        # Exponential backoff
        max_duration=600,          # 10 minutes max
        soft_timeout=540,          # 9 minutes soft limit
        jitter=True                # Prevent thundering herd
    )
}
```

#### **Classification Task** (`app/queue/tasks/ai_classification.py`)
- **Line 352-413**: `_classify_invoice_via_api()` - Calls AI Core with progress updates
- **Line 480-942**: `classify_invoice_task()` - Main Celery task with polling logic
- **Pattern**: Progress updates at 10%, 20%, 30%, 50%, 90%, 100%

#### **Journal Service** (`app/services/journal/ai_core_journal_service.py`)
- **Line 680-794**: `_call_ai_core_journal_endpoint()` - Single synchronous call
- **Pattern**: Waits for entire processing with configurable timeout

#### **External Task Recovery** (`app/services/external_task_recovery_service.py`)
- **Line 75-112**: `get_abandoned_tasks()` - Finds timed-out tasks for recovery
- **Line 114-150**: `mark_checking()` - Prevents concurrent recovery checks
- **Pattern**: Background recovery service polls abandoned tasks every few minutes

### 1.3 Polling Flow Sequence

```
1. Task Initiation
   └─ B4B API creates QueueJob
   └─ Celery worker calls AI Core API
   └─ AI Core returns task_id
   └─ ExternalTaskTracker created (for recovery)

2. Polling Loop (in Celery worker)
   └─ Sleep 2s → Check status (HTTP GET)
   └─ Sleep 3s → Check status
   └─ Sleep 4.5s → Check status
   └─ Sleep 6.75s → Check status
   └─ ... (exponential backoff)
   └─ Until: status = "completed" | "failed" | timeout

3. Result Retrieval
   └─ Fetch final result data
   └─ Update QueueJob status
   └─ Update ExternalTaskTracker
   └─ Send notification to user

4. Timeout/Recovery (if polling times out)
   └─ QueueJob marked as TIMEOUT
   └─ ExternalTaskTracker remains PENDING
   └─ Background recovery task periodically checks
   └─ If recovered: Update QueueJob with results
```

### 1.4 Current Limitations

#### **Performance Issues**
- **Network Overhead**: 50-100+ HTTP requests per task (10-minute task = ~30 polls)
- **Latency**: 2-30 second delay between completion and detection
- **Resource Waste**: CPU cycles spent sleeping and polling
- **Scalability**: N concurrent tasks = N polling loops

#### **Operational Issues**
- **Timeout Recovery**: Separate background job needed for abandoned tasks
- **Progress Granularity**: Limited to client-side polling intervals
- **Error Detection Delay**: Failures not detected until next poll
- **Concurrent Polling**: Risk of thundering herd without jitter

#### **Code Complexity**
- **Polling Logic**: Spread across multiple files (client, config, tasks)
- **Recovery System**: Additional complexity for timeout handling
- **State Management**: ExternalTaskTracker model needed for recovery

---

## 2. Proposed Webhook Architecture

### 2.1 Target Architecture

```
┌─────────────┐                    ┌──────────────┐
│   B4B API   │                    │  AI Core     │
│             │  1. Start Task     │  Service     │
│  Celery     │───────────────────▶│              │
│  Worker     │                    │              │
│             │                    │  Processing  │
│             │                    │     ...      │
│             │  2. Status Updates │              │
│  Webhook    │◀───────────────────│  (Real-time) │
│  Endpoint   │  (POST callback)   │              │
│             │                    │              │
│             │  3. Completion     │              │
│             │◀───────────────────│  Callback    │
└─────────────┘                    └──────────────┘
    Push-based updates (No polling)
```

### 2.2 Webhook Event Types

```python
# AI Core will POST to: /api/v1/internal/webhooks/ai-core

# Event: task_started
{
  "event_id": "evt_abc123xyz",
  "event_name": "task_started",
  "event_payload": {
    "task_id": "uuid",
    "timestamp": "2025-10-13T10:00:00Z",
    "service": "ai_core"
  }
}

# Event: task_progress
{
  "event_id": "evt_def456uvw",
  "event_name": "task_progress",
  "event_payload": {
    "task_id": "uuid",
    "progress": 45,
    "message": "Processing line items...",
    "current_step": "classification",
    "timestamp": "2025-10-13T10:01:30Z"
  }
}

# Event: task_completed
{
  "event_id": "evt_ghi789rst",
  "event_name": "task_completed",
  "event_payload": {
    "task_id": "uuid",
    "status": "completed",
    "result": { /* full result data */ },
    "processing_time_ms": 95000,
    "timestamp": "2025-10-13T10:02:15Z"
  }
}

# Event: task_failed
{
  "event_id": "evt_jkl012mno",
  "event_name": "task_failed",
  "event_payload": {
    "task_id": "uuid",
    "status": "failed",
    "error": {
      "code": "CLASSIFICATION_ERROR",
      "message": "Failed to classify line item 3"
    },
    "timestamp": "2025-10-13T10:01:45Z"
  }
}
```

### 2.2.1 Webhook Request Headers (HTTP Message Signatures)

```http
POST /api/v1/internal/webhooks/ai-core HTTP/1.1
Host: be.dev.finizi.app
Content-Type: application/json
User-Agent: Finizi-AI-Core/1.0
Date: Mon, 13 Oct 2025 10:02:15 GMT
Content-Digest: sha-256=:Q0b7...base64...==:
Signature-Input: sig1=("@method" "@target-uri" "content-digest" "date");alg="hmac-sha256";created=1739427735;expires=1739428035;keyid="ai-core"
Signature: sig1=:zVq+...base64-signature.../w==:
```

### 2.3 Benefits of Webhook Approach

#### **Performance Improvements**
- ✅ **Zero Polling Overhead**: No repeated HTTP requests
- ✅ **Instant Updates**: Real-time status changes (< 1 second latency)
- ✅ **Reduced Network Traffic**: 3-5 webhooks vs 50-100 polls
- ✅ **Better Scalability**: O(1) per task regardless of duration

#### **Operational Improvements**
- ✅ **No Timeout Recovery Needed**: Webhooks deliver eventually
- ✅ **Granular Progress**: AI Core can push fine-grained updates
- ✅ **Immediate Error Detection**: Failures reported instantly
- ✅ **Simpler Architecture**: Eliminate polling config, recovery service

#### **Developer Experience**
- ✅ **Cleaner Code**: Event-driven vs polling loops
- ✅ **Easier Testing**: Simulate webhooks easily
- ✅ **Better Observability**: Clear audit trail of events

---

## 3. Gap Analysis

### 3.1 Missing Components in B4B API

#### **A. Webhook Endpoint** ❌ NOT IMPLEMENTED
**Required**: Public HTTP endpoint to receive AI Core callbacks

```python
# app/api/v1/internal/webhooks.py (NEW FILE)
from fastapi import APIRouter, Depends, Request
from app.schemas.webhook import AICorWebhookEvent
from app.services.webhook_service import WebhookService
from app.core.http_message_signatures import verify_http_message_signature

router = APIRouter(prefix="/internal/webhooks", tags=["webhooks"])

@router.post("/ai-core")
async def ai_core_webhook(
    request: Request,
    _: None = Depends(verify_http_message_signature),
    db: Session = Depends(get_db)
):
    """Receive webhook callbacks from AI Core service"""
    event: AICorWebhookEvent = await request.json()
    # 1. Signature verified by dependency
    # 2. Find ExternalTaskTracker by event["event_payload"]["task_id"]
    # 3. Update status based on event_name
    # 4. Update QueueJob progress
    # 5. Send WebSocket notification to user
    # 6. Return 200 OK
    pass
```

**Dependencies**:
- New API route file
- Webhook signature verification
- Event schema definitions

---

#### **B. Webhook Service** ❌ NOT IMPLEMENTED
**Required**: Business logic to process webhook events

```python
# app/services/webhook_service.py (NEW FILE)
class WebhookService:
    def process_ai_core_event(self, task_id: str, event: dict):
        """Process AI Core webhook event and update database"""
        # 1. Find tracker by external_task_id
        # 2. Update tracker status
        # 3. Update QueueJob progress/status
        # 4. Trigger notifications
        # 5. Handle result data for completion events
        pass
```

**Dependencies**:
- Integration with ExternalTaskTracker
- Integration with QueueJob updates
- Integration with notification service

---

#### **C. Webhook Security** ❌ NOT IMPLEMENTED
**Required**: HTTP Message Signatures (RFC 9421) verification

```python
# app/core/http_message_signatures.py (NEW FILE)
# Outline using a Python HTTP Message Signatures library
from fastapi import Request, HTTPException

def verify_http_message_signature(request: Request, secret_resolver) -> None:
    """Verify Signature-Input/Signature covering method, target-uri, content-digest, date.
    secret_resolver(keyid: str) -> bytes should return the shared secret for keyid.
    Raise HTTPException(401) on failure.
    """
    # Pseudocode (library-specific API may vary):
    # 1) Read headers: Signature-Input, Signature, Date, Content-Digest
    # 2) Resolve secret using keyid from Signature-Input
    # 3) Reconstruct the signature base per components
    # 4) Verify Content-Digest against raw body
    # 5) Verify signature using HMAC-SHA256 and created/expires window
    # If any step fails -> raise HTTPException(status_code=401)
    pass
```

**Dependencies**:
- Shared secret configuration (env variable)
- HTTP Message Signatures library (Python) for verification
- Signature verification on all webhook requests

```bash
# Install RFC 9421 HTTP Message Signatures library
pip install http-message-signatures
```

---

#### **D. Webhook Configuration** ❌ NOT IMPLEMENTED
**Required**: Environment configuration for webhook URLs

```bash
# .env additions
WEBHOOK_BASE_URL=https://be.dev.finizi.app
WEBHOOK_SECRET=<shared-secret-with-ai-core>
AI_CORE_WEBHOOK_ENABLED=true
```

**Dependencies**:
- Configuration model updates
- Dynamic webhook URL generation

---

#### **E. Idempotency Handling** ❌ NOT IMPLEMENTED
**Required**: Handle duplicate webhook deliveries

```python
# app/models/webhook_event_log.py (NEW FILE)
class WebhookEventLog(Base):
    """Log webhook events to handle duplicates"""
    __tablename__ = "webhook_event_logs"

    id = Column(UUID, primary_key=True)
    event_id = Column(String, unique=True, index=True)  # Unique event ID from AI Core
    task_id = Column(String, index=True)
    event_name = Column(String)  # e.g., "task_started", "task_completed"
    event_payload = Column(JSON)  # Full event payload
    processed_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
```

**Dependencies**:
- Database migration for new table
- Idempotency check in webhook handler

---

### 3.2 Required Changes in AI Core Service

#### **A. Webhook Client** ❌ NOT IMPLEMENTED IN AI CORE
**Required**: HTTP client to send webhook callbacks

```python
# finizi-ai-core/app/services/webhook_client.py (NEW FILE)
import httpx
import hmac
import hashlib

class WebhookClient:
    async def send_event(
        self,
        webhook_url: str,
        task_id: str,
        event: dict
    ):
        """Send webhook event with signature"""
        payload = json.dumps(event)
        signature = self._generate_signature(payload)

        async with httpx.AsyncClient() as client:
            await client.post(
                f"{webhook_url}",
                json=event,
                headers={"X-Signature": signature},
                timeout=10.0
            )

    def _generate_signature(self, payload: str) -> str:
        """Generate HMAC-SHA256 signature"""
        return hmac.new(
            self.secret.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()
```

---

#### **B. Task Status Hooks** ❌ NOT IMPLEMENTED IN AI CORE
**Required**: Trigger webhooks at key task lifecycle points

```python
# Modifications needed in AI Core task execution
async def process_classification_task(task_id, data):
    # Send task_started webhook
    await webhook_client.send_event(
        webhook_url=data.get("webhook_url"),
        task_id=task_id,
        event={
            "event_id": generate_event_id(),
            "event_name": "task_started",
            "event_payload": {
                "task_id": task_id,
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "service": "ai_core"
            }
        }
    )

    try:
        # Processing logic...

        # Send progress updates
        await webhook_client.send_event(
            ...,
            event={
                "event_id": generate_event_id(),
                "event_name": "task_progress",
                "event_payload": {
                    "task_id": task_id,
                    "progress": 50,
                    "message": "Processing...",
                    "timestamp": datetime.utcnow().isoformat() + "Z"
                }
            }
        )

        # Send completion
        await webhook_client.send_event(
            ...,
            event={
                "event_id": generate_event_id(),
                "event_name": "task_completed",
                "event_payload": {
                    "task_id": task_id,
                    "status": "completed",
                    "result": {...},
                    "timestamp": datetime.utcnow().isoformat() + "Z"
                }
            }
        )
    except Exception as e:
        # Send failure
        await webhook_client.send_event(
            ...,
            event={
                "event_id": generate_event_id(),
                "event_name": "task_failed",
                "event_payload": {
                    "task_id": task_id,
                    "status": "failed",
                    "error": {"code": "ERROR", "message": str(e)},
                    "timestamp": datetime.utcnow().isoformat() + "Z"
                }
            }
        )
```

---

#### **C. Retry Logic for Webhook Failures** ❌ NOT IMPLEMENTED
**Required**: Handle temporary B4B API unavailability

```python
# Retry strategy for webhook delivery
async def send_webhook_with_retry(url, event, max_retries=3):
    for attempt in range(max_retries):
        try:
            response = await http_client.post(url, json=event, timeout=10)
            if response.status_code == 200:
                return True
            elif response.status_code >= 500:
                # Server error - retry
                await asyncio.sleep(2 ** attempt)
                continue
            else:
                # Client error (4xx) - don't retry
                logger.error(f"Webhook rejected: {response.status_code}")
                return False
        except httpx.TimeoutException:
            await asyncio.sleep(2 ** attempt)

    logger.error(f"Webhook delivery failed after {max_retries} attempts")
    return False
```

---

#### **D. Webhook Configuration in Request** ❌ NOT IMPLEMENTED
**Required**: B4B API passes webhook URL when starting tasks

```python
# Modified AI Core request schema
{
  "request_metadata": {
    "request_id": "uuid",
    "webhook_url": "https://be.dev.finizi.app/api/v1/internal/webhooks/ai-core",  # NEW
    "webhook_events": ["progress", "completed", "failed"],  # NEW
    ...
  },
  "invoice_data": {...},
  ...
}
```

---

### 3.3 Database Schema Changes

#### **A. Add webhook_url to ExternalTaskTracker** ⚠️ NEEDS MIGRATION

```python
# Migration: add webhook tracking fields
class ExternalTaskTracker(Base):
    # Existing fields...

    # NEW: Webhook delivery tracking
    webhook_url = Column(String, nullable=True)
    webhook_delivered = Column(Boolean, default=False)
    webhook_delivery_attempts = Column(Integer, default=0)
    last_webhook_attempt = Column(DateTime, nullable=True)
    webhook_error = Column(String, nullable=True)
```

**Migration File**: `alembic/versions/xxxx_add_webhook_tracking.py`

---

#### **B. Create webhook_event_logs table** ⚠️ NEEDS MIGRATION

```sql
CREATE TABLE webhook_event_logs (
    id UUID PRIMARY KEY,
    event_id VARCHAR UNIQUE NOT NULL,  -- Unique event ID from AI Core
    task_id VARCHAR NOT NULL,
    event_name VARCHAR NOT NULL,  -- e.g., "task_started", "task_completed"
    event_payload JSONB NOT NULL,  -- Full event payload
    processed_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_webhook_event_task_id ON webhook_event_logs(task_id);
CREATE INDEX idx_webhook_event_name ON webhook_event_logs(event_name);
```

---

### 3.4 Testing Requirements

#### **Unit Tests** ❌ NOT IMPLEMENTED
```python
# tests/test_webhook_service.py
def test_process_completed_event():
    """Test processing completed webhook event"""
    pass

def test_verify_webhook_signature():
    """Test HMAC signature verification"""
    pass

def test_handle_duplicate_events():
    """Test idempotency handling"""
    pass
```

#### **Integration Tests** ❌ NOT IMPLEMENTED
```python
# tests/test_webhook_integration.py
async def test_webhook_endpoint_receives_event():
    """Test webhook endpoint receives and processes event"""
    pass

async def test_webhook_updates_queue_job():
    """Test webhook updates QueueJob status correctly"""
    pass
```

#### **E2E Tests** ❌ NOT IMPLEMENTED
- Simulate AI Core sending webhooks
- Verify end-to-end flow from task start to completion
- Test failure scenarios and retries

---

## 4. Migration Strategy

### 4.1 Phased Approach (Recommended)

#### **Phase 1: Dual Mode (Polling + Webhooks)** 🔄
**Duration**: 2-4 weeks
**Goal**: Run both systems in parallel for safe rollout

```python
# Configuration flag
AI_CORE_WEBHOOK_ENABLED = os.getenv("AI_CORE_WEBHOOK_ENABLED", "false") == "true"

# Modified task flow
if AI_CORE_WEBHOOK_ENABLED:
    # Pass webhook_url to AI Core
    # Disable polling loop
    # Wait for webhook callbacks
else:
    # Use existing polling logic
    pass
```

**Rollout Steps**:
1. Implement webhook infrastructure (endpoints, service, security)
2. Deploy to dev environment
3. Test with subset of tasks (10% traffic)
4. Monitor for webhook delivery issues
5. Gradually increase traffic to 50%, 100%

---

#### **Phase 2: Webhook-Only Mode** ✅
**Duration**: 1-2 weeks
**Goal**: Full migration, deprecate polling

```python
# Remove polling configuration
# Remove ExponentialBackoffPoller class
# Remove get_extraction_status() polling logic
# Simplify ExternalTaskTracker (no recovery needed)
```

**Cleanup Tasks**:
- Remove `app/core/polling_config.py`
- Remove recovery service background tasks
- Simplify `ai_core_client.py` (remove polling methods)
- Update documentation

---

#### **Phase 3: Cleanup and Optimization** 🧹
**Duration**: 1 week
**Goal**: Remove legacy code, optimize performance

- Remove `external_task_recovery_service.py`
- Remove recovery-related fields from ExternalTaskTracker
- Archive old code for reference
- Performance benchmarks and optimization

---

### 4.2 Backward Compatibility Considerations

**During Dual Mode**:
- Keep existing polling code intact
- Use feature flag to toggle behavior
- Monitor both systems for parity

**Deprecation Timeline**:
- Phase 1: Weeks 1-4 (Dual mode testing)
- Phase 2: Weeks 5-6 (Webhook-only)
- Phase 3: Week 7 (Cleanup)

---

## 5. Implementation Checklist

### 5.1 B4B API Changes

- [ ] **Create webhook endpoint** (`app/api/v1/internal/webhooks.py`)
  - [ ] POST `/api/v1/internal/webhooks/ai-core`
  - [ ] Signature verification
  - [ ] Event routing

- [ ] **Implement webhook service** (`app/services/webhook_service.py`)
  - [ ] Event processing logic
  - [ ] Integration with ExternalTaskTracker
  - [ ] Integration with QueueJob updates
  - [ ] Notification triggers

- [ ] **Add webhook security** (`app/core/webhook_security.py`)
  - [ ] HMAC-SHA256 signature verification
  - [ ] Shared secret management

- [ ] **Create webhook schemas** (`app/schemas/webhook.py`)
  - [ ] AICoreWebhookEvent model
  - [ ] Event type enums
  - [ ] Validation logic

- [ ] **Database migrations**
  - [ ] Add webhook tracking fields to ExternalTaskTracker
  - [ ] Create webhook_event_logs table
  - [ ] Add indexes

- [ ] **Update AI Core client** (`app/services/ai_core_client.py`)
  - [ ] Pass webhook_url in requests
  - [ ] Add feature flag for webhook mode
  - [ ] Keep polling as fallback

- [ ] **Configuration**
  - [ ] Add WEBHOOK_BASE_URL env var
  - [ ] Add WEBHOOK_SECRET env var
  - [ ] Add AI_CORE_WEBHOOK_ENABLED flag

- [ ] **Testing**
  - [ ] Unit tests for webhook service
  - [ ] Unit tests for signature verification
  - [ ] Integration tests for webhook endpoint
  - [ ] E2E tests with mocked AI Core

- [ ] **Documentation**
  - [ ] Update API docs
  - [ ] Add webhook troubleshooting guide
  - [ ] Update architecture diagrams

---

### 5.2 AI Core Changes

- [ ] **Create webhook client** (`app/services/webhook_client.py`)
  - [ ] HTTP client with retry logic
  - [ ] Signature generation
  - [ ] Event formatting

- [ ] **Add webhook hooks to task execution**
  - [ ] task_started event
  - [ ] task_progress events (configurable)
  - [ ] task_completed event
  - [ ] task_failed event

- [ ] **Configuration**
  - [ ] Add WEBHOOK_SECRET env var
  - [ ] Webhook retry settings
  - [ ] Event filtering config

- [ ] **Error handling**
  - [ ] Retry logic for failed deliveries
  - [ ] Logging for webhook failures
  - [ ] Fallback if webhook disabled

- [ ] **Testing**
  - [ ] Unit tests for webhook client
  - [ ] Integration tests with B4B API
  - [ ] Mock webhook endpoint for testing

---

### 5.3 Deployment Checklist

- [ ] **Pre-deployment**
  - [ ] Review code changes
  - [ ] Run full test suite
  - [ ] Update environment variables
  - [ ] Database migrations ready

- [ ] **Deployment (Dev)**
  - [ ] Deploy B4B API with webhook endpoint
  - [ ] Deploy AI Core with webhook client
  - [ ] Configure shared secret
  - [ ] Enable webhook mode (10% traffic)

- [ ] **Validation (Dev)**
  - [ ] Test webhook delivery
  - [ ] Verify signature validation
  - [ ] Check database updates
  - [ ] Monitor error logs

- [ ] **Rollout (Staging)**
  - [ ] Increase traffic to 50%
  - [ ] Monitor performance metrics
  - [ ] Compare webhook vs polling latency
  - [ ] Check for webhook failures

- [ ] **Production Rollout**
  - [ ] Gradual rollout: 10% → 50% → 100%
  - [ ] Monitor webhook delivery rate
  - [ ] Track error rates
  - [ ] Performance comparison

- [ ] **Post-Migration**
  - [ ] Disable polling code
  - [ ] Remove recovery service
  - [ ] Cleanup legacy code
  - [ ] Update monitoring dashboards

---

## 6. Risk Assessment

### 6.1 Technical Risks

| Risk | Impact | Probability | Mitigation |
|------|--------|-------------|------------|
| **Webhook delivery failures** | High | Medium | Implement retry logic with exponential backoff |
| **Network issues (B4B API down)** | High | Low | AI Core queues events, retries delivery |
| **Duplicate webhook events** | Medium | Medium | Idempotency with webhook_event_logs table |
| **Signature verification issues** | High | Low | Thorough testing, shared secret management |
| **Missing webhook events** | High | Low | Fallback: Keep minimal polling as safety net |
| **Performance degradation** | Medium | Low | Load testing, monitor webhook endpoint latency |

---

### 6.2 Operational Risks

| Risk | Impact | Probability | Mitigation |
|------|--------|-------------|------------|
| **Dual-mode complexity** | Medium | High | Feature flags, clear documentation, monitoring |
| **Configuration errors** | High | Medium | Validation on startup, clear error messages |
| **Debugging difficulties** | Medium | Medium | Comprehensive logging, webhook event audit trail |
| **Rollback complexity** | Medium | Low | Keep polling code intact during Phase 1 |

---

### 6.3 Business Risks

| Risk | Impact | Probability | Mitigation |
|------|--------|-------------|------------|
| **Service downtime during migration** | High | Low | Phased rollout, dual-mode operation |
| **Data loss (missed status updates)** | High | Low | Idempotency, event logging, monitoring |
| **User experience degradation** | Medium | Low | Testing, gradual rollout, quick rollback plan |

---

## 7. Success Metrics

### 7.1 Performance Metrics

- **Network Efficiency**
  - Target: 95% reduction in HTTP requests
  - Current: ~50-100 requests per task
  - Target: 3-5 webhooks per task

- **Latency Improvement**
  - Target: < 1 second from completion to detection
  - Current: 2-30 seconds (polling interval)

- **Resource Usage**
  - Target: 80% reduction in CPU cycles for status checking
  - Measure: Celery worker CPU usage

### 7.2 Reliability Metrics

- **Webhook Delivery Success Rate**: > 99%
- **Duplicate Event Rate**: < 0.1%
- **Webhook Endpoint Uptime**: > 99.9%
- **Event Processing Time**: < 100ms

### 7.3 Code Quality Metrics

- **Test Coverage**: > 90% for webhook components
- **Code Removal**: > 500 lines of polling logic deleted
- **Cyclomatic Complexity**: Reduction in task logic complexity

---

## 8. Timeline Estimate

| Phase | Duration | Key Milestones |
|-------|----------|----------------|
| **Phase 1: Implementation** | 2 weeks | Webhook endpoint, service, security |
| **Phase 2: AI Core Integration** | 1 week | Webhook client, task hooks |
| **Phase 3: Testing** | 1 week | Unit, integration, E2E tests |
| **Phase 4: Dev Deployment** | 1 week | Deploy, test, validate |
| **Phase 5: Staging Rollout** | 1 week | 10% → 50% traffic |
| **Phase 6: Production Rollout** | 2 weeks | 50% → 100% traffic |
| **Phase 7: Cleanup** | 1 week | Remove polling, optimize |
| **Total** | **9 weeks** | Full migration complete |

---

## 9. Recommendations

### 9.1 Immediate Actions (Week 1)

1. **Create webhook infrastructure** in b4b-api
   - Endpoint, service, security layer
   - Database migrations
   - Feature flag configuration

2. **Design webhook event schema**
   - Define event types
   - Validate with AI Core team
   - Document signature algorithm

3. **Set up development environment**
   - Shared secret configuration
   - Webhook testing tools (ngrok for local dev)
   - Monitoring dashboards

### 9.2 Best Practices

1. **Use idempotency tokens** for all webhook events
2. **Implement comprehensive logging** for debugging
3. **Monitor webhook delivery metrics** from day 1
4. **Keep polling as fallback** during Phase 1
5. **Test failure scenarios** (network issues, signature failures)
6. **Document webhook event schemas** clearly

### 9.3 Long-term Considerations

1. **Webhook versioning**: Plan for future changes to event schema
2. **Rate limiting**: Protect webhook endpoint from abuse
3. **Event replay**: Ability to replay missed/failed events
4. **Webhook observability**: Dashboard for delivery metrics
5. **Multi-service webhooks**: Extend pattern to other external services

---

## 10. Conclusion

The migration from polling to webhooks will significantly improve:
- ✅ **Performance**: 95% reduction in HTTP requests
- ✅ **Latency**: Real-time updates (< 1s vs 2-30s)
- ✅ **Scalability**: O(1) per task instead of O(n) polling
- ✅ **Code Quality**: Simpler, event-driven architecture

**Recommended Approach**: Phased migration with dual-mode operation ensures safety and allows for gradual validation.

**Total Effort**: ~9 weeks for complete migration including testing, rollout, and cleanup.

**ROI**: High - Immediate performance gains with long-term maintenance benefits.

---

## Appendix A: Example Webhook Payload

```json
{
  "event_id": "evt_abc123xyz",
  "event_name": "task_completed",
  "event_payload": {
    "task_id": "task_uuid_here",
    "service": "ai_core",
    "timestamp": "2025-10-13T10:15:30.123Z",
    "status": "completed",
    "result": {
      "invoice_id": "inv_123",
      "total_items": 10,
      "processed_items": 10,
      "classified_items": [
        {
          "line_item_id": "line_1",
          "confidence": 0.95,
          "classification": "6421"
        }
      ]
    },
    "processing_time_ms": 125000,
    "metadata": {
      "llm_model": "gemini-2.5-flash",
      "llm_provider": "vertex-ai"
    }
  }
}
```

---

## Appendix B: Key Code Locations

| Component | File Path | Lines | Purpose |
|-----------|-----------|-------|---------|
| AI Core Client | `app/services/ai_core_client.py` | 1-1509 | HTTP client for AI Core |
| Polling Config | `app/core/polling_config.py` | 1-158 | Polling intervals and backoff |
| Classification Task | `app/queue/tasks/ai_classification.py` | 480-942 | Main Celery task with polling |
| External Tracker Model | `app/models/external_task_tracker.py` | 1-114 | Database model for recovery |
| Recovery Service | `app/services/external_task_recovery_service.py` | 1-429 | Background recovery logic |

---

**Document Version**: 1.0
**Last Updated**: 2025-10-13
**Author**: Claude Code Analysis
**Review Status**: Ready for stakeholder review
