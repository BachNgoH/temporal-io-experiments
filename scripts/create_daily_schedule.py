#!/usr/bin/env python3
"""CLI script to create daily schedules for invoice imports."""

import asyncio
import sys

import httpx


async def create_daily_schedule(
    schedule_id: str,
    company_id: str,
    username: str,
    password: str,
    hour: int = 1,
    minute: int = 0,
    api_url: str = "http://localhost:8000",
):
    """
    Create a daily schedule for importing previous day's invoices.

    Schedule Behavior:
    - Runs daily at specified time (hour:minute in UTC)
    - Imports FULL previous day (00:00:00 to 23:59:59)
    - Best practice: Run at low-traffic time (e.g., 1:00 AM UTC)

    Args:
        schedule_id: Unique identifier for the schedule
        company_id: Company ID for GDT portal
        username: GDT portal username
        password: GDT portal password
        hour: Hour to run (0-23, UTC, default: 1 AM)
        minute: Minute to run (0-59, default: 0)
        api_url: API base URL
    """
    payload = {
        "schedule_id": schedule_id,
        "task_type": "gdt_invoice_import",
        "task_params": {
            "company_id": company_id,
            "credentials": {
                "username": username,
                "password": password,
            },
            # Use Go template syntax for dynamic dates (previous day)
            "date_range_start": '{{ .ScheduledTime.Add(-24h).Format "2006-01-02" }}',
            "date_range_end": '{{ .ScheduledTime.Add(-24h).Format "2006-01-02" }}',
            "flows": [
                "ban_ra_dien_tu",
                "ban_ra_may_tinh_tien",
                "mua_vao_dien_tu",
                "mua_vao_may_tinh_tien",
            ],
            "discovery_method": "excel",
            "processing_mode": "sequential",
        },
        "hour": hour,
        "minute": minute,
        "note": f"Daily invoice import for company {company_id} - imports previous day's invoices",
    }

    async with httpx.AsyncClient() as client:
        try:
            print(f"🗓️  Creating daily schedule: {schedule_id}")
            print(f"📋 Company ID: {company_id}")
            print(f"⏰ Schedule time: {hour:02d}:{minute:02d} UTC (runs daily)")
            print(f"📅 Import scope: FULL previous day (00:00:00 to 23:59:59)")
            print()

            response = await client.post(f"{api_url}/api/schedules/create", json=payload)
            response.raise_for_status()

            result = response.json()
            print(f"✅ Schedule created successfully!")
            print(f"   Schedule ID: {result['schedule_id']}")
            print(f"   Status: {result['status']}")
            print(f"   Message: {result['message']}")
            print()
            print(f"📌 How it works:")
            print(f"   - Runs daily at {hour:02d}:{minute:02d} UTC")
            print(f"   - Imports full previous day (00:00:00 to 23:59:59)")
            print(f"   - Uses excel discovery + sequential processing (most reliable)")
            print()
            print(f"💡 Example: If it runs on Jan 2 at {hour:02d}:{minute:02d}, it imports all")
            print(f"   invoices from Jan 1 (00:00:00 to 23:59:59)")

        except httpx.HTTPStatusError as e:
            print(f"❌ Failed to create schedule: {e.response.status_code}")
            print(f"   Error: {e.response.text}")
            sys.exit(1)
        except Exception as e:
            print(f"❌ Error: {str(e)}")
            sys.exit(1)


async def list_schedules(api_url: str = "http://localhost:8000"):
    """List all existing schedules."""
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(f"{api_url}/api/schedules")
            response.raise_for_status()

            result = response.json()
            schedules = result.get("schedules", [])

            if not schedules:
                print("No schedules found.")
                return

            print(f"Found {len(schedules)} schedule(s):")
            print()
            for schedule in schedules:
                print(f"  • {schedule['id']}")
                print(f"    Actions: {schedule['info']['num_actions']}")
                print(f"    Paused: {schedule['info']['paused']}")
                print()

        except Exception as e:
            print(f"❌ Error: {str(e)}")
            sys.exit(1)


async def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Create daily schedules for invoice imports",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Create a daily schedule (runs at 1:00 AM UTC, imports full previous day)
  # If run on Jan 2 at 1 AM, imports all Jan 1 invoices (00:00:00-23:59:59)
  python scripts/create_daily_schedule.py create \\
      --schedule-id daily-import-company123 \\
      --company-id 0123456789 \\
      --username user@example.com \\
      --password secretpass

  # Create schedule at custom time (3:30 AM UTC)
  python scripts/create_daily_schedule.py create \\
      --schedule-id daily-import-company456 \\
      --company-id 0123456789 \\
      --username user@example.com \\
      --password secretpass \\
      --hour 3 \\
      --minute 30

  # List all schedules
  python scripts/create_daily_schedule.py list

Note: Schedule runs daily at specified UTC time and imports the FULL previous
      day (from 00:00:00 to 23:59:59). Best practice is to run during low-traffic
      hours (e.g., 1-4 AM UTC).
        """,
    )

    parser.add_argument(
        "--api-url",
        default="http://localhost:8000",
        help="API base URL (default: http://localhost:8000)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Create command
    create_parser = subparsers.add_parser("create", help="Create a new daily schedule")
    create_parser.add_argument(
        "--schedule-id", required=True, help="Unique schedule identifier"
    )
    create_parser.add_argument("--company-id", required=True, help="Company ID for GDT portal")
    create_parser.add_argument("--username", required=True, help="GDT portal username")
    create_parser.add_argument("--password", required=True, help="GDT portal password")
    create_parser.add_argument(
        "--hour", type=int, default=1, help="Hour to run (0-23, UTC, default: 1)"
    )
    create_parser.add_argument(
        "--minute", type=int, default=0, help="Minute to run (0-59, default: 0)"
    )

    # List command
    subparsers.add_parser("list", help="List all schedules")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "create":
        await create_daily_schedule(
            schedule_id=args.schedule_id,
            company_id=args.company_id,
            username=args.username,
            password=args.password,
            hour=args.hour,
            minute=args.minute,
            api_url=args.api_url,
        )
    elif args.command == "list":
        await list_schedules(api_url=args.api_url)


if __name__ == "__main__":
    asyncio.run(main())
