"""API models for FastAPI."""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TaskType(str, Enum):
    """Supported task types - easily extensible."""

    GDT_INVOICE_IMPORT = "gdt_invoice_import"
    # Future task types:
    # GDT_TAX_REPORT_SYNC = "gdt_tax_report_sync"
    # GDT_COMPLIANCE_CHECK = "gdt_compliance_check"
    # DATA_PIPELINE = "data_pipeline"
    # DOCUMENT_PROCESSOR = "document_processor"


class TaskStatus(str, Enum):
    """Task execution status."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ============================================================================
# Generic Task Models (Base)
# ============================================================================


class TaskRequest(BaseModel):
    """Base model for task requests."""

    task_type: TaskType
    task_params: dict[str, Any] = Field(
        ..., description="Task-specific parameters (varies by task type)"
    )


class TaskResponse(BaseModel):
    """Response after starting a task."""

    workflow_id: str
    task_type: TaskType
    status: TaskStatus
    message: str


class TaskStatusResponse(BaseModel):
    """Task status and progress."""

    workflow_id: str
    task_type: TaskType
    status: TaskStatus
    progress: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None


# ============================================================================
# GDT Invoice Import Models (Specific Task Type)
# ============================================================================


class InvoiceFlow(str, Enum):
    """GDT invoice flows."""

    BAN_RA_DIEN_TU = "ban_ra_dien_tu"  # Outbound electronic invoices
    BAN_RA_MAY_TINH_TIEN = "ban_ra_may_tinh_tien"  # Outbound cash register invoices
    MUA_VAO_DIEN_TU = "mua_vao_dien_tu"  # Inbound electronic invoices
    MUA_VAO_MAY_TINH_TIEN = "mua_vao_may_tinh_tien"  # Inbound cash register invoices


class GdtInvoiceImportParams(BaseModel):
    """Parameters for GDT invoice import task."""

    company_id: str = Field(..., description="Unique company identifier")
    company_name: str = Field(..., description="Company name")
    credentials: dict[str, str] = Field(
        ..., description="GDT portal login credentials (username, password)"
    )
    date_range_start: str | None = Field(
        None, description="Start date (YYYY-MM-DD, defaults to yesterday if not provided)"
    )
    date_range_end: str | None = Field(
        None, description="End date (YYYY-MM-DD, defaults to yesterday if not provided)"
    )
    flows: list[InvoiceFlow] = Field(
        default=[
            InvoiceFlow.BAN_RA_DIEN_TU,
            InvoiceFlow.BAN_RA_MAY_TINH_TIEN,
            InvoiceFlow.MUA_VAO_DIEN_TU,
            InvoiceFlow.MUA_VAO_MAY_TINH_TIEN,
        ],
        description="Invoice flows to crawl (default: all flows)",
    )
    discovery_method: str = Field(
        default="excel", description="Discovery method: 'api' or 'excel' (default: excel)"
    )
    processing_mode: str = Field(
        default="sequential",
        description="Processing mode: 'sequential' or 'parallel' (default: sequential)",
    )


class GdtInvoiceImportProgress(BaseModel):
    """Progress information for GDT invoice import."""

    total_invoices: int = 0
    completed_invoices: int = 0
    failed_invoices: int = 0
    percentage: float = 0.0


class GdtInvoiceImportResult(BaseModel):
    """Result of GDT invoice import task."""

    company_id: str
    total_invoices: int
    completed_invoices: int
    failed_invoices: int
    invoices: list[dict[str, Any]]


# ============================================================================
# Future Task Types (Examples)
# ============================================================================


class GdtTaxReportSyncParams(BaseModel):
    """Parameters for GDT tax report sync task (example future task)."""

    company_id: str
    report_period: str  # 2024-Q1, 2024-Q2, etc.
    report_types: list[str]  # vat, corporate_tax, etc.


class GdtComplianceCheckParams(BaseModel):
    """Parameters for GDT compliance check task (example future task)."""

    company_id: str
    check_types: list[str]  # invoice_matching, tax_calculation, etc.
    date_range_start: str
    date_range_end: str


class DataPipelineParams(BaseModel):
    """Parameters for generic data pipeline task (example future task)."""

    pipeline_name: str
    source_config: dict[str, Any]
    transform_steps: list[str]
    destination_config: dict[str, Any]


# ============================================================================
# Schedule Models
# ============================================================================


class CreateScheduleRequest(BaseModel):
    """Request to create a daily schedule for any task type."""

    schedule_id: str = Field(..., description="Unique schedule identifier")
    task_type: TaskType = Field(..., description="Type of task to schedule")
    task_params: dict[str, Any] = Field(
        ..., description="Task-specific parameters (supports Go template for dynamic dates)"
    )
    hour: int = Field(default=1, ge=0, le=23, description="Hour to run daily (0-23, UTC)")
    minute: int = Field(default=0, ge=0, le=59, description="Minute to run (0-59)")
    paused: bool = Field(default=False, description="Create schedule in paused state")
    note: str = Field(default="", description="Optional note describing the schedule")


class ScheduleResponse(BaseModel):
    """Response after creating a schedule."""

    schedule_id: str
    task_type: TaskType
    status: str
    message: str
