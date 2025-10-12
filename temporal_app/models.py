"""Data models for Temporal workflows and activities."""

from dataclasses import dataclass
from datetime import datetime


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


@dataclass
class InvoiceFetchResult:
    """Result of fetching a single invoice."""

    invoice_id: str
    success: bool
    data: dict[str, str] | None = None
    error: str | None = None
