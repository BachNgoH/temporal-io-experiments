#!/usr/bin/env python3
"""
Test script for GDT invoice import with real credentials.

Usage:
    python tests/test_gdt_flows.py --tasks 1    # Crawl last 1 month
    python tests/test_gdt_flows.py --tasks 10   # Crawl last 10 months
    python tests/test_gdt_flows.py --tasks 3 --flows ban_ra_dien_tu mua_vao_dien_tu  # Specific flows only
"""

import argparse
import asyncio
import httpx
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta

# ============================================================================
# Configuration
# ============================================================================

API_BASE_URL = "http://localhost:8000"

# Real GDT credentials
GDT_USERNAME = "0317530616"
GDT_PASSWORD = "DlzGy00@"

# Company info (based on username which is tax code)
COMPANY_ID = "0317530616"
COMPANY_NAME = "PATEDELI"

# All available flows
ALL_FLOWS = [
    "ban_ra_dien_tu",
    "ban_ra_may_tinh_tien",
    "mua_vao_dien_tu",
    "mua_vao_may_tinh_tien",
]


# ============================================================================
# Helper Functions
# ============================================================================

def get_month_ranges(num_months: int) -> list[tuple[str, str]]:
    """
    Generate date ranges for the last N months (excluding current month).

    Example:
        If today is 2025-10-12 and num_months=3:
        Returns: [
            ("2025-09-01", "2025-09-30"),  # Last month
            ("2025-08-01", "2025-08-31"),  # 2 months ago
            ("2025-07-01", "2025-07-31"),  # 3 months ago
        ]
    """
    today = datetime.now()

    # Start from last month (not current month)
    current_month_start = today.replace(day=1)
    last_month_start = current_month_start - relativedelta(months=1)

    ranges = []
    for i in range(num_months):
        # Calculate month
        month_start = last_month_start - relativedelta(months=i)
        month_end = month_start + relativedelta(months=1) - timedelta(days=1)

        # Format as YYYY-MM-DD
        start_str = month_start.strftime("%Y-%m-%d")
        end_str = month_end.strftime("%Y-%m-%d")

        ranges.append((start_str, end_str))

    return ranges


async def start_workflow(
    company_id: str,
    company_name: str,
    username: str,
    password: str,
    date_range_start: str,
    date_range_end: str,
    flows: list[str],
) -> dict:
    """Start a GDT invoice import workflow."""

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{API_BASE_URL}/api/tasks/start",
            json={
                "task_type": "gdt_invoice_import",
                "task_params": {
                    "company_id": company_id,
                    "company_name": company_name,
                    "credentials": {
                        "username": username,
                        "password": password,
                    },
                    "date_range_start": date_range_start,
                    "date_range_end": date_range_end,
                    "flows": flows,
                },
            },
        )
        response.raise_for_status()
        return response.json()


async def check_workflow_status(workflow_id: str) -> dict:
    """Check workflow status."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{API_BASE_URL}/api/tasks/{workflow_id}/status"
        )
        response.raise_for_status()
        return response.json()


async def wait_for_workflow(workflow_id: str, max_wait_seconds: int = 600) -> dict:
    """Wait for workflow to complete."""
    print(f"   Waiting for workflow {workflow_id}...")

    start_time = datetime.now()
    while True:
        status = await check_workflow_status(workflow_id)

        if status["status"] in ["completed", "failed", "cancelled"]:
            return status

        # Check timeout
        elapsed = (datetime.now() - start_time).total_seconds()
        if elapsed > max_wait_seconds:
            print(f"   ⏱️  Timeout waiting for workflow {workflow_id}")
            return status

        # Print progress if available
        if status.get("progress"):
            progress = status["progress"]
            print(
                f"   Progress: {progress.get('completed_invoices', 0)}/{progress.get('total_invoices', 0)} invoices"
            )

        # Wait before checking again
        await asyncio.sleep(5)


# ============================================================================
# Main Test Functions
# ============================================================================

async def run_sequential_test(num_tasks: int, flows: list[str]):
    """Run tasks sequentially (one at a time)."""
    print(f"\n{'='*80}")
    print(f"SEQUENTIAL TEST: {num_tasks} tasks")
    print(f"Flows: {flows}")
    print(f"{'='*80}\n")

    month_ranges = get_month_ranges(num_tasks)

    results = []
    for idx, (start_date, end_date) in enumerate(month_ranges, 1):
        print(f"\n[Task {idx}/{num_tasks}] Starting workflow for {start_date} to {end_date}")

        try:
            # Start workflow
            start_result = await start_workflow(
                company_id=COMPANY_ID,
                company_name=COMPANY_NAME,
                username=GDT_USERNAME,
                password=GDT_PASSWORD,
                date_range_start=start_date,
                date_range_end=end_date,
                flows=flows,
            )

            workflow_id = start_result["workflow_id"]
            print(f"   ✅ Started: {workflow_id}")

            # Wait for completion
            final_status = await wait_for_workflow(workflow_id, max_wait_seconds=600)

            if final_status["status"] == "completed":
                result = final_status.get("result", {})
                print(f"   ✅ Completed: {result.get('completed_invoices', 0)} invoices")
                results.append({
                    "workflow_id": workflow_id,
                    "date_range": f"{start_date} to {end_date}",
                    "status": "completed",
                    "invoices": result.get("completed_invoices", 0),
                })
            else:
                print(f"   ❌ Failed: {final_status.get('error', 'Unknown error')}")
                results.append({
                    "workflow_id": workflow_id,
                    "date_range": f"{start_date} to {end_date}",
                    "status": "failed",
                    "error": final_status.get("error"),
                })

        except Exception as e:
            print(f"   ❌ Error: {str(e)}")
            results.append({
                "date_range": f"{start_date} to {end_date}",
                "status": "error",
                "error": str(e),
            })

    # Print summary
    print(f"\n{'='*80}")
    print("SUMMARY")
    print(f"{'='*80}")

    total_invoices = 0
    successful = 0
    failed = 0

    for result in results:
        if result["status"] == "completed":
            successful += 1
            total_invoices += result["invoices"]
            print(f"✅ {result['date_range']}: {result['invoices']} invoices")
        else:
            failed += 1
            print(f"❌ {result['date_range']}: {result.get('error', 'Failed')}")

    print(f"\nTotal: {successful} successful, {failed} failed, {total_invoices} invoices")


async def run_parallel_test(num_tasks: int, flows: list[str]):
    """Run tasks in parallel (all at once)."""
    print(f"\n{'='*80}")
    print(f"PARALLEL TEST: {num_tasks} tasks")
    print(f"Flows: {flows}")
    print(f"{'='*80}\n")

    month_ranges = get_month_ranges(num_tasks)

    # Start all workflows in parallel
    print(f"Starting {num_tasks} workflows in parallel...")

    workflow_ids = []
    for idx, (start_date, end_date) in enumerate(month_ranges, 1):
        try:
            start_result = await start_workflow(
                company_id=COMPANY_ID,
                company_name=COMPANY_NAME,
                username=GDT_USERNAME,
                password=GDT_PASSWORD,
                date_range_start=start_date,
                date_range_end=end_date,
                flows=flows,
            )

            workflow_id = start_result["workflow_id"]
            workflow_ids.append({
                "workflow_id": workflow_id,
                "date_range": f"{start_date} to {end_date}",
            })
            print(f"   [{idx}/{num_tasks}] ✅ Started: {workflow_id} ({start_date} to {end_date})")

        except Exception as e:
            print(f"   [{idx}/{num_tasks}] ❌ Failed to start: {str(e)}")

    # Wait for all to complete
    print(f"\nWaiting for {len(workflow_ids)} workflows to complete...")

    results = []
    for wf_info in workflow_ids:
        try:
            final_status = await wait_for_workflow(wf_info["workflow_id"], max_wait_seconds=600)

            if final_status["status"] == "completed":
                result = final_status.get("result", {})
                results.append({
                    "workflow_id": wf_info["workflow_id"],
                    "date_range": wf_info["date_range"],
                    "status": "completed",
                    "invoices": result.get("completed_invoices", 0),
                })
            else:
                results.append({
                    "workflow_id": wf_info["workflow_id"],
                    "date_range": wf_info["date_range"],
                    "status": "failed",
                    "error": final_status.get("error"),
                })

        except Exception as e:
            results.append({
                "workflow_id": wf_info["workflow_id"],
                "date_range": wf_info["date_range"],
                "status": "error",
                "error": str(e),
            })

    # Print summary
    print(f"\n{'='*80}")
    print("SUMMARY")
    print(f"{'='*80}")

    total_invoices = 0
    successful = 0
    failed = 0

    for result in results:
        if result["status"] == "completed":
            successful += 1
            total_invoices += result["invoices"]
            print(f"✅ {result['date_range']}: {result['invoices']} invoices")
        else:
            failed += 1
            print(f"❌ {result['date_range']}: {result.get('error', 'Failed')}")

    print(f"\nTotal: {successful} successful, {failed} failed, {total_invoices} invoices")


# ============================================================================
# CLI
# ============================================================================

async def main():
    parser = argparse.ArgumentParser(
        description="Test GDT invoice import with real credentials"
    )
    parser.add_argument(
        "--tasks",
        type=int,
        default=1,
        help="Number of tasks to create (1 = last month, 10 = last 10 months)",
    )
    parser.add_argument(
        "--flows",
        nargs="+",
        choices=ALL_FLOWS,
        default=ALL_FLOWS,
        help="Invoice flows to crawl (default: all flows)",
    )
    parser.add_argument(
        "--mode",
        choices=["sequential", "parallel"],
        default="sequential",
        help="Execution mode (sequential or parallel)",
    )

    args = parser.parse_args()

    print("\n" + "="*80)
    print("GDT INVOICE IMPORT TEST")
    print("="*80)
    print(f"Company ID: {COMPANY_ID}")
    print(f"Username: {GDT_USERNAME}")
    print(f"Number of tasks: {args.tasks}")
    print(f"Flows: {args.flows}")
    print(f"Mode: {args.mode}")

    # Check API is running
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{API_BASE_URL}/")
            print(f"API Status: ✅ {response.json()['status']}")
    except Exception as e:
        print(f"❌ API not available: {str(e)}")
        print("Make sure the API is running: docker-compose up -d")
        return

    # Run tests
    if args.mode == "sequential":
        await run_sequential_test(args.tasks, args.flows)
    else:
        await run_parallel_test(args.tasks, args.flows)


if __name__ == "__main__":
    asyncio.run(main())
