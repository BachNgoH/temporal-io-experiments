#!/usr/bin/env python3
"""
STRESS TEST script for GDT invoice import with real credentials.

Usage:
    python tests/test_gdt_flows.py                    # DEFAULT: 12 concurrent tasks (full year)
    python tests/test_gdt_flows.py --stress-test      # FORCE: 12 concurrent tasks (full year)
    python tests/test_gdt_flows.py --tasks 1          # Single task test
    python tests/test_gdt_flows.py --tasks 6          # 6 concurrent tasks (half year)
    python tests/test_gdt_flows.py --mode sequential  # Sequential processing (not stress test)
    python tests/test_gdt_flows.py --flows ban_ra_dien_tu mua_vao_dien_tu  # Specific flows only

STRESS TEST MODE:
    - Tests system load capacity with 12 concurrent workflows
    - Each workflow processes 1 month of data
    - All workflows run simultaneously to test concurrency
    - Measures success rates, processing times, and system stability
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

# GDT_USERNAME = "8026037048"
# GDT_PASSWORD = "Ngo8@vvt"


# Company info (based on username which is tax code)
# COMPANY_ID = "Ngo8Vovantan"
# COMPANY_NAME = "Ngo8Vovantan"
COMPANY_NAME = "PATEDELI"
COMPANY_ID = "bffe767f-aed0-4d73-b996-dc80edfd752c"


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
    discovery_method: str = "excel",  # Excel is more reliable
    excel_fallback: bool = True,
    processing_mode: str = "parallel",  # "parallel" or "sequential"
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
                    "discovery_method": discovery_method,
                    "excel_fallback": excel_fallback,
                    "processing_mode": processing_mode,
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

async def run_sequential_test(num_tasks: int, flows: list[str], discovery_method: str = "excel", excel_fallback: bool = True, processing_mode: str = "parallel"):
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
                discovery_method=discovery_method,
                excel_fallback=excel_fallback,
                processing_mode=processing_mode,
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


async def run_parallel_test(num_tasks: int, flows: list[str], discovery_method: str = "excel", excel_fallback: bool = True, processing_mode: str = "parallel"):
    """Run tasks in parallel (all at once) - STRESS TEST MODE."""
    print(f"\n{'='*80}")
    print(f"🚀 PARALLEL STRESS TEST: {num_tasks} tasks")
    print(f"📊 Flows: {flows}")
    print(f"⚡ Testing system load capacity with concurrent workflows")
    print(f"{'='*80}\n")

    month_ranges = get_month_ranges(num_tasks)

    # Start all workflows in parallel - TRUE CONCURRENCY TEST
    print(f"🚀 Starting {num_tasks} workflows SIMULTANEOUSLY...")
    print(f"📅 Date ranges: {month_ranges}")

    # Create all workflow start tasks
    start_tasks = []
    for idx, (start_date, end_date) in enumerate(month_ranges, 1):
        task = start_workflow(
            company_id=COMPANY_ID,
            company_name=COMPANY_NAME,
            username=GDT_USERNAME,
            password=GDT_PASSWORD,
            date_range_start=start_date,
            date_range_end=end_date,
            flows=flows,
            discovery_method=discovery_method,
            excel_fallback=excel_fallback,
            processing_mode=processing_mode,
        )
        start_tasks.append((task, idx, start_date, end_date))

    # Execute ALL workflow starts concurrently
    print(f"⚡ Executing {len(start_tasks)} workflow starts in parallel...")
    start_results = await asyncio.gather(*[task for task, _, _, _ in start_tasks], return_exceptions=True)

    # Process start results
    workflow_ids = []
    successful_starts = 0
    failed_starts = 0

    for i, (result, idx, start_date, end_date) in enumerate(zip(start_results, *zip(*[(idx, start_date, end_date) for _, idx, start_date, end_date in start_tasks]))):
        if isinstance(result, Exception):
            print(f"   [{idx}/{num_tasks}] ❌ Failed to start: {str(result)}")
            failed_starts += 1
        else:
            workflow_id = result["workflow_id"]
            workflow_ids.append({
                "workflow_id": workflow_id,
                "date_range": f"{start_date} to {end_date}",
                "month": idx,
            })
            print(f"   [{idx}/{num_tasks}] ✅ Started: {workflow_id} ({start_date} to {end_date})")
            successful_starts += 1

    print(f"\n📊 STARTUP SUMMARY:")
    print(f"   ✅ Successful starts: {successful_starts}")
    print(f"   ❌ Failed starts: {failed_starts}")
    print(f"   🎯 Success rate: {successful_starts/num_tasks*100:.1f}%")

    if not workflow_ids:
        print("❌ No workflows started successfully - aborting test")
        return

    # Wait for all workflows to complete - CONCURRENT MONITORING
    print(f"\n⏳ Monitoring {len(workflow_ids)} workflows concurrently...")
    print(f"🔄 Each workflow will be monitored in parallel")

    # Create monitoring tasks for all workflows
    monitor_tasks = []
    for wf_info in workflow_ids:
        task = wait_for_workflow(wf_info["workflow_id"], max_wait_seconds=1200)  # 20 minutes timeout
        monitor_tasks.append((task, wf_info))

    # Execute ALL monitoring tasks concurrently
    print(f"⚡ Starting concurrent monitoring of {len(monitor_tasks)} workflows...")
    monitor_results = await asyncio.gather(*[task for task, _ in monitor_tasks], return_exceptions=True)

    # Process monitoring results
    results = []
    for i, (result, wf_info) in enumerate(zip(monitor_results, [wf_info for _, wf_info in monitor_tasks])):
        if isinstance(result, Exception):
            results.append({
                "workflow_id": wf_info["workflow_id"],
                "date_range": wf_info["date_range"],
                "month": wf_info["month"],
                "status": "error",
                "error": str(result),
            })
        else:
            if result["status"] == "completed":
                result_data = result.get("result", {})
                results.append({
                    "workflow_id": wf_info["workflow_id"],
                    "date_range": wf_info["date_range"],
                    "month": wf_info["month"],
                    "status": "completed",
                    "invoices": result_data.get("completed_invoices", 0),
                    "total_invoices": result_data.get("total_invoices", 0),
                    "success_rate": result_data.get("success_rate", 0),
                })
            else:
                results.append({
                    "workflow_id": wf_info["workflow_id"],
                    "date_range": wf_info["date_range"],
                    "month": wf_info["month"],
                    "status": "failed",
                    "error": result.get("error"),
                })

    # Print detailed summary
    print(f"\n{'='*80}")
    print("🎯 STRESS TEST RESULTS")
    print(f"{'='*80}")

    total_invoices = 0
    total_processed = 0
    successful = 0
    failed = 0
    total_success_rate = 0

    # Sort results by month for better readability
    results.sort(key=lambda x: x.get("month", 0))

    for result in results:
        if result["status"] == "completed":
            successful += 1
            invoices = result.get("invoices", 0)
            total_invoices += invoices
            total_processed += result.get("total_invoices", 0)
            success_rate = result.get("success_rate", 0)
            total_success_rate += success_rate
            
            print(f"✅ Month {result['month']:2d} ({result['date_range']}): "
                  f"{invoices:4d} invoices ({success_rate:5.1f}% success rate)")
        else:
            failed += 1
            print(f"❌ Month {result['month']:2d} ({result['date_range']}): "
                  f"{result.get('error', 'Failed')}")

    # Overall statistics
    overall_success_rate = (successful / num_tasks * 100) if num_tasks > 0 else 0
    avg_success_rate = (total_success_rate / successful) if successful > 0 else 0
    
    print(f"\n{'='*80}")
    print("📊 STRESS TEST SUMMARY")
    print(f"{'='*80}")
    print(f"🎯 Workflows: {successful} successful, {failed} failed")
    print(f"📈 Overall success rate: {overall_success_rate:.1f}%")
    print(f"📄 Total invoices processed: {total_invoices:,}")
    print(f"📊 Total invoices discovered: {total_processed:,}")
    print(f"⚡ Average processing success rate: {avg_success_rate:.1f}%")
    print(f"🚀 System handled {num_tasks} concurrent workflows: {'✅ PASSED' if overall_success_rate >= 80 else '❌ FAILED'}")
    
    if overall_success_rate >= 90:
        print(f"🏆 EXCELLENT: System handled high load with {overall_success_rate:.1f}% success rate!")
    elif overall_success_rate >= 80:
        print(f"✅ GOOD: System handled load with {overall_success_rate:.1f}% success rate")
    else:
        print(f"⚠️  NEEDS IMPROVEMENT: System struggled with {overall_success_rate:.1f}% success rate")


# ============================================================================
# CLI
# ============================================================================

async def main():
    parser = argparse.ArgumentParser(
        description="Test GDT invoice import with real credentials - STRESS TEST MODE"
    )
    parser.add_argument(
        "--tasks",
        type=int,
        default=1,
        help="Number of tasks to create (12 = full year, 1 = last month)",
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
        default="parallel",
        help="Execution mode (parallel = stress test, sequential = normal test)",
    )
    parser.add_argument(
        "--stress-test",
        action="store_true",
        help="Force stress test mode (12 concurrent tasks for full year)",
    )
    parser.add_argument(
        "--discovery-method",
        choices=["api", "excel"],
        default="excel",
        help="Discovery method: excel (more reliable) or api (faster)",
    )
    parser.add_argument(
        "--no-excel-fallback",
        action="store_true",
        help="Disable Excel fallback when API discovery fails",
    )
    parser.add_argument(
        "--processing-mode",
        choices=["parallel", "sequential"],
        default="parallel",
        help="Processing mode: parallel (faster) or sequential (more reliable)",
    )

    args = parser.parse_args()

    # Override for stress test mode
    if args.stress_test:
        args.tasks = 12
        args.mode = "parallel"
        print("🚀 STRESS TEST MODE ACTIVATED")

    print("\n" + "="*80)
    print("🚀 GDT INVOICE IMPORT STRESS TEST")
    print("="*80)
    print(f"🏢 Company ID: {COMPANY_ID}")
    print(f"👤 Username: {GDT_USERNAME}")
    print(f"📊 Number of tasks: {args.tasks}")
    print(f"🔄 Flows: {args.flows}")
    print(f"⚡ Mode: {args.mode}")
    print(f"🔍 Discovery method: {args.discovery_method}")
    print(f"🔄 Excel fallback: {'disabled' if args.no_excel_fallback else 'enabled'}")
    print(f"⚙️ Processing mode: {args.processing_mode}")
    
    if args.mode == "parallel":
        print(f"🔥 STRESS TEST: {args.tasks} concurrent workflows")
        print(f"📅 Testing full year data processing capacity")
    else:
        print(f"📋 NORMAL TEST: {args.tasks} sequential workflows")

    # Check API is running
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{API_BASE_URL}/")
            api_status = response.json()
            print(f"🌐 API Status: ✅ {api_status['status']}")
            print(f"🔗 Temporal Status: {api_status.get('temporal_status', 'unknown')}")
            
            # Check detailed health
            health_response = await client.get(f"{API_BASE_URL}/health")
            health_status = health_response.json()
            print(f"💚 Health Check: {health_status['status']}")
            if health_status['status'] != 'healthy':
                print(f"⚠️  Health Issue: {health_status.get('error', 'Unknown')}")
                
    except Exception as e:
        print(f"❌ API not available: {str(e)}")
        print("Make sure the API is running: docker-compose up -d")
        return

    # Run tests
    if args.mode == "sequential":
        await run_sequential_test(args.tasks, args.flows, args.discovery_method, not args.no_excel_fallback, args.processing_mode)
    else:
        await run_parallel_test(args.tasks, args.flows, args.discovery_method, not args.no_excel_fallback, args.processing_mode)


if __name__ == "__main__":
    asyncio.run(main())
