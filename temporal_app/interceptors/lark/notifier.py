"""Worker interceptors that notify Lark on workflow lifecycle and invoice failures.

Notes:
- Workflow interceptor keeps I/O minimal and outside replay-sensitive code by
  only sending on first-run (not on replay) via the info flag.
- Activity interceptor watches `fetch_invoice` failures to report series/number.
"""

from __future__ import annotations

import logging
from typing import Any

from temporalio import activity, workflow
from temporalio.worker import (
    Interceptor,
    WorkflowInboundInterceptor,
    ActivityInboundInterceptor,
    WorkflowInterceptorClassInput,
)

from app.config import settings
from temporal_app.interceptors.lark.client import LarkWebhookBot


logger = logging.getLogger(__name__)


class LarkNotifierInterceptor(Interceptor):
    def __init__(self) -> None:
        self.bot = LarkWebhookBot(settings.lark_webhook_url)

    def workflow_interceptor_class(self, input: WorkflowInterceptorClassInput):
        return lambda next: _LarkWorkflowInbound(next, self.bot)

    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return _LarkActivityInbound(next, self.bot)


class _LarkWorkflowInbound(WorkflowInboundInterceptor):
    def __init__(self, next: WorkflowInboundInterceptor, bot: LarkWebhookBot) -> None:
        super().__init__(next)
        self.bot = bot

    async def execute_workflow(self, input: Any) -> Any:
        # Notify start via activity to keep workflow deterministic
        info = workflow.info()
        try:
            params = {}
            args = getattr(input, "args", ())
            if isinstance(args, (list, tuple)) and args and isinstance(args[0], dict):
                params = args[0]
            company_id = params.get("company_id") if isinstance(params, dict) else None
            await workflow.execute_activity(
                "lark.notify",
                {
                    "event": "workflow_started",
                    "fields": {
                        "Workflow": info.workflow_type,
                        "WorkflowID": info.workflow_id,
                        "RunID": info.run_id,
                        "CompanyID": company_id or "",
                    },
                },
                start_to_close_timeout=workflow.timedelta(seconds=10),
            )
        except Exception:
            # ignore notification errors
            pass

        try:
            result = await super().execute_workflow(input)
        except Exception as e:
            # Notify failure
            try:
                await workflow.execute_activity(
                    "lark.notify",
                    {
                        "event": "workflow_failed",
                        "fields": {
                            "Workflow": info.workflow_type,
                            "WorkflowID": info.workflow_id,
                            "RunID": info.run_id,
                            "Error": str(e)[:500],
                        },
                    },
                    start_to_close_timeout=workflow.timedelta(seconds=10),
                )
            except Exception:
                pass
            raise

        # Notify completion with summary-like fields if available
        try:
            summary_fields = {
                "Workflow": info.workflow_type,
                "WorkflowID": info.workflow_id,
                "RunID": info.run_id,
            }
            if isinstance(result, dict):
                for k in ("total_invoices", "completed_invoices", "failed_invoices", "success_rate"):
                    if k in result:
                        summary_fields[k] = result[k]
                if "company_id" in result:
                    summary_fields["CompanyID"] = result["company_id"]
            await workflow.execute_activity(
                "lark.notify",
                {"event": "workflow_completed", "fields": summary_fields},
                start_to_close_timeout=workflow.timedelta(seconds=10),
            )
        except Exception:
            pass

        return result


class _LarkActivityInbound(ActivityInboundInterceptor):
    def __init__(self, next: ActivityInboundInterceptor, bot: LarkWebhookBot) -> None:
        super().__init__(next)
        self.bot = bot

    async def execute_activity(self, input: Any) -> Any:
        try:
            result = await super().execute_activity(input)
        except Exception as e:
            # Activity failure path (e.g., network error) -> send alert with context
            if self.bot.is_configured():
                try:
                    info = activity.info()
                    await self.bot.send_card(
                        title="Activity Failed",
                        fields={
                            "Activity": info.activity_type,
                            "WorkflowID": info.workflow_execution.workflow_id,
                            "WorkflowType": info.workflow_execution.workflow_type,
                            "Attempt": info.attempt,
                            "Error": str(e)[:500],
                        },
                        severity="HIGH",
                    )
                except Exception:
                    pass
            raise

        # Inspect successful result for fetch failures that are modeled as success=False
        try:
            if self.bot.is_configured():
                info = activity.info()
                if info.activity_type.endswith("fetch_invoice"):
                    series = None
                    number = None
                    err = None
                    success_val = None

                    # Dataclass result
                    try:
                        success_val = getattr(result, "success", None)
                        data = getattr(result, "data", None) or {}
                        err = getattr(result, "error", None)
                        metadata = data.get("metadata") if isinstance(data, dict) else None
                        number = data.get("invoice_number") if isinstance(data, dict) else None
                        if isinstance(metadata, dict):
                            series = metadata.get("khhdon")
                    except Exception:
                        pass

                    # Dict result fallback
                    if success_val is None and isinstance(result, dict):
                        success_val = result.get("success")
                        ddata = result.get("data") or {}
                        if isinstance(ddata, dict):
                            metadata = ddata.get("metadata") or {}
                            series = metadata.get("khhdon")
                            number = ddata.get("invoice_number")
                        err = result.get("error") or err

                    if success_val is False:
                        await self.bot.send_card(
                            title="Invoice Fetch Failed",
                            fields={
                                "Series": series or "",
                                "Number": number or "",
                                "Error": str(err)[:300] if err else "fetch failed",
                            },
                            severity="MEDIUM",
                        )
                # Discovery summary notification (number found + any flow failed)
                if info.activity_type.endswith("discover_invoices") or info.activity_type.endswith("discover_invoices_excel"):
                    try:
                        total = None
                        failed_flows = None
                        # Pydantic model
                        if hasattr(result, "invoice_count"):
                            total = getattr(result, "invoice_count", None)
                            failed_flows = getattr(result, "failed_flows", None)
                        # Dict
                        elif isinstance(result, dict):
                            total = result.get("invoice_count")
                            failed_flows = result.get("failed_flows")
                        if total is not None:
                            await self.bot.send_card(
                                title="Discovery Completed",
                                fields={
                                    "InvoicesFound": total,
                                    "AnyFlowFailed": bool(failed_flows) if failed_flows is not None else False,
                                },
                                severity="LOW" if not failed_flows else "CRITICAL",
                            )
                    except Exception:
                        pass
        except Exception:
            pass

        return result


