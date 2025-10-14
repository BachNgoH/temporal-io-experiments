#!/usr/bin/env python3
"""
API TEST script for Company Lookup workflow.

Usage:
    python tests/test_company_lookup_flows.py                              # Single tax code lookup
    python tests/test_company_lookup_flows.py --tax-code 123456789         # Specific tax code
    python tests/test_company_lookup_flows.py --company-name "Test Company" # Company name lookup
    python tests/test_company_lookup_flows.py --list-mode                  # List mode search
    python tests/test_company_lookup_flows.py --stress-test                # Stress test with multiple searches
"""

import argparse
import asyncio
import httpx
from datetime import datetime

# ============================================================================
# Configuration
# ============================================================================

API_BASE_URL = "http://localhost:8000"

# Test data
TEST_TAX_CODES = [
    "0317530616",  # PATEDELI
    "8026037048",  # Alternative company
    "5300843289",
]

TEST_COMPANY_NAMES = [
    "PATEDELI",
    "CÔNG TY CỔ PHẦN ĐẦU TƯ PHÁT TRIỂN CÔNG NGHIỆP",
    "CÔNG TY TNHH TM DV XÂY DỰNG MINH CHÂU",
]


# ============================================================================
# Helper Functions
# ============================================================================

async def start_company_lookup(
    search_term: str,
    search_type: str = "tax_code",
    list_mode: bool = False,
) -> dict:
    """Start a company lookup workflow."""
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{API_BASE_URL}/api/tasks/start",
            json={
                "task_type": "entity_lookup",
                "task_params": {
                    "search_term": search_term,
                    "search_type": search_type,
                    "list_mode": list_mode,
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


async def wait_for_workflow(workflow_id: str, max_wait_seconds: int = 300) -> dict:
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
            print(f"   Progress: {progress}")

        # Wait before checking again
        await asyncio.sleep(3)


def format_result(result: dict) -> str:
    """Format workflow result for display."""
    if result["status"] == "completed":
        workflow_result = result.get("result", {})
        if workflow_result.get("result"):
            company_data = workflow_result["result"]
            
            if company_data.get("companies"):
                # List mode result
                companies = company_data["companies"]
                return f"Found {len(companies)} companies"
            elif company_data.get("data"):
                # Single company result
                data = company_data["data"]
                return (
                    f"Company: {data.get('company_name', 'N/A')}\n"
                    f"Tax Code: {data.get('tax_code', 'N/A')}\n"
                    f"Address: {data.get('address', 'N/A')}\n"
                    f"Status: {data.get('status', 'N/A')}"
                )
        
        return "Completed but no data found"
    else:
        return f"Failed: {result.get('error', 'Unknown error')}"


# ============================================================================
# Test Functions
# ============================================================================

async def run_single_lookup(search_term: str, search_type: str = "tax_code", list_mode: bool = False):
    """Run a single company lookup test."""
    print(f"\n{'='*80}")
    print(f"SINGLE LOOKUP TEST")
    print(f"Search Term: {search_term}")
    print(f"Search Type: {search_type}")
    print(f"List Mode: {list_mode}")
    print(f"{'='*80}\n")

    try:
        # Start workflow
        print("Starting company lookup workflow...")
        start_result = await start_company_lookup(search_term, search_type, list_mode)
        
        workflow_id = start_result["workflow_id"]
        print(f"✅ Started: {workflow_id}")

        # Wait for completion
        final_status = await wait_for_workflow(workflow_id, max_wait_seconds=300)

        if final_status["status"] == "completed":
            print(f"\n✅ COMPLETED SUCCESSFULLY")
            print(f"Result:\n{format_result(final_status)}")
            
            # Show processing time
            workflow_result = final_status.get("result", {})
            if workflow_result.get("processing_time"):
                processing_time = workflow_result["processing_time"]
                print(f"\n⏱️ Processing Time: {processing_time:.2f} seconds")
        else:
            print(f"\n❌ FAILED")
            print(f"Error: {final_status.get('error', 'Unknown error')}")

    except Exception as e:
        print(f"❌ Error: {str(e)}")


async def run_stress_test(num_searches: int = 10):
    """Run stress test with multiple concurrent searches."""
    print(f"\n{'='*80}")
    print(f"🚀 STRESS TEST: {num_searches} concurrent searches")
    print(f"⚡ Testing system load capacity with concurrent workflows")
    print(f"{'='*80}\n")

    # Prepare test data - mix of tax codes and company names
    test_cases = []
    for i in range(num_searches):
        if i % 2 == 0:
            # Use tax code
            tax_code = TEST_TAX_CODES[i % len(TEST_TAX_CODES)]
            test_cases.append({
                "search_term": tax_code,
                "search_type": "tax_code",
                "list_mode": False,
            })
        else:
            # Use company name
            company_name = TEST_COMPANY_NAMES[i % len(TEST_COMPANY_NAMES)]
            test_cases.append({
                "search_term": company_name,
                "search_type": "company_name",
                "list_mode": i % 3 == 0,  # Every 3rd search in list mode
            })

    # Start all workflows in parallel
    print(f"🚀 Starting {num_searches} company lookup workflows SIMULTANEOUSLY...")
    
    start_tasks = []
    for idx, test_case in enumerate(test_cases, 1):
        task = start_company_lookup(
            test_case["search_term"],
            test_case["search_type"],
            test_case["list_mode"],
        )
        start_tasks.append((task, idx, test_case))

    # Execute all workflow starts concurrently
    print(f"⚡ Executing {len(start_tasks)} workflow starts in parallel...")
    start_results = await asyncio.gather(*[task for task, _, _ in start_tasks], return_exceptions=True)

    # Process start results
    workflow_ids = []
    successful_starts = 0
    failed_starts = 0

    for i, (result, idx, test_case) in enumerate(zip(start_results, *zip(*[(idx, test_case) for _, idx, test_case in start_tasks]))):
        if isinstance(result, Exception):
            print(f"   [{idx:2d}/{num_searches}] ❌ Failed to start: {str(result)}")
            failed_starts += 1
        else:
            workflow_id = result["workflow_id"]
            workflow_ids.append({
                "workflow_id": workflow_id,
                "search_term": test_case["search_term"],
                "search_type": test_case["search_type"],
                "list_mode": test_case["list_mode"],
                "index": idx,
            })
            print(f"   [{idx:2d}/{num_searches}] ✅ Started: {workflow_id} ({test_case['search_term']})")
            successful_starts += 1

    print(f"\n📊 STARTUP SUMMARY:")
    print(f"   ✅ Successful starts: {successful_starts}")
    print(f"   ❌ Failed starts: {failed_starts}")
    print(f"   🎯 Success rate: {successful_starts/num_searches*100:.1f}%")

    if not workflow_ids:
        print("❌ No workflows started successfully - aborting test")
        return

    # Wait for all workflows to complete
    print(f"\n⏳ Monitoring {len(workflow_ids)} workflows concurrently...")
    
    monitor_tasks = []
    for wf_info in workflow_ids:
        task = wait_for_workflow(wf_info["workflow_id"], max_wait_seconds=300)
        monitor_tasks.append((task, wf_info))

    # Execute all monitoring tasks concurrently
    print(f"⚡ Starting concurrent monitoring of {len(monitor_tasks)} workflows...")
    monitor_results = await asyncio.gather(*[task for task, _ in monitor_tasks], return_exceptions=True)

    # Process monitoring results
    results = []
    for i, (result, wf_info) in enumerate(zip(monitor_results, [wf_info for _, wf_info in monitor_tasks])):
        if isinstance(result, Exception):
            results.append({
                "workflow_id": wf_info["workflow_id"],
                "search_term": wf_info["search_term"],
                "search_type": wf_info["search_type"],
                "list_mode": wf_info["list_mode"],
                "index": wf_info["index"],
                "status": "error",
                "error": str(result),
            })
        else:
            results.append({
                "workflow_id": wf_info["workflow_id"],
                "search_term": wf_info["search_term"],
                "search_type": wf_info["search_type"],
                "list_mode": wf_info["list_mode"],
                "index": wf_info["index"],
                "status": result["status"],
                "result": result.get("result", {}),
                "processing_time": result.get("result", {}).get("processing_time", 0),
            })

    # Print detailed results
    print(f"\n{'='*80}")
    print("🎯 STRESS TEST RESULTS")
    print(f"{'='*80}")

    successful = 0
    failed = 0
    total_processing_time = 0

    # Sort results by index for better readability
    results.sort(key=lambda x: x.get("index", 0))

    for result in results:
        idx = result["index"]
        search_term = result["search_term"][:30] + "..." if len(result["search_term"]) > 30 else result["search_term"]
        search_type = result["search_type"][:4]
        
        if result["status"] == "completed":
            successful += 1
            processing_time = result.get("processing_time", 0)
            total_processing_time += processing_time
            
            result_data = result.get("result", {}).get("result", {})
            if result_data.get("companies"):
                count = len(result_data["companies"])
                print(f"✅ [{idx:2d}] {search_type} {search_term}: Found {count} companies ({processing_time:.1f}s)")
            elif result_data.get("data"):
                company_name = result_data["data"].get("company_name", "N/A")
                print(f"✅ [{idx:2d}] {search_type} {search_term}: {company_name} ({processing_time:.1f}s)")
            else:
                print(f"✅ [{idx:2d}] {search_type} {search_term}: No data found ({processing_time:.1f}s)")
        else:
            failed += 1
            error_msg = result.get("error", "Failed")[:50]
            print(f"❌ [{idx:2d}] {search_type} {search_term}: {error_msg}")

    # Overall statistics
    overall_success_rate = (successful / num_searches * 100) if num_searches > 0 else 0
    avg_processing_time = (total_processing_time / successful) if successful > 0 else 0
    
    print(f"\n{'='*80}")
    print("📊 STRESS TEST SUMMARY")
    print(f"{'='*80}")
    print(f"🎯 Workflows: {successful} successful, {failed} failed")
    print(f"📈 Overall success rate: {overall_success_rate:.1f}%")
    print(f"⏱️ Average processing time: {avg_processing_time:.1f} seconds")
    print(f"🚀 System handled {num_searches} concurrent searches: {'✅ PASSED' if overall_success_rate >= 80 else '❌ FAILED'}")
    
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
        description="Test Company Lookup workflow via API"
    )
    parser.add_argument(
        "--tax-code",
        type=str,
        help="Tax code to search for",
    )
    parser.add_argument(
        "--company-name",
        type=str,
        help="Company name to search for",
    )
    parser.add_argument(
        "--list-mode",
        action="store_true",
        help="Enable list mode search",
    )
    parser.add_argument(
        "--stress-test",
        action="store_true",
        help="Run stress test with multiple concurrent searches",
    )
    parser.add_argument(
        "--searches",
        type=int,
        default=10,
        help="Number of searches for stress test (default: 10)",
    )

    args = parser.parse_args()

    print("\n" + "="*80)
    print("🔍 COMPANY LOOKUP API TEST")
    print("="*80)

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

    # Run tests based on arguments
    if args.stress_test:
        print(f"🚀 STRESS TEST MODE: {args.searches} concurrent searches")
        await run_stress_test(args.searches)
    elif args.tax_code:
        print(f"🔍 TAX CODE LOOKUP: {args.tax_code}")
        await run_single_lookup(args.tax_code, "tax_code", args.list_mode)
    elif args.company_name:
        print(f"🔍 COMPANY NAME LOOKUP: {args.company_name}")
        await run_single_lookup(args.company_name, "company_name", args.list_mode)
    else:
        # Default: run a single tax code lookup
        print(f"🔍 DEFAULT TAX CODE LOOKUP: {TEST_TAX_CODES[0]}")
        await run_single_lookup(TEST_TAX_CODES[0], "tax_code", False)


if __name__ == "__main__":
    asyncio.run(main())