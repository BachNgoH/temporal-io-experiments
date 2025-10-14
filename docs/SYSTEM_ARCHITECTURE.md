# System Architecture Documentation

## Overview

The Temporal Task System is a production-ready, extensible workflow orchestration platform built on Temporal.io with FastAPI. It's designed for scalable task execution on Google Cloud Platform (GCP) with a hybrid architecture that balances cost and performance.

## Core Architecture

### Hybrid Deployment Model

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

### System Components

#### 1. API Layer (FastAPI)
- **Location**: [`app/main.py`](../app/main.py)
- **Purpose**: Stateless REST API for task management
- **Key Features**:
  - Task initiation and status monitoring
  - No database dependency - all state managed by Temporal
  - Health checks and monitoring endpoints
  - Internal webhook receiver for event processing

#### 2. Workflow Engine (Temporal)
- **Location**: [`temporal_app/workflows/`](../temporal_app/workflows/)
- **Purpose**: Orchestrates complex, long-running tasks
- **Key Features**:
  - Durable execution with automatic retries
  - State management and persistence
  - Activity coordination and parallel execution
  - Built-in monitoring through Temporal Web UI

#### 3. Activities Layer
- **Location**: [`temporal_app/activities/`](../temporal_app/activities/)
- **Purpose**: Individual task implementations
- **Key Features**:
  - Modular, reusable components
  - Built-in retry and error handling
  - Event emission via decorators
  - Rate limiting and backoff strategies

#### 4. Worker Infrastructure
- **Base Workers**: Always-on instances on Compute Engine
- **Burst Workers**: On-demand Cloud Run Jobs for scaling
- **Configuration**: [`temporal_app/worker.py`](../temporal_app/worker.py)

## Data Flow

### Task Execution Flow

1. **Task Submission**
   ```
   Client → FastAPI → Temporal Server → Workflow Queue
   ```

2. **Worker Processing**
   ```
   Workflow Queue → Worker → Activities → External Systems
   ```

3. **Event Emission**
   ```
   Activities → Webhook Decorator → FastAPI → Event Storage
   ```

4. **Status Monitoring**
   ```
   Client → FastAPI → Temporal Server → Workflow State
   ```

### State Management

The system is designed to be stateless at the API layer:

- **Temporal Server**: Manages workflow state, execution history, and retries
- **PostgreSQL**: Persists Temporal's internal state
- **Event Storage**: Local JSON files for webhook events (temporary)
- **No Application Database**: All business state lives in Temporal

## Key Design Patterns

### 1. Extensible Task Types

The system supports multiple task types through a unified interface:

```python
# Adding new task types is straightforward
class TaskType(str, Enum):
    GDT_INVOICE_IMPORT = "gdt_invoice_import"
    # Future task types can be added here
```

### 2. Activity Decorators

Reusable decorators handle cross-cutting concerns:

```python
@emit_on_complete(
    event_name="discovery.completed",
    payload_from_result=lambda result, *args: {...},
    compact_from_result=lambda result, *args: {...},
)
async def discover_invoices(...):
    # Activity implementation
```

### 3. Adaptive Batch Processing

Intelligent batch sizing based on system performance:

```python
class BatchConfig:
    batch_size: int = 8
    min_batch_size: int = 3
    max_batch_size: int = 10
    
    def reduce_batch_size(self) -> None:
        self.batch_size = max(self.min_batch_size, self.batch_size - 2)
```

### 4. Hybrid Worker Architecture

Two-tier worker strategy for cost optimization:

- **Base Mode**: Continuous processing with moderate concurrency
- **Burst Mode**: High-concurrency, short-lived workers for backlog clearing

## Technology Stack

### Core Technologies

- **Temporal.io**: Workflow orchestration and state management
- **FastAPI**: REST API framework
- **PostgreSQL**: Temporal state persistence
- **Docker**: Containerization
- **Python 3.11+**: Primary programming language

### External Integrations

- **Google Cloud Platform**: Infrastructure and services
- **Vertex AI (Gemini)**: CAPTCHA solving
- **Google Cloud Storage**: XML file storage
- **httpx**: Async HTTP client

### Development Tools

- **uv**: Python package management
- **pytest**: Testing framework
- **black**: Code formatting
- **ruff**: Linting and code analysis
- **mypy**: Static type checking

## Security Architecture

### Authentication & Authorization

- **Bearer Tokens**: GDT portal authentication
- **Service Accounts**: GCP resource access
- **Webhook Signatures**: RFC 9421 HTTP Message Signatures for event verification

### Data Protection

- **TLS**: All external communications encrypted
- **Credential Management**: Environment-based configuration
- **Temporary Storage**: Local files cleaned up after processing

## Monitoring & Observability

### Logging Strategy

- **Structured Logging**: Consistent log formats across components
- **Log Levels**: Appropriate verbosity for different environments
- **Activity Heartbeats**: Long-running activity progress tracking

### Metrics & Monitoring

- **Temporal Web UI**: Real-time workflow monitoring
- **Health Checks**: API and worker health endpoints
- **Event Tracking**: Comprehensive activity lifecycle events

### Error Handling

- **Retry Policies**: Configurable exponential backoff
- **Error Classification**: Retriable vs. non-retriable errors
- **Circuit Breakers**: Protection against cascade failures

## Scalability Architecture

### Horizontal Scaling

- **Worker Replication**: Multiple worker instances
- **Task Queue Partitioning**: Separate queues for different task types
- **Load Distribution**: Temporal's built-in load balancing

### Vertical Scaling

- **Resource Allocation**: Configurable CPU/memory per worker
- **Concurrency Control**: Tunable activity and workflow limits
- **Burst Capacity**: On-demand resource provisioning

## Deployment Architecture

### Local Development

```yaml
# docker-compose.yml
services:
  - postgresql (Temporal state)
  - temporal (Workflow engine)
  - temporal-ui (Monitoring)
  - api (FastAPI)
  - temporal-worker (Base workers)
```

### Production Deployment

- **Compute Engine**: Base infrastructure
- **Cloud Run Jobs**: Burst scaling
- **Cloud Storage**: File persistence
- **Load Balancer**: API traffic distribution

## Cost Optimization

### Base Infrastructure (~$50/mo)

- Compute Engine (e2-medium): $25-40
- Storage and network: $5-10
- Temporal operations: $5-10

### Burst Scaling (~$1-5 per batch)

- Cloud Run Jobs: Pay-per-use
- Auto-scaling: 0-100 workers
- Quick termination: Cost stops when work completes

### High-Throughput Scenario (~$650/mo)

- Compute Engine (e2-standard-2): $50
- Burst executions (10/day × $2): $600
- Total for 100 companies × 1000 invoices

## Reliability & Fault Tolerance

### High Availability

- **Temporal Durability**: Workflows survive crashes and restarts
- **Automatic Retries**: Configurable retry policies
- **Graceful Degradation**: Fallback mechanisms for external failures

### Disaster Recovery

- **State Persistence**: PostgreSQL backups
- **Infrastructure as Code**: Reproducible deployments
- **Multi-Region Support**: Geographic distribution options

## Performance Characteristics

### Latency

- **API Response**: <100ms for status queries
- **Task Initiation**: <500ms for workflow start
- **Activity Execution**: Variable based on external systems

### Throughput

- **Base Workers**: 200 concurrent activities
- **Burst Workers**: 2500+ concurrent activities
- **Batch Processing**: Adaptive sizing (3-15 items per batch)

### Resource Efficiency

- **Memory Management**: Automatic cleanup of temporary resources
- **Connection Pooling**: Efficient HTTP client usage
- **CPU Utilization**: Configurable concurrency limits