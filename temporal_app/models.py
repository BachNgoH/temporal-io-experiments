"""Data models for Temporal workflows and activities."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from pydantic import BaseModel


# ============================================================================
# GDT Invoice Import Models
# ============================================================================


@dataclass
class GdtLoginRequest:
    """Request for GDT login."""

    company_id: str
    username: str
    password: str


@dataclass
class GdtSession:
    """GDT session information."""

    company_id: str
    session_id: str
    access_token: str
    cookies: dict[str, str]
    expires_at: datetime


@dataclass
class GdtInvoice:
    """GDT invoice information."""

    invoice_id: str
    invoice_number: str
    invoice_date: str
    invoice_type: str
    amount: float
    tax_amount: float
    supplier_name: str
    supplier_tax_code: str
    metadata: dict[str, str]


class DiscoveryResult(BaseModel):
    """Result of discovery activities.

    - invoices: raw items (from API `datas` or Excel rows) - unparsed for flexibility
    - The workflow will normalize these into `GdtInvoice` before fetching
    """

    company_id: str
    date_range_start: str
    date_range_end: str
    flows: list[str]
    invoice_count: int
    invoices: list[Any]
    raw_invoices: list[Any] | None = None


@dataclass
class InvoiceFetchResult:
    """Result of fetching a single invoice."""

    invoice_id: str
    success: bool
    data: dict[str, Any] | None = None
    error: str | None = None
    invoice_xml: str | None = None  # XML content as string


# ============================================================================
# Event DTOs (typed payloads for start/end hooks)
# ============================================================================


@dataclass
class SummaryV1:
    """Small, serializable summary of a run."""

    total_invoices: int
    completed_invoices: int
    failed_invoices: int
    success_rate: float


@dataclass
class WorkflowStartEventV1:
    """Event payload for workflow start."""

    workflow_id: str
    run_id: str
    company_id: str
    date_range_start: str
    date_range_end: str
    flows: list[str]
    run_ref: str | None = None


@dataclass
class WorkflowEndEventV1:
    """Event payload for workflow completion."""

    workflow_id: str
    run_id: str
    company_id: str
    status: str  # "completed" | "failed"
    summary: SummaryV1
    result_ref: str | None = None


@dataclass
class DiscoveryEventV1:
    """Event payload for discovery lifecycle events."""

    phase: str  # "start" | "end"
    company_id: str
    method: str  # "api" | "excel"
    flows: list[str]
    invoice_count: int | None = None


@dataclass
class FetchEventV1:
    """Event payload for per-invoice fetch lifecycle events."""

    phase: str  # "start" | "end"
    company_id: str
    invoice_id: str
    flow_type: str | None
    endpoint_kind: str | None
    success: bool | None = None
    error: str | None = None
