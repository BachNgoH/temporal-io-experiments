"""Event activities for workflow lifecycle hooks.

These activities are intentionally minimal and idempotent-friendly. They accept
typed DTOs and only perform side effects (logging, persistence hooks later).
"""

import os
import httpx
from temporalio import activity

from temporal_app.models import (
    DiscoveryEventV1,
    FetchEventV1,
    WorkflowEndEventV1,
    WorkflowStartEventV1,
)


@activity.defn
async def emit_workflow_started(event: WorkflowStartEventV1) -> None:
    """Emit workflow start event.

    Safe to retry; consumers should dedupe by (workflow_id, run_id).
    """
    activity.logger.info(
        "🟢 Workflow STARTED | wf=%s run=%s company=%s range=%s..%s flows=%s ref=%s",
        event.workflow_id,
        event.run_id,
        event.company_id,
        event.date_range_start,
        event.date_range_end,
        ",".join(event.flows or []),
        event.run_ref or "-",
    )
    await _post_event(
        event_name="workflow.started",
        payload={
            "workflow_id": event.workflow_id,
            "run_id": event.run_id,
            "company_id": event.company_id,
            "date_range_start": event.date_range_start,
            "date_range_end": event.date_range_end,
            "flows": event.flows,
            "run_ref": event.run_ref,
        },
    )


@activity.defn
async def emit_workflow_completed(event: WorkflowEndEventV1) -> None:
    """Emit workflow completion event with a compact summary.

    Safe to retry; consumers should dedupe by (workflow_id, run_id).
    """
    summary = event.summary
    activity.logger.info(
        "🔴 Workflow %s | wf=%s run=%s company=%s total=%d done=%d fail=%d rate=%.2f%% ref=%s",
        event.status,
        event.workflow_id,
        event.run_id,
        event.company_id,
        summary.total_invoices,
        summary.completed_invoices,
        summary.failed_invoices,
        summary.success_rate,
        event.result_ref or "-",
    )
    await _post_event(
        event_name="workflow.completed",
        payload={
            "status": event.status,
            "workflow_id": event.workflow_id,
            "run_id": event.run_id,
            "company_id": event.company_id,
            "summary": {
                "total_invoices": summary.total_invoices,
                "completed_invoices": summary.completed_invoices,
                "failed_invoices": summary.failed_invoices,
                "success_rate": summary.success_rate,
            },
            "result_ref": event.result_ref,
        },
    )


@activity.defn
async def emit_discovery_event(event: DiscoveryEventV1) -> None:
    """Emit discovery phase event (start or end)."""
    if event.phase == "start":
        activity.logger.info(
            "🔍 Discovery START | company=%s method=%s flows=%s",
            event.company_id,
            event.method,
            ",".join(event.flows or []),
        )
    else:
        activity.logger.info(
            "📊 Discovery END | company=%s method=%s flows=%s invoices=%s",
            event.company_id,
            event.method,
            ",".join(event.flows or []),
            str(event.invoice_count) if event.invoice_count is not None else "-",
        )
        await _post_event(
            event_name="discovery.completed",
            payload={
                "company_id": event.company_id,
                "method": event.method,
                "flows": event.flows,
                "invoice_count": event.invoice_count,
            },
        )


@activity.defn
async def emit_fetch_event(event: FetchEventV1) -> None:
    """Emit per-invoice fetch lifecycle event (start or end)."""
    if event.phase == "start":
        activity.logger.info(
            "📄 Fetch START | company=%s invoice=%s flow=%s endpoint=%s",
            event.company_id,
            event.invoice_id,
            event.flow_type or "-",
            event.endpoint_kind or "-",
        )
    else:
        if event.success:
            activity.logger.info(
                "✅ Fetch END   | company=%s invoice=%s flow=%s endpoint=%s",
                event.company_id,
                event.invoice_id,
                event.flow_type or "-",
                event.endpoint_kind or "-",
            )
            await _post_event(
                event_name="fetch.completed",
                payload={
                    "company_id": event.company_id,
                    "invoice_id": event.invoice_id,
                    "flow_type": event.flow_type,
                    "endpoint_kind": event.endpoint_kind,
                    "success": True,
                },
            )
        else:
            activity.logger.warning(
                "❌ Fetch END   | company=%s invoice=%s flow=%s endpoint=%s error=%s",
                event.company_id,
                event.invoice_id,
                event.flow_type or "-",
                event.endpoint_kind or "-",
                (event.error or "-")[:200],
            )
            await _post_event(
                event_name="fetch.completed",
                payload={
                    "company_id": event.company_id,
                    "invoice_id": event.invoice_id,
                    "flow_type": event.flow_type,
                    "endpoint_kind": event.endpoint_kind,
                    "success": False,
                    "error": event.error,
                },
            )


@activity.defn
async def emit_fetch_batch_event(batch_num: int, total_batches: int, successes: int, failures: int) -> None:
    """Emit a summary event per processed batch to reduce event volume."""
    activity.logger.info(
        "📦 Batch %s/%s summary: %s success, %s failed",
        batch_num,
        total_batches,
        successes,
        failures,
    )
    await _post_event(
        event_name="fetch.batch_completed",
        payload={
            "batch_num": batch_num,
            "total_batches": total_batches,
            "successes": successes,
            "failures": failures,
        },
    )

async def _post_event(event_name: str, payload: dict) -> None:
    """POST event envelope to internal webhook endpoint.

    Endpoint can be overridden by env EVENT_WEBHOOK_URL; defaults to local FastAPI.
    """
    url = os.getenv("EVENT_WEBHOOK_URL", "http://localhost:8000/internal/webhooks")
    envelope = {
        "event_id": activity.info().activity_id,
        "event_name": event_name,
        "payload": payload,
        "workflow_id": activity.info().task_token.decode(errors="ignore") if hasattr(activity.info(), "task_token") else None,
        "run_id": os.getenv("TEMP_RUN_ID", None),
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json=envelope)
    except Exception as e:
        activity.logger.warning(f"Failed to POST event {event_name} to {url}: {str(e)}")


