"""GDT Invoice Import Workflow - Extensible base for future task types."""

import asyncio
from datetime import timedelta
from dataclasses import dataclass

from temporalio import workflow
from temporalio.common import RetryPolicy

# Import activities
with workflow.unsafe.imports_passed_through():
    from temporal_app.activities import (
        discover_invoices,
        discover_invoices_excel,
        fetch_invoice,
        login_to_gdt,
    )
    from temporal_app.models import (
        GdtInvoice,
        GdtLoginRequest,
        GdtSession,
        InvoiceFetchResult,
        SummaryV1,
    )


@dataclass
class BatchConfig:
    """Configuration for batch processing with adaptive sizing."""
    batch_size: int = 8
    min_batch_size: int = 3
    max_batch_size: int = 10
    delay: float = 1.0
    base_delay: float = 1.0
    processing_mode: str = "sequential"  # "parallel" or "sequential"
    
    def __post_init__(self):
        """Initialize delay from base_delay and configure for processing mode."""
        self.delay = self.base_delay
        
        # Override settings for sequential mode
        if self.processing_mode == "sequential":
            self.batch_size = 1
            self.min_batch_size = 1
            self.max_batch_size = 1
            self.base_delay = 2.0  # Longer delay for sequential
            self.delay = 2.0
    
    def reduce_batch_size(self) -> None:
        """Reduce batch size when hitting rate limits."""
        self.batch_size = max(self.min_batch_size, self.batch_size - 2)
    
    def increase_batch_size(self) -> None:
        """Increase batch size when performing well."""
        self.batch_size = min(self.max_batch_size, self.batch_size + 1)
    
    def increase_delay(self, rate_limit_errors: int) -> None:
        """Increase delay based on rate limit errors."""
        self.delay = min(5.0, self.base_delay * (1 + rate_limit_errors))
    
    def reset_delay(self) -> None:
        """Reset delay to base value."""
        self.delay = self.base_delay


@dataclass
class RetryConfig:
    """Configuration for retry processing."""
    batch_size: int = 3
    delay: float = 3.0


@dataclass
class BatchStats:
    """Statistics for a batch of invoice processing results."""
    successes: int = 0
    failures: int = 0
    rate_limit_errors: int = 0


@dataclass
class DiscoveryConfig:
    """Configuration for invoice discovery method."""
    method: str = "excel"  # "api" or "excel" - Excel is more reliable
    excel_fallback: bool = True  # Fallback to Excel if API fails
    
    def __post_init__(self):
        """Validate discovery method."""
        if self.method not in ["api", "excel"]:
            raise ValueError(f"Invalid discovery method: {self.method}. Must be 'api' or 'excel'")


@workflow.defn
class GdtInvoiceImportWorkflow:
    """
    Workflow for importing invoices from GDT portal.

    Architecture: Login → Discover → Fetch (with concurrency control)

    Smart Batch Processing Strategy:
    - Process invoices in adaptive batches (3-15 invoices per batch)
    - Wait for batch completion before starting next batch
    - Adaptive batch sizing based on success/failure rates
    - Conservative retry policy to avoid retry exhaustion
    - Optional retry phase for failed invoices

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
        self.processing_mode: str = "sequential"  # Default to parallel
        self.company_id: str = ""

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

        # Extract processing mode from parameters
        self.processing_mode = params.get("processing_mode", "sequential")
        workflow.logger.info(f"Processing mode: {self.processing_mode}")

        # Store company_id for events
        self.company_id = params.get("company_id", "")

        # Prepare flows for event payloads
        flows_param = params.get(
            "flows",
            [
                "ban_ra_dien_tu",
                "ban_ra_may_tinh_tien",
                "mua_vao_dien_tu",
                "mua_vao_may_tinh_tien",
            ],
        )
        event_flows = [f.value if hasattr(f, "value") else f for f in flows_param]

        # No workflow-level external emits anymore; discovery/fetch handle their own webhooks

        try:
            # Step 1: Login to GDT portal
            self.session = await self._login(params)

            # Step 2: Discover all invoices
            self.invoices = await self._discover(params)
            self.total_invoices = len(self.invoices)

            workflow.logger.info(f"Found {self.total_invoices} invoices to import")

            # Step 3: Fetch all invoices in parallel (with concurrency limit)
            await self._fetch_all_invoices()

            # Step 4: Emit END event and return compact result
            success_rate = (
                round(self.completed_invoices / self.total_invoices * 100, 2)
                if self.total_invoices > 0
                else 0.0
            )
            summary = SummaryV1(
                total_invoices=self.total_invoices,
                completed_invoices=self.completed_invoices,
                failed_invoices=self.failed_invoices,
                success_rate=success_rate,
            )
            result_ref = f"wf:{workflow.info().workflow_id}:{workflow.info().run_id}"

            # Completed - result emitted via activities; return compact summary

            return {
                "status": "completed",
                "company_id": params["company_id"],
                "total_invoices": self.total_invoices,
                "completed_invoices": self.completed_invoices,
                "failed_invoices": self.failed_invoices,
                "success_rate": success_rate,
                "result_ref": result_ref,
            }

        except Exception as e:
            workflow.logger.error(f"Workflow failed: {str(e)}")
            success_rate = (
                round(self.completed_invoices / (self.total_invoices or 1) * 100, 2)
                if self.total_invoices > 0
                else 0.0
            )
            summary = SummaryV1(
                total_invoices=self.total_invoices,
                completed_invoices=self.completed_invoices,
                failed_invoices=self.failed_invoices,
                success_rate=success_rate,
            )
            # Failure - activities may have posted partial progress already
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
        """Discover all invoices in date range for all flows using configurable method."""
        # Get flows from params (default to all flows if not provided)
        flows = params.get(
            "flows",
            [
                "ban_ra_dien_tu",
                "ban_ra_may_tinh_tien",
                "mua_vao_dien_tu",
                "mua_vao_may_tinh_tien",
            ],
        )

        # Convert enum values to strings if needed
        flow_strings = [f.value if hasattr(f, "value") else f for f in flows]
        
        # Get discovery configuration from params
        discovery_config = DiscoveryConfig(
            method=params.get("discovery_method", "excel"),  # Excel is more reliable
            excel_fallback=params.get("excel_fallback", True)
        )
        
        workflow.logger.info(f"🔍 Discovery method: {discovery_config.method}")
        workflow.logger.info(f"🔄 Excel fallback enabled: {discovery_config.excel_fallback}")

        # Discovery emits via decorator on completion

        # Try primary discovery method
        try:
            if discovery_config.method == "api":
                invoices = await self._discover_via_api(params, flow_strings)
            else:  # excel
                invoices = await self._discover_via_excel(params, flow_strings)
                
            if invoices:
                workflow.logger.info(f"✅ {discovery_config.method.upper()} discovery successful: {len(invoices)} invoices")
                return invoices
            else:
                workflow.logger.warning(f"⚠️ {discovery_config.method.upper()} discovery returned no invoices")
                
        except Exception as e:
            workflow.logger.error(f"❌ {discovery_config.method.upper()} discovery failed: {str(e)}")
        
        # Try fallback method if enabled and primary failed
        if discovery_config.excel_fallback and discovery_config.method == "api":
            workflow.logger.info("🔄 Attempting Excel fallback discovery...")
            try:
                invoices = await self._discover_via_excel(params, flow_strings)
                if invoices:
                    workflow.logger.info(f"✅ Excel fallback successful: {len(invoices)} invoices")
                    return invoices
                else:
                    workflow.logger.warning("⚠️ Excel fallback returned no invoices")
            except Exception as e:
                workflow.logger.error(f"❌ Excel fallback failed: {str(e)}")
        
        # If both methods failed or returned no invoices
        workflow.logger.error("❌ All discovery methods failed or returned no invoices")
        return []

    async def _discover_via_api(self, params: dict, flow_strings: list[str]) -> list[GdtInvoice]:
        """Discover invoices using API method."""
        workflow.logger.info(f"🔗 API Discovery: Processing {len(flow_strings)} flows")

        invoices = await workflow.execute_activity(
            discover_invoices,
            args=[
                self.session,
                params["date_range_start"],
                params["date_range_end"],
                flow_strings,
            ],
            start_to_close_timeout=timedelta(minutes=20),
            heartbeat_timeout=timedelta(minutes=3),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=5),
                maximum_interval=timedelta(minutes=5),
                maximum_attempts=8,
                backoff_coefficient=1.8,
            ),
        )
        
        workflow.logger.info(f"✅ API Discovery: Found {len(invoices)} invoices")
        return invoices

    async def _discover_via_excel(self, params: dict, flow_strings: list[str]) -> list[GdtInvoice]:
        """Discover invoices using Excel export method."""
        workflow.logger.info(f"📊 Excel Discovery: Processing {len(flow_strings)} flows")
        
        invoices = await workflow.execute_activity(
            discover_invoices_excel,
            args=[
                self.session,
                params["date_range_start"],
                params["date_range_end"],
                flow_strings,
            ],
            start_to_close_timeout=timedelta(minutes=30),  # Longer timeout for Excel downloads
            heartbeat_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=10),
                maximum_interval=timedelta(minutes=10),
                maximum_attempts=5,
                backoff_coefficient=2.0,
            ),
        )

        workflow.logger.info(f"✅ Excel Discovery: Found {len(invoices)} invoices")
        return invoices

    async def _fetch_all_invoices(self) -> None:
        """
        Fetch all invoices using smart batch processing to avoid 429 cascades.
        
        This method orchestrates the entire invoice fetching process:
        1. Process invoices in adaptive batches
        2. Retry failed invoices with smaller batches
        3. Update progress tracking throughout
        """
        workflow.logger.info(f"🚀 Starting invoice fetch: {len(self.invoices)} invoices")
        
        # Phase 1: Main batch processing
        self.results = await self._process_invoice_batches()
        
        # Phase 2: Retry failed invoices
        await self._retry_failed_invoices()
        
        workflow.logger.info(
            f"✅ Final results: {self.completed_invoices} succeeded, "
            f"{self.failed_invoices} failed"
        )

    async def _process_invoice_batches(self) -> list[InvoiceFetchResult]:
        """Process invoices in adaptive batches with intelligent sizing."""
        # Get processing mode from workflow parameters
        processing_mode = getattr(self, 'processing_mode', 'sequential')
        config = BatchConfig(processing_mode=processing_mode)
        all_results = []
        
        mode_text = "SEQUENTIAL" if processing_mode == "sequential" else "PARALLEL"
        workflow.logger.info(f"📦 Processing {len(self.invoices)} invoices in {mode_text} mode (batch size: {config.batch_size})")
        
        for i in range(0, len(self.invoices), config.batch_size):
            batch = self.invoices[i:i + config.batch_size]
            batch_num = (i // config.batch_size) + 1
            total_batches = (len(self.invoices) + config.batch_size - 1) // config.batch_size
            
            # Process single batch
            batch_results = await self._process_single_batch(batch, batch_num, total_batches)
            all_results.extend(batch_results)
            
            # Update batch configuration based on results
            config = self._update_batch_config(config, batch_results)
            
            # Wait before next batch (except for last batch)
            if i + config.batch_size < len(self.invoices):
                await asyncio.sleep(config.delay)
        
        return all_results

    async def _process_single_batch(self, batch: list[GdtInvoice], batch_num: int, total_batches: int) -> list[InvoiceFetchResult]:
        """Process a single batch of invoices - waits for ALL invoices to complete before returning."""
        workflow.logger.info(f"📦 Processing batch {batch_num}/{total_batches}: {len(batch)} invoices")
        
        # Execute all invoices in the batch - WAIT for ALL to complete
        batch_tasks = [self._fetch_single_invoice(invoice) for invoice in batch]
        workflow.logger.info(f"⏳ Waiting for all {len(batch)} invoices in batch {batch_num} to complete...")
        batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)
        
        # Analyze and log results
        batch_stats = self._analyze_batch_results(batch_results)
        self._update_progress(batch_stats)
        
        workflow.logger.info(
            f"✅ Batch {batch_num} COMPLETED - all {len(batch)} invoices finished: "
            f"{batch_stats.successes} success, {batch_stats.failures} failed "
            f"(429 errors: {batch_stats.rate_limit_errors})"
        )

        # Batch summary external emits removed (decorators handle postings)
        
        return batch_results

    def _analyze_batch_results(self, batch_results: list) -> BatchStats:
        """Analyze batch results and return statistics."""
        stats = BatchStats()
        
        for result in batch_results:
            if isinstance(result, InvoiceFetchResult):
                if result.success:
                    stats.successes += 1
                else:
                    stats.failures += 1
                    if self._is_rate_limit_error(result.error):
                        stats.rate_limit_errors += 1
            else:
                stats.failures += 1
        
        return stats

    def _is_rate_limit_error(self, error: str) -> bool:
        """Check if error is a rate limit error."""
        if not error:
            return False
        error_str = str(error).lower()
        return "429" in error_str or "rate limit" in error_str

    def _update_progress(self, batch_stats: BatchStats) -> None:
        """Update workflow progress counters."""
        self.completed_invoices += batch_stats.successes
        self.failed_invoices += batch_stats.failures

    def _update_batch_config(self, config: BatchConfig, batch_results: list) -> BatchConfig:
        """Update batch configuration based on results."""
        batch_stats = self._analyze_batch_results(batch_results)
        
        # Adjust batch size based on rate limiting
        if batch_stats.rate_limit_errors > 0:
            config.reduce_batch_size()
            workflow.logger.info(f"📉 Reduced batch size to {config.batch_size} due to rate limiting")
        elif batch_stats.successes >= len(batch_results) * 0.8:  # 80% success rate
            config.increase_batch_size()
            workflow.logger.info(f"📈 Increased batch size to {config.batch_size} due to good performance")
        
        # Adjust delay based on rate limiting
        if batch_stats.rate_limit_errors > 0:
            config.increase_delay(batch_stats.rate_limit_errors)
            workflow.logger.info(f"⏳ Extended delay to {config.delay:.1f}s due to rate limiting")
        else:
            config.reset_delay()
        
        return config

    async def _retry_failed_invoices(self) -> None:
        """Retry failed invoices in smaller batches."""
        failed_invoices = self._get_failed_invoices()
        
        if not failed_invoices:
            return
        
        workflow.logger.info(f"🔄 Retrying {len(failed_invoices)} failed invoices")
        
        retry_config = RetryConfig()
        
        for i in range(0, len(failed_invoices), retry_config.batch_size):
            retry_batch = failed_invoices[i:i + retry_config.batch_size]
            retry_batch_num = (i // retry_config.batch_size) + 1
            total_retry_batches = (len(failed_invoices) + retry_config.batch_size - 1) // retry_config.batch_size
            
            await self._process_retry_batch(retry_batch, retry_batch_num, total_retry_batches)
            
            # Wait before next retry batch
            if i + retry_config.batch_size < len(failed_invoices):
                await asyncio.sleep(retry_config.delay)

    def _get_failed_invoices(self) -> list[GdtInvoice]:
        """Get list of invoices that failed in the main processing."""
        failed_invoices = []
        
        for i, result in enumerate(self.results):
            if isinstance(result, InvoiceFetchResult) and not result.success:
                failed_invoices.append(self.invoices[i])
            elif not isinstance(result, InvoiceFetchResult):
                failed_invoices.append(self.invoices[i])
        
        return failed_invoices

    async def _process_retry_batch(self, retry_batch: list[GdtInvoice], batch_num: int, total_batches: int) -> None:
        """Process a single retry batch - waits for ALL invoices to complete before returning."""
        workflow.logger.info(f"🔄 Retry batch {batch_num}/{total_batches}: {len(retry_batch)} invoices")
        
        # Execute retry batch - WAIT for ALL to complete
        retry_tasks = [self._fetch_single_invoice(invoice) for invoice in retry_batch]
        workflow.logger.info(f"⏳ Waiting for all {len(retry_batch)} invoices in retry batch {batch_num} to complete...")
        retry_results = await asyncio.gather(*retry_tasks, return_exceptions=True)
        
        # Update results for successful retries
        retry_successes = 0
        retry_failures = 0
        
        for j, retry_result in enumerate(retry_results):
            original_index = self.invoices.index(retry_batch[j])
            
            if isinstance(retry_result, InvoiceFetchResult) and retry_result.success:
                self.results[original_index] = retry_result
                retry_successes += 1
                self.completed_invoices += 1
                self.failed_invoices -= 1
            else:
                retry_failures += 1
        
        workflow.logger.info(f"✅ Retry batch {batch_num} COMPLETED - all {len(retry_batch)} invoices finished: {retry_successes} success, {retry_failures} failed")

    async def _fetch_single_invoice(self, invoice: GdtInvoice) -> InvoiceFetchResult:
        """
        Fetch single invoice with conservative retry logic for batch processing.

        Conservative Retry Policy for Batch Processing:
        - initial_interval=5s: Longer initial delay to avoid retry storms
        - maximum_interval=30s: Shorter max wait to fail fast in batches
        - maximum_attempts=5: Fewer attempts to avoid blocking batch
        - backoff_coefficient=2.0: Standard exponential backoff

        Why this works better with batch processing:
        - Longer initial delay prevents immediate retry storms
        - Fewer attempts means failed invoices don't block the batch
        - Shorter max wait allows batch to complete faster
        - Failed invoices can be retried in subsequent batches
        """
        try:
            # Perform fetch (activity will POST full JSON via decorator)
            result = await workflow.execute_activity(
                fetch_invoice,
                args=[invoice, self.session],
                start_to_close_timeout=timedelta(minutes=5),  # Shorter timeout for batch processing
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=5),  # Longer initial delay
                    maximum_interval=timedelta(seconds=30),  # Shorter max wait
                    maximum_attempts=5,  # Fewer attempts
                    backoff_coefficient=2.0,  # Standard backoff
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
