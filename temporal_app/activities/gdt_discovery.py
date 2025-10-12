"""GDT invoice discovery activities."""

import asyncio
import random
from datetime import datetime, timedelta

from temporalio import activity

from temporal_app.models import GdtInvoice, GdtSession


@activity.defn
async def discover_invoices(
    session: GdtSession,
    date_range_start: str,
    date_range_end: str,
) -> list[GdtInvoice]:
    """
    Discover all invoices in date range from GDT portal.

    This is a MOCK implementation. In production, this would:
    1. Make paginated API calls to GDT
    2. Handle rate limiting
    3. Parse invoice list from response
    4. Return all discovered invoices
    """
    activity.logger.info(
        f"Discovering invoices for {session.company_id} "
        f"from {date_range_start} to {date_range_end}"
    )

    # Mock: Generate random number of invoices (simulating real workload)
    # For testing: between 50-200 invoices
    num_invoices = random.randint(50, 200)

    invoices: list[GdtInvoice] = []

    # Simulate paginated discovery
    page_size = 50
    total_pages = (num_invoices + page_size - 1) // page_size

    for page in range(total_pages):
        # Send heartbeat to show progress
        activity.heartbeat(f"Page {page + 1}/{total_pages}, found {len(invoices)} invoices")

        # Simulate API call delay
        await asyncio.sleep(random.uniform(0.5, 1.5))

        # Generate mock invoices for this page
        invoices_in_page = min(page_size, num_invoices - len(invoices))

        for i in range(invoices_in_page):
            invoice_num = len(invoices) + 1
            invoice = GdtInvoice(
                invoice_id=f"INV-{session.company_id}-{invoice_num:05d}",
                invoice_number=f"GDT{datetime.now().year}{invoice_num:06d}",
                invoice_date=_random_date(date_range_start, date_range_end),
                invoice_type=random.choice(["VAT", "Service", "Sales"]),
                amount=round(random.uniform(100.0, 50000.0), 2),
                tax_amount=round(random.uniform(10.0, 5000.0), 2),
                supplier_name=f"Supplier {random.randint(1, 100)}",
                supplier_tax_code=f"TAX{random.randint(1000000000, 9999999999)}",
                metadata={
                    "status": "issued",
                    "payment_status": random.choice(["paid", "unpaid", "partial"]),
                },
            )
            invoices.append(invoice)

        activity.logger.info(f"Page {page + 1}/{total_pages} complete")

    activity.logger.info(f"✅ Discovery complete: {len(invoices)} invoices found")

    return invoices


def _random_date(start: str, end: str) -> str:
    """Generate random date between start and end."""
    start_date = datetime.strptime(start, "%Y-%m-%d")
    end_date = datetime.strptime(end, "%Y-%m-%d")

    time_between = end_date - start_date
    random_days = random.randint(0, time_between.days)
    random_date = start_date + timedelta(days=random_days)

    return random_date.strftime("%Y-%m-%d")
