"""Temporal Worker - Runs on Compute Engine (base) or Cloud Run Jobs (burst)."""

import asyncio
import logging
import os
import signal
import sys
from datetime import timedelta

from temporalio.client import Client, TLSConfig
from temporalio.worker import Worker

from app.config import settings
from temporal_app.activities import (
    discover_invoices,
    discover_invoices_excel,
    fetch_invoice,
    login_to_gdt,
)
from temporal_app.workflows import GdtInvoiceImportWorkflow

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class TemporalWorker:
    """
    Temporal Worker for hybrid deployment.

    Deployment modes:
    1. BASE MODE (Compute Engine): Always-on workers (2-3 instances)
       - Handles steady-state workload
       - Polls continuously
       - Low cost baseline

    2. BURST MODE (Cloud Run Jobs): On-demand workers (0-100 instances)
       - Triggered when queue depth is high
       - Processes backlog quickly
       - Exits when queue empty (cost = $0)
    """

    def __init__(self) -> None:
        self.client: Client | None = None
        self.worker: Worker | None = None
        self.shutdown_event = asyncio.Event()
        self.is_burst_mode = os.getenv("WORKER_MODE", "base") == "burst"

    async def start(self) -> None:
        """Start the worker and begin polling."""
        mode = "BURST" if self.is_burst_mode else "BASE"
        logger.info(f"🚀 Starting Temporal Worker ({mode} MODE)...")
        logger.info(f"Temporal Host: {settings.temporal_host}")
        logger.info(f"Task Queue: {settings.temporal_task_queue}")

        # Connect to Temporal Server
        self.client = await self._connect_to_temporal()

        # Create worker with different settings for base vs burst
        concurrency_settings = self._get_concurrency_settings()

        self.worker = Worker(
            self.client,
            task_queue=settings.temporal_task_queue,
            # Register workflows
            workflows=[
                GdtInvoiceImportWorkflow,
                # Add future workflows here:
                # GdtTaxReportSyncWorkflow,
                # DataPipelineWorkflow,
            ],
            # Register activities
            activities=[
                login_to_gdt,
                discover_invoices,
                discover_invoices_excel,
                fetch_invoice,
                # Add future activities here
            ],
            # Concurrency settings (different for base vs burst)
            max_concurrent_workflow_tasks=concurrency_settings["workflows"],
            max_concurrent_activities=concurrency_settings["activities"],
            # Graceful shutdown
            graceful_shutdown_timeout=timedelta(seconds=30),
        )

        logger.info("✅ Worker initialized successfully")
        logger.info(
            f"📊 Concurrency: {concurrency_settings['workflows']} workflows, "
            f"{concurrency_settings['activities']} activities"
        )
        logger.info("🔄 Starting to poll for tasks...")

        # Setup signal handlers for graceful shutdown
        self._setup_signal_handlers()

        try:
            if self.is_burst_mode:
                # Burst mode: Poll for work, exit when queue empty
                await self._run_burst_mode()
            else:
                # Base mode: Run continuously
                await self.worker.run()

        except asyncio.CancelledError:
            logger.info("Worker cancelled, shutting down...")
        except Exception as e:
            logger.error(f"Worker error: {e}")
            raise
        finally:
            await self.shutdown()

    def _get_concurrency_settings(self) -> dict:
        """
        Get concurrency settings based on deployment mode.

        BASE MODE (Compute Engine):
        - Higher concurrency per worker for better task isolation
        - Multiple workers always running
        - Total: 2 workers × 100 = 200 concurrent activities

        BURST MODE (Cloud Run Jobs):
        - Maximum concurrency per worker for peak performance
        - Many workers for short duration
        - Total: 50 workers × 50 = 2500 concurrent activities

        Improved settings for better task isolation:
        - More activities per worker = better resource utilization
        - Higher workflow concurrency = multiple tasks can run simultaneously
        - No artificial limits per workflow = full parallelism
        """
        if self.is_burst_mode:
            return {
                "workflows": 200,  # More workflows per worker
                "activities": 50,  # Higher activity concurrency
            }
        else:
            return {
                "workflows": 100,  # More workflows per worker
                "activities": 20,  # Higher activity concurrency
            }

    async def _run_burst_mode(self) -> None:
        """
        Run worker in burst mode.

        Strategy:
        1. Poll for tasks
        2. Process available work
        3. Check queue depth periodically
        4. Exit when queue is empty (gracefully)
        """
        logger.info("🚀 Burst mode: Processing tasks until queue empty...")

        # Run worker in background task
        worker_task = asyncio.create_task(self.worker.run())

        # Monitor queue depth
        idle_checks = 0
        max_idle_checks = 3  # Exit after 3 consecutive empty checks

        while not self.shutdown_event.is_set():
            await asyncio.sleep(30)  # Check every 30 seconds

            # In production: Query Temporal for queue depth
            # For now: Simple heuristic - check if worker is idle
            # If idle for 3 checks (90 seconds), assume queue is empty

            # TODO: Add actual queue depth check
            # queue_depth = await self._get_queue_depth()
            # if queue_depth == 0:
            #     idle_checks += 1
            # else:
            #     idle_checks = 0

            # Placeholder for demo
            logger.info("📊 Checking queue depth...")

            # For now: Just run for a while then exit
            # In production: Exit when queue actually empty
            # Uncomment below for auto-exit:
            # if idle_checks >= max_idle_checks:
            #     logger.info("✅ Queue empty, shutting down burst worker")
            #     break

        # Cancel worker task
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass

        logger.info("✅ Burst worker completed")

    async def _connect_to_temporal(self) -> Client:
        """Connect to Temporal Server (local or cloud)."""
        if settings.temporal_use_cloud:
            # Temporal Cloud connection
            logger.info("Connecting to Temporal Cloud...")

            if not settings.temporal_cert_path or not settings.temporal_key_path:
                raise ValueError("Temporal Cloud requires cert and key paths")

            with open(settings.temporal_cert_path, "rb") as cert_file:
                client_cert = cert_file.read()

            with open(settings.temporal_key_path, "rb") as key_file:
                client_key = key_file.read()

            client = await Client.connect(
                settings.temporal_host,
                namespace=settings.temporal_namespace,
                tls=TLSConfig(
                    client_cert=client_cert,
                    client_private_key=client_key,
                ),
            )
        else:
            # Self-hosted Temporal (Compute Engine)
            logger.info("Connecting to self-hosted Temporal...")

            client = await Client.connect(
                settings.temporal_host,
                namespace=settings.temporal_namespace,
            )

        logger.info(f"✅ Connected to Temporal: {settings.temporal_host}")
        return client

    def _setup_signal_handlers(self) -> None:
        """Setup graceful shutdown on SIGTERM/SIGINT."""

        def signal_handler(sig: int, frame: object) -> None:
            logger.info(f"Received signal {sig}, initiating graceful shutdown...")
            asyncio.create_task(self._trigger_shutdown())

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

    async def _trigger_shutdown(self) -> None:
        """Trigger graceful shutdown."""
        self.shutdown_event.set()

        if self.worker:
            logger.info("Shutting down worker...")
            # Worker will finish current tasks before shutting down
            # (up to graceful_shutdown_timeout)

    async def shutdown(self) -> None:
        """Cleanup on shutdown."""
        if self.client:
            await self.client.close()
            logger.info("✅ Temporal client closed")


async def main() -> None:
    """
    Main entry point for worker.

    Environment variables:
    - WORKER_MODE: "base" or "burst" (default: base)
    - TEMPORAL_HOST: Temporal server address (default: localhost:7233)
    - TEMPORAL_TASK_QUEUE: Task queue name (default: default-task-queue)
    - TEMPORAL_USE_CLOUD: Use Temporal Cloud (default: false)
    - TEMPORAL_CERT_PATH: Path to client cert (for Temporal Cloud)
    - TEMPORAL_KEY_PATH: Path to client key (for Temporal Cloud)

    Deployment:
    1. BASE MODE (Compute Engine):
       docker-compose up -d temporal-worker

    2. BURST MODE (Cloud Run Jobs):
       gcloud run jobs execute temporal-worker-burst --task-count=50
    """
    worker = TemporalWorker()

    try:
        await worker.start()
    except KeyboardInterrupt:
        logger.info("Worker interrupted by user")
    except Exception as e:
        logger.error(f"Worker failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
