"""GDT Invoice Import Workflow - Extensible base for future task types."""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

# Import activities
with workflow.unsafe.imports_passed_through():
    from temporal_app.activities import discover_invoices, fetch_invoice, login_to_gdt
    from temporal_app.models import GdtInvoice, GdtLoginRequest, GdtSession, InvoiceFetchResult


@workflow.defn
class GdtInvoiceImportWorkflow:
    """
    Workflow for importing invoices from GDT portal.

    Architecture: Login → Discover → Fetch (with concurrency control)

    Rate Limiting Strategy:
    - Activities detect 429 and throw RateLimitError
    - Temporal retries with exponential backoff (automatic spread)
    - Workflow-level concurrency control (max 10 concurrent per company)
    - Activities use shared rate limit state (prevents cascade failures)

    This is designed to be extensible - future task types can follow
    the same pattern:
    - Authenticate
    - Discover items
    - Process items in parallel
    """

    def __init__(self) -> None:
        self.session: GdtSession | None = None
        self.invoices: list[GdtInvoice] = []
        self.results: list[InvoiceFetchResult] = []

        # Progress tracking
        self.total_invoices = 0
        self.completed_invoices = 0
        self.failed_invoices = 0

    @workflow.run
    async def run(self, params: dict) -> dict:
        """
        Main workflow execution.

        Args:
            params: Task parameters (see GdtInvoiceImportParams)

        Returns:
            dict: Task result with statistics
        """
        workflow.logger.info(
            f"Starting GDT invoice import for {params['company_id']} "
            f"from {params['date_range_start']} to {params['date_range_end']}"
        )

        try:
            # Step 1: Login to GDT portal
            self.session = await self._login(params)

            # Step 2: Discover all invoices
            self.invoices = await self._discover(params)
            self.total_invoices = len(self.invoices)

            workflow.logger.info(f"Found {self.total_invoices} invoices to import")

            # Step 3: Fetch all invoices in parallel (with concurrency limit)
            await self._fetch_all_invoices()

            # Step 4: Return result
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
                "invoices": [r.data for r in self.results if r.success],
            }

        except Exception as e:
            workflow.logger.error(f"Workflow failed: {str(e)}")
            return {
                "status": "failed",
                "error": str(e),
                "completed_invoices": self.completed_invoices,
                "total_invoices": self.total_invoices,
            }

    async def _login(self, params: dict) -> GdtSession:
        """Login to GDT portal with automatic retry."""
        login_request = GdtLoginRequest(
            company_id=params["company_id"],
            username=params["credentials"]["username"],
            password=params["credentials"]["password"],
        )

        session = await workflow.execute_activity(
            login_to_gdt,
            login_request,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=10),
                maximum_interval=timedelta(minutes=2),
                maximum_attempts=5,
                backoff_coefficient=2.0,
            ),
        )

        workflow.logger.info(f"✅ Logged in successfully: {session.session_id}")
        return session

    async def _discover(self, params: dict) -> list[GdtInvoice]:
        """Discover all invoices in date range."""
        invoices = await workflow.execute_activity(
            discover_invoices,
            args=[
                self.session,
                params["date_range_start"],
                params["date_range_end"],
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

    async def _fetch_all_invoices(self) -> None:
        """
        Fetch all invoices with concurrency control.

        Rate Limiting Strategy:
        - Workflow-level: max 10 concurrent fetches per workflow (via semaphore)
        - Activity-level: Activities check rate limit before fetching
        - Temporal retry: Exponential backoff spreads retries automatically
        - Shared state: Activities coordinate via Temporal signals/queries

        Why this works:
        1. Semaphore prevents too many concurrent requests
        2. When 429 occurs, that activity retries with backoff
        3. Other activities continue (not blocked)
        4. Exponential backoff spreads retries naturally
        5. Eventually all invoices complete

        Example:
        - 100 invoices, 10 concurrent
        - Invoice #5 hits 429 → retries in 10s, 20s, 40s...
        - Invoices #6-10 continue normally
        - When invoice #5 retries, others may have finished (less contention)
        """
        # Concurrency limit: 10 invoices at a time per workflow
        # Adjust based on:
        # - GDT rate limits (e.g., 100 req/min → ~10 concurrent is safe)
        # - Worker capacity
        # - Number of concurrent workflows
        max_concurrent = 10
        semaphore = asyncio.Semaphore(max_concurrent)

        async def fetch_with_semaphore(invoice: GdtInvoice) -> InvoiceFetchResult:
            async with semaphore:
                return await self._fetch_single_invoice(invoice)

        # Execute all fetches in parallel (with concurrency limit)
        tasks = [fetch_with_semaphore(invoice) for invoice in self.invoices]

        self.results = await asyncio.gather(*tasks, return_exceptions=True)

        # Count successes/failures
        for result in self.results:
            if isinstance(result, InvoiceFetchResult):
                if result.success:
                    self.completed_invoices += 1
                else:
                    self.failed_invoices += 1
            else:
                # Exception occurred
                self.failed_invoices += 1

        workflow.logger.info(
            f"✅ Fetch complete: {self.completed_invoices} succeeded, "
            f"{self.failed_invoices} failed"
        )

    async def _fetch_single_invoice(self, invoice: GdtInvoice) -> InvoiceFetchResult:
        """
        Fetch single invoice with retry logic.

        Retry Policy Explanation:
        - initial_interval=5s: First retry after 5 seconds
        - maximum_interval=5min: Cap retry delay at 5 minutes
        - maximum_attempts=7: Try up to 7 times total
        - backoff_coefficient=2.0: Double delay each retry (5s, 10s, 20s, 40s, 80s, 160s, 300s)

        This exponential backoff naturally spreads out retries:
        - 100 invoices hit 429 at t=0
        - Retry 1: spread over 5s window (not all at same time)
        - Retry 2: spread over 10s window
        - Retry 3: spread over 20s window
        - Eventually rate limit clears
        """
        try:
            result = await workflow.execute_activity(
                fetch_invoice,
                args=[invoice, self.session],
                start_to_close_timeout=timedelta(minutes=10),  # Total time including retries
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=5),
                    maximum_interval=timedelta(minutes=5),
                    maximum_attempts=7,
                    backoff_coefficient=2.0,
                ),
            )
            return result

        except Exception as e:
            workflow.logger.error(f"Failed to fetch invoice {invoice.invoice_id}: {str(e)}")
            return InvoiceFetchResult(
                invoice_id=invoice.invoice_id,
                success=False,
                error=str(e),
            )

    @workflow.query
    def get_progress(self) -> dict:
        """
        Query to get current progress.

        Can be called while workflow is running (non-blocking).
        """
        return {
            "total_invoices": self.total_invoices,
            "completed_invoices": self.completed_invoices,
            "failed_invoices": self.failed_invoices,
            "percentage": (
                round(self.completed_invoices / self.total_invoices * 100, 2)
                if self.total_invoices > 0
                else 0.0
            ),
        }

    @workflow.signal
    async def cancel_workflow(self) -> None:
        """Signal to cancel workflow gracefully."""
        workflow.logger.info("Workflow cancellation requested")
        raise Exception("Workflow cancelled by user")
