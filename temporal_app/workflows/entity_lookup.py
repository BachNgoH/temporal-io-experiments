"""Entity Lookup Workflow - Clean, modular workflow using existing activities.

- Supports two modes:
  - tax_code: Extract from GDT portal (extract_from_gdt)
  - company_name: Use thuvienphapluat-only search (search_by_company_name_only)

Follows project conventions and avoids referencing non-existent modules.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


# Import activities (safe for worker process)
with workflow.unsafe.imports_passed_through():
    from temporal_app.activities.entity_lookup import (
        extract_from_gdt,
        search_by_company_name_only,
    )


@workflow.defn
class EntityLookupWorkflow:
    """Workflow for entity/company lookup using small, focused activities."""

    def __init__(self) -> None:
        self.start_time_ms = 0
        self.extraction_folder = ""

    @workflow.run
    async def run(self, params: dict) -> dict:
        """Entry point.

        Expected params:
            - search_term: str (tax code or company name)
            - search_type: 'tax_code' | 'company_name' (default: 'tax_code')
            - list_mode: bool (default: False)
        """
        # Use deterministic workflow clock
        self.start_time_ms = int(workflow.now().timestamp() * 1000)

        search_term = params.get("search_term", "").strip()
        search_type = params.get("search_type", "tax_code").strip() or "tax_code"
        list_mode = bool(params.get("list_mode", False))

        if not search_term:
            return {"status": "failed", "error": "search_term is required"}

        workflow.logger.info(
            f"Entity lookup start: term='{search_term}', type='{search_type}', list_mode={list_mode}"
        )

        # 1) Defer extraction folder creation to activities for portability
        self.extraction_folder = ""

        try:
            # 2) Execute a small number of focused activities
            if search_type == "company_name":
                result = await self._do_company_name_search(search_term, list_mode)
            else:  # tax_code (default)
                result = await self._do_tax_code_extraction(search_term)

            # 3) Build compact response
            processing_time = (int(workflow.now().timestamp() * 1000) - self.start_time_ms) / 1000.0
            workflow.logger.info(f"Entity lookup completed in {processing_time:.2f}s")
            return {
                "status": "completed",
                "search_term": search_term,
                "search_type": search_type,
                "list_mode": list_mode,
                "processing_time": processing_time,
                "result": result,
            }

        except Exception as e:
            processing_time = (int(workflow.now().timestamp() * 1000) - self.start_time_ms) / 1000.0
            workflow.logger.error(f"Entity lookup failed: {str(e)}")
            return {
                "status": "failed",
                "error": str(e),
                "search_term": search_term,
                "search_type": search_type,
                "processing_time": processing_time,
            }

    # --------------------------- helpers (workflow code) ---------------------------

    def _prepare_extraction_folder(self, search_term: str) -> str:
        """Deprecated: activities create folders when needed."""
        return ""

    async def _do_tax_code_extraction(self, search_term: str) -> dict:
        """Call GDT extraction activity for tax code lookups."""
        return await workflow.execute_activity(
            extract_from_gdt,
            args=[search_term, self.extraction_folder],
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=10),
                maximum_interval=timedelta(minutes=2),
                maximum_attempts=3,
                backoff_coefficient=2.0,
            ),
        )

    async def _do_company_name_search(self, search_term: str, list_mode: bool) -> dict:
        """Call name-only activity for company name lookups."""
        # Minimal request object to avoid cross-module class coupling
        request = {"search_term": search_term, "search_type": "company_name", "list_mode": list_mode}
        return await workflow.execute_activity(
            search_by_company_name_only,
            args=[request, self.extraction_folder],
            start_to_close_timeout=timedelta(minutes=3),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=5),
                maximum_interval=timedelta(minutes=1),
                maximum_attempts=2,
                backoff_coefficient=2.0,
            ),
        )

    @workflow.query
    def get_progress(self) -> dict:
        """Lightweight progress query."""
        if self.start_time_ms > 0:
            elapsed_ms = int(workflow.now().timestamp() * 1000) - self.start_time_ms
        else:
            elapsed_ms = 0
        return {"elapsed_ms": elapsed_ms, "extraction_folder": self.extraction_folder}

    @workflow.signal
    async def cancel_workflow(self) -> None:
        workflow.logger.info("Entity lookup cancellation requested")
        raise Exception("Workflow cancelled by user")


