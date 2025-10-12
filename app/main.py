"""Stateless FastAPI application - all state managed by Temporal."""

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from temporalio.client import Client

from app.config import settings
from app.models import TaskRequest, TaskResponse, TaskStatus, TaskStatusResponse, TaskType
from temporal_app.workflows.gdt_invoice_import import GdtInvoiceImportWorkflow


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
