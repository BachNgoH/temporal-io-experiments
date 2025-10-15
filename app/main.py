"""Stateless FastAPI application - all state managed by Temporal."""

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi import Request
import os
import json
from datetime import datetime, timezone
from temporalio.client import Client

from app.config import settings
from app.models import (
    CreateScheduleRequest,
    ScheduleResponse,
    TaskRequest,
    TaskResponse,
    TaskStatus,
    TaskStatusResponse,
    TaskType,
)
from temporal_app.workflows.gdt_invoice_import import GdtInvoiceImportWorkflow
from temporalio.client import (
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleCalendarSpec,
    ScheduleRange,
    ScheduleSpec,
    ScheduleState,
)


# Global Temporal client
temporal_client: Client | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - setup and teardown."""
    global temporal_client

    # Startup: Connect to Temporal
    temporal_client = await Client.connect(
        settings.temporal_host,
        namespace=settings.temporal_namespace,
    )
    print(f"✅ Connected to Temporal at {settings.temporal_host}")

    yield

    # Shutdown: Close Temporal connection
    if temporal_client:
        await temporal_client.close()
    print("👋 Temporal connection closed")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
)


@app.get("/")
async def root() -> dict[str, str]:
    """Health check endpoint."""
    temporal_status = "connected" if temporal_client else "disconnected"
    return {
        "app": settings.app_name,
        "version": settings.app_version,
        "status": "healthy",
        "temporal_status": temporal_status,
        "temporal_host": settings.temporal_host,
    }


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Detailed health check including Temporal connection."""
    if not temporal_client:
        return {
            "status": "unhealthy",
            "error": "Temporal client not initialized",
            "temporal_status": "disconnected",
        }
    
    try:
        # Test Temporal connection by listing workflows (lightweight operation)
        await temporal_client.list_workflows().next()
        return {
            "status": "healthy",
            "temporal_status": "connected",
            "temporal_host": settings.temporal_host,
            "temporal_namespace": settings.temporal_namespace,
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": f"Temporal connection failed: {str(e)}",
            "temporal_status": "error",
        }


@app.post("/api/tasks/start")
async def start_task(request: TaskRequest) -> TaskResponse:
    """
    Start a new task workflow.

    Stateless design: No database writes, all state managed by Temporal.
    """
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")

    # Route to appropriate workflow based on task type
    workflow_class = _get_workflow_class(request.task_type)
    workflow_id = _generate_workflow_id(request.task_type, request.task_params)

    print(f"🚀 Starting workflow: {workflow_id}")
    print(f"📋 Task params: {request.task_params}")

    try:
        # Check if workflow already exists (idempotency check)
        try:
            existing_handle = temporal_client.get_workflow_handle(workflow_id)
            description = await existing_handle.describe()
            print(f"⚠️ Workflow {workflow_id} already exists with status: {description.status.name}")
            
            # If workflow is already running, return success
            if description.status.name in ["RUNNING", "PENDING"]:
                return TaskResponse(
                    workflow_id=workflow_id,
                    task_type=request.task_type,
                    status=TaskStatus.RUNNING,
                    message=f"Task {request.task_type.value} already running",
                )
        except Exception:
            # Workflow doesn't exist, continue to create new one
            pass

        # Start workflow (idempotent - same ID won't create duplicate)
        print(f"🔄 Creating new workflow: {workflow_id}")
        handle = await temporal_client.start_workflow(
            workflow_class.run,
            request.task_params,
            id=workflow_id,
            task_queue=settings.temporal_task_queue,
            # Search attributes for filtering in Temporal UI
            search_attributes={
                "CustomKeywordField": [request.task_type.value],
            },
        )

        print(f"✅ Workflow started successfully: {workflow_id}")
        return TaskResponse(
            workflow_id=workflow_id,
            task_type=request.task_type,
            status=TaskStatus.RUNNING,
            message=f"Task {request.task_type.value} started successfully",
        )

    except Exception as e:
        print(f"❌ Failed to start workflow {workflow_id}: {str(e)}")
        print(f"❌ Error type: {type(e).__name__}")
        import traceback
        print(f"❌ Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Failed to start task: {str(e)}")


@app.get("/api/tasks/{workflow_id}/status")
async def get_task_status(workflow_id: str) -> TaskStatusResponse:
    """
    Get task status and progress.

    Queries Temporal directly - no local database needed.
    """
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")

    try:
        # Get workflow handle
        handle = temporal_client.get_workflow_handle(workflow_id)

        # Get workflow description
        description = await handle.describe()

        # Query progress (non-blocking query to workflow)
        progress = None
        result = None
        try:
            progress = await handle.query("get_progress")
        except Exception:
            pass  # Progress query not available or workflow not started yet

        # Try to get result if completed
        if description.status.name in ["COMPLETED", "FAILED"]:
            try:
                result = await handle.result()
            except Exception as e:
                result = {"error": str(e)}

        # Map Temporal status to our status enum
        status_mapping = {
            "RUNNING": TaskStatus.RUNNING,
            "COMPLETED": TaskStatus.COMPLETED,
            "FAILED": TaskStatus.FAILED,
            "CANCELED": TaskStatus.CANCELLED,
            "TERMINATED": TaskStatus.CANCELLED,
            "TIMED_OUT": TaskStatus.FAILED,
        }

        return TaskStatusResponse(
            workflow_id=workflow_id,
            task_type=_extract_task_type_from_workflow_id(workflow_id),
            status=status_mapping.get(description.status.name, TaskStatus.PENDING),
            progress=progress,
            result=result,
            start_time=description.start_time,
            end_time=description.close_time,
        )

    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Workflow not found: {str(e)}")


@app.post("/api/tasks/{workflow_id}/cancel")
async def cancel_task(workflow_id: str) -> dict[str, str]:
    """Cancel a running task."""
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")

    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        await handle.cancel()

        return {
            "workflow_id": workflow_id,
            "status": "cancelled",
            "message": "Task cancellation requested",
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to cancel task: {str(e)}")


# ============================================================================
# Schedule Management Endpoints
# ============================================================================


@app.post("/api/schedules/create")
async def create_schedule(request: CreateScheduleRequest) -> ScheduleResponse:
    """
    Create a daily schedule for any task type.

    Schedule Behavior:
    - Runs daily at specified time (hour:minute in UTC)
    - Imports FULL previous day (00:00:00 to 23:59:59)
    - Best practice: Run at low-traffic time (e.g., 1:00 AM UTC)

    The task_params can use Go template syntax for dynamic dates:
    - {{ .ScheduledTime.Add(-24h).Format "2006-01-02" }} - previous day
    - {{ .ScheduledTime.Format "2006-01-02" }} - current day

    Example for daily invoice import (runs at 1 AM, imports previous full day):
    ```json
    {
        "schedule_id": "daily-invoice-import-company123",
        "task_type": "gdt_invoice_import",
        "task_params": {
            "company_id": "0123456789",
            "credentials": {"username": "user", "password": "pass"},
            "date_range_start": "{{ .ScheduledTime.Add(-24h).Format \"2006-01-02\" }}",
            "date_range_end": "{{ .ScheduledTime.Add(-24h).Format \"2006-01-02\" }}",
            "flows": ["ban_ra_dien_tu", "mua_vao_dien_tu"],
            "discovery_method": "excel",
            "processing_mode": "sequential"
        },
        "hour": 1,
        "minute": 0,
        "note": "Daily import - runs at 1 AM UTC, imports full previous day (00:00-23:59)"
    }
    ```

    Omit date_range_start/end to auto-import yesterday:
    ```json
    {
        "schedule_id": "daily-invoice-import-company123",
        "task_type": "gdt_invoice_import",
        "task_params": {
            "company_id": "0123456789",
            "credentials": {"username": "user", "password": "pass"}
        },
        "hour": 1,
        "minute": 0
    }
    ```
    """
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")

    try:
        # Get workflow class for the task type
        workflow_class = _get_workflow_class(request.task_type)

        # Create schedule
        schedule = Schedule(
            action=ScheduleActionStartWorkflow(
                workflow_class.run,
                request.task_params,
                id=f"{request.schedule_id}-{{{{ .ScheduledTime.Format \"20060102-150405\" }}}}",
                task_queue=settings.temporal_task_queue,
            ),
            spec=ScheduleSpec(
                calendars=[
                    ScheduleCalendarSpec(
                        hour=[ScheduleRange(start=request.hour)],
                        minute=[ScheduleRange(start=request.minute)],
                    )
                ],
            ),
            state=ScheduleState(
                note=request.note or f"Daily {request.task_type.value} schedule",
                paused=request.paused,
            ),
        )

        print(f"🗓️  Creating schedule: {request.schedule_id}")
        print(f"📋 Task type: {request.task_type.value}")
        print(f"⏰ Time: {request.hour:02d}:{request.minute:02d} UTC")

        await temporal_client.create_schedule(request.schedule_id, schedule)

        print(f"✅ Schedule created successfully: {request.schedule_id}")
        return ScheduleResponse(
            schedule_id=request.schedule_id,
            task_type=request.task_type,
            status="created",
            message=f"Schedule created - runs daily at {request.hour:02d}:{request.minute:02d} UTC",
        )

    except Exception as e:
        print(f"❌ Failed to create schedule: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to create schedule: {str(e)}")


@app.get("/api/schedules/{schedule_id}")
async def get_schedule(schedule_id: str) -> dict[str, Any]:
    """Get schedule details."""
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")

    try:
        handle = temporal_client.get_schedule_handle(schedule_id)
        desc = await handle.describe()

        return {
            "schedule_id": schedule_id,
            "paused": desc.schedule.state.paused,
            "note": desc.schedule.state.note,
            "num_actions": desc.info.num_actions,
            "num_actions_skipped": desc.info.num_actions_skipped_overlap,
        }

    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Schedule not found: {str(e)}")


@app.get("/api/schedules")
async def list_schedules() -> dict[str, list[dict[str, Any]]]:
    """List all schedules."""
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")

    try:
        schedules = []
        async for schedule in await temporal_client.list_schedules():
            schedules.append(
                {
                    "id": schedule.id,
                    "info": {
                        "num_actions": schedule.info.num_actions,
                        "paused": schedule.info.paused,
                    },
                }
            )

        return {"schedules": schedules}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list schedules: {str(e)}")


@app.post("/api/schedules/{schedule_id}/trigger")
async def trigger_schedule(schedule_id: str) -> dict[str, str]:
    """Manually trigger a schedule to run immediately."""
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")

    try:
        handle = temporal_client.get_schedule_handle(schedule_id)
        await handle.trigger()

        return {
            "schedule_id": schedule_id,
            "status": "triggered",
            "message": "Schedule triggered successfully",
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to trigger schedule: {str(e)}")


@app.post("/api/schedules/{schedule_id}/pause")
async def pause_schedule(schedule_id: str, note: str = "") -> dict[str, str]:
    """Pause a schedule."""
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")

    try:
        handle = temporal_client.get_schedule_handle(schedule_id)
        await handle.pause(note=note)

        return {
            "schedule_id": schedule_id,
            "status": "paused",
            "message": "Schedule paused successfully",
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to pause schedule: {str(e)}")


@app.post("/api/schedules/{schedule_id}/unpause")
async def unpause_schedule(schedule_id: str, note: str = "") -> dict[str, str]:
    """Unpause a schedule."""
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")

    try:
        handle = temporal_client.get_schedule_handle(schedule_id)
        await handle.unpause(note=note)

        return {
            "schedule_id": schedule_id,
            "status": "unpaused",
            "message": "Schedule unpaused successfully",
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to unpause schedule: {str(e)}")


@app.delete("/api/schedules/{schedule_id}")
async def delete_schedule(schedule_id: str) -> dict[str, str]:
    """Delete a schedule."""
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")

    try:
        handle = temporal_client.get_schedule_handle(schedule_id)
        await handle.delete()

        return {
            "schedule_id": schedule_id,
            "status": "deleted",
            "message": "Schedule deleted successfully",
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete schedule: {str(e)}")


# ============================================================================
# Internal webhook receiver (mock) - receives event posts from worker
# ============================================================================


@app.post("/internal/webhooks")
async def receive_internal_webhook(request: Request) -> dict[str, str]:
    """Mock endpoint to receive internal event webhooks from worker.

    Accepts arbitrary JSON. Logs and returns ack. Replace later with real handler.
    """
    try:
        body = await request.json()
    except Exception:
        body = {"error": "invalid json"}

    print("📨 Internal webhook received:")
    print(body)

    # Persist event to local JSON file under app/data/events/{run_id}
    try:
        run_id = body.get("run_id") or "unknown"
        event_name = body.get("event_name", "event")
        event_id = body.get("event_id", "no-id")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        base_dir = os.path.join(os.path.dirname(__file__), "data", "events", run_id)
        os.makedirs(base_dir, exist_ok=True)

        filename = f"{timestamp}_{event_name}_{event_id}.json"
        filepath = os.path.join(base_dir, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False, indent=2)

        print(f"💾 Saved event to {filepath}")
    except Exception as e:
        print(f"⚠️ Failed to persist webhook event: {e}")

    return {"status": "ok"}


# ============================================================================
# Helper Functions
# ============================================================================


def _get_workflow_class(task_type: TaskType) -> Any:
    """Route task type to appropriate workflow class."""
    workflow_mapping = {
        TaskType.GDT_INVOICE_IMPORT: GdtInvoiceImportWorkflow,
        # Future task types:
        # TaskType.GDT_TAX_REPORT_SYNC: GdtTaxReportSyncWorkflow,
        # TaskType.DATA_PIPELINE: DataPipelineWorkflow,
    }

    workflow = workflow_mapping.get(task_type)
    if not workflow:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported task type: {task_type}",
        )

    return workflow


def _generate_workflow_id(task_type: TaskType, params: dict[str, Any]) -> str:
    """Generate deterministic workflow ID for idempotency."""
    import time
    
    if task_type == TaskType.GDT_INVOICE_IMPORT:
        # Add timestamp to make workflow ID unique for concurrent requests
        timestamp = int(time.time() * 1000)  # milliseconds for uniqueness
        return (
            f"{task_type.value}-"
            f"{params['company_id']}-"
            f"{params['date_range_start']}-"
            f"{params['date_range_end']}-"
            f"{timestamp}"
        )

    # Default: use task type + company_id + timestamp if available
    company_id = params.get("company_id", "unknown")
    timestamp = int(time.time() * 1000)
    return f"{task_type.value}-{company_id}-{timestamp}"


def _extract_task_type_from_workflow_id(workflow_id: str) -> TaskType:
    """Extract task type from workflow ID."""
    # Workflow ID format: task_type-company_id-...
    task_type_str = workflow_id.split("-")[0]

    try:
        return TaskType(task_type_str)
    except ValueError:
        return TaskType.GDT_INVOICE_IMPORT  # Default fallback
