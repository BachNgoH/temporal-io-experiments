# Scheduling API - External Integration Guide

This guide explains how external services can integrate with the Temporal Task System's scheduling feature to automate recurring tasks.

## Overview

The Scheduling API allows external services to:
- Create daily schedules for automated task execution
- Manage existing schedules (pause, resume, delete, trigger)
- Monitor schedule execution status
- Support any task type (invoice imports, reports, data pipelines, etc.)

## Base URL

```
Production: https://your-domain.com
Development: http://localhost:8000
```

## Authentication

*TODO: Add authentication headers when implemented*

Currently, all endpoints are accessible without authentication for development.

---

## API Endpoints

### 1. Create Schedule

Create a new daily schedule for automated task execution.

**Endpoint:** `POST /api/schedules/create`

**Request Body:**
```json
{
  "schedule_id": "unique-schedule-identifier",
  "task_type": "gdt_invoice_import",
  "task_params": {
    "company_id": "0123456789",
    "credentials": {
      "username": "user@example.com",
      "password": "secure_password"
    },
    "date_range_start": "{{ .ScheduledTime.Add(-24h).Format \"2006-01-02\" }}",
    "date_range_end": "{{ .ScheduledTime.Add(-24h).Format \"2006-01-02\" }}"
  },
  "hour": 1,
  "minute": 0,
  "paused": false,
  "note": "Daily import at 1 AM UTC"
}
```

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `schedule_id` | string | Yes | Unique identifier for the schedule |
| `task_type` | string | Yes | Type of task to schedule (e.g., `gdt_invoice_import`) |
| `task_params` | object | Yes | Task-specific parameters (varies by task type) |
| `hour` | integer | No | Hour to run (0-23, UTC). Default: 1 |
| `minute` | integer | No | Minute to run (0-59). Default: 0 |
| `paused` | boolean | No | Create schedule in paused state. Default: false |
| `note` | string | No | Optional description |

**Dynamic Date Templating:**

Use Go template syntax for dynamic dates in `task_params`:

- `{{ .ScheduledTime.Add(-24h).Format "2006-01-02" }}` - Previous day
- `{{ .ScheduledTime.Format "2006-01-02" }}` - Current day
- `{{ .ScheduledTime.Add(-168h).Format "2006-01-02" }}` - 7 days ago

**Response:**
```json
{
  "schedule_id": "unique-schedule-identifier",
  "task_type": "gdt_invoice_import",
  "status": "created",
  "message": "Schedule created - runs daily at 01:00 UTC"
}
```

**Status Codes:**
- `200 OK` - Schedule created successfully
- `400 Bad Request` - Invalid parameters
- `500 Internal Server Error` - Server error

---

### 2. List All Schedules

Get a list of all schedules in the system.

**Endpoint:** `GET /api/schedules`

**Response:**
```json
{
  "schedules": [
    {
      "id": "schedule-1",
      "info": {
        "num_actions": 5,
        "paused": false
      }
    },
    {
      "id": "schedule-2",
      "info": {
        "num_actions": 12,
        "paused": true
      }
    }
  ]
}
```

---

### 3. Get Schedule Details

Get detailed information about a specific schedule.

**Endpoint:** `GET /api/schedules/{schedule_id}`

**Response:**
```json
{
  "schedule_id": "daily-import-company123",
  "paused": false,
  "note": "Daily import at 1 AM UTC",
  "num_actions": 30,
  "num_actions_skipped": 0
}
```

**Parameters:**

| Field | Description |
|-------|-------------|
| `num_actions` | Total number of times this schedule has executed |
| `num_actions_skipped` | Number of executions skipped due to overlap policy |
| `paused` | Whether the schedule is currently paused |

---

### 4. Trigger Schedule Manually

Manually trigger a schedule to run immediately (outside its regular schedule).

**Endpoint:** `POST /api/schedules/{schedule_id}/trigger`

**Response:**
```json
{
  "schedule_id": "daily-import-company123",
  "status": "triggered",
  "message": "Schedule triggered successfully"
}
```

**Use Case:** Test a schedule or run it on-demand without waiting for the scheduled time.

---

### 5. Pause Schedule

Pause a schedule to prevent it from executing.

**Endpoint:** `POST /api/schedules/{schedule_id}/pause`

**Query Parameters:**
- `note` (optional): Reason for pausing

**Response:**
```json
{
  "schedule_id": "daily-import-company123",
  "status": "paused",
  "message": "Schedule paused successfully"
}
```

---

### 6. Unpause Schedule

Resume a paused schedule.

**Endpoint:** `POST /api/schedules/{schedule_id}/unpause`

**Query Parameters:**
- `note` (optional): Note about resuming

**Response:**
```json
{
  "schedule_id": "daily-import-company123",
  "status": "unpaused",
  "message": "Schedule unpaused successfully"
}
```

---

### 7. Delete Schedule

Permanently delete a schedule. This does not affect workflows already started by the schedule.

**Endpoint:** `DELETE /api/schedules/{schedule_id}`

**Response:**
```json
{
  "schedule_id": "daily-import-company123",
  "status": "deleted",
  "message": "Schedule deleted successfully"
}
```

---

## Task Types

### GDT Invoice Import (`gdt_invoice_import`)

Automated daily import of invoices from GDT portal.

**Task Parameters:**

```json
{
  "company_id": "0123456789",
  "credentials": {
    "username": "user@example.com",
    "password": "secure_password"
  },
  "date_range_start": "2024-01-01",  // Optional: defaults to yesterday
  "date_range_end": "2024-01-01",    // Optional: defaults to yesterday
  "flows": [                         // Optional: defaults to all flows
    "ban_ra_dien_tu",
    "ban_ra_may_tinh_tien",
    "mua_vao_dien_tu",
    "mua_vao_may_tinh_tien"
  ],
  "discovery_method": "excel",       // Optional: "api" or "excel" (default: excel)
  "processing_mode": "sequential"    // Optional: "sequential" or "parallel" (default: sequential)
}
```

**Minimal Example (Auto-import yesterday):**
```json
{
  "company_id": "0123456789",
  "credentials": {
    "username": "user@example.com",
    "password": "secure_password"
  }
}
```

---

## Integration Examples

### Python

```python
import requests

API_URL = "http://localhost:8000"

# Create a daily schedule
def create_daily_schedule(schedule_id: str, company_id: str, username: str, password: str):
    response = requests.post(
        f"{API_URL}/api/schedules/create",
        json={
            "schedule_id": schedule_id,
            "task_type": "gdt_invoice_import",
            "task_params": {
                "company_id": company_id,
                "credentials": {
                    "username": username,
                    "password": password
                }
            },
            "hour": 1,
            "minute": 0,
            "note": f"Daily import for company {company_id}"
        }
    )
    return response.json()

# List all schedules
def list_schedules():
    response = requests.get(f"{API_URL}/api/schedules")
    return response.json()

# Get schedule details
def get_schedule(schedule_id: str):
    response = requests.get(f"{API_URL}/api/schedules/{schedule_id}")
    return response.json()

# Pause a schedule
def pause_schedule(schedule_id: str, reason: str = ""):
    response = requests.post(
        f"{API_URL}/api/schedules/{schedule_id}/pause",
        params={"note": reason}
    )
    return response.json()

# Delete a schedule
def delete_schedule(schedule_id: str):
    response = requests.delete(f"{API_URL}/api/schedules/{schedule_id}")
    return response.json()
```

### Node.js / TypeScript

```typescript
import axios from 'axios';

const API_URL = 'http://localhost:8000';

// Create a daily schedule
async function createDailySchedule(
  scheduleId: string,
  companyId: string,
  username: string,
  password: string
) {
  const response = await axios.post(`${API_URL}/api/schedules/create`, {
    schedule_id: scheduleId,
    task_type: 'gdt_invoice_import',
    task_params: {
      company_id: companyId,
      credentials: {
        username,
        password
      }
    },
    hour: 1,
    minute: 0,
    note: `Daily import for company ${companyId}`
  });

  return response.data;
}

// List all schedules
async function listSchedules() {
  const response = await axios.get(`${API_URL}/api/schedules`);
  return response.data;
}

// Trigger schedule manually
async function triggerSchedule(scheduleId: string) {
  const response = await axios.post(
    `${API_URL}/api/schedules/${scheduleId}/trigger`
  );
  return response.data;
}
```

### cURL

```bash
# Create a schedule
curl -X POST http://localhost:8000/api/schedules/create \
  -H 'Content-Type: application/json' \
  -d '{
    "schedule_id": "daily-import-company123",
    "task_type": "gdt_invoice_import",
    "task_params": {
      "company_id": "0123456789",
      "credentials": {
        "username": "user@example.com",
        "password": "secure_password"
      }
    },
    "hour": 1,
    "minute": 0
  }'

# List all schedules
curl http://localhost:8000/api/schedules

# Get schedule details
curl http://localhost:8000/api/schedules/daily-import-company123

# Trigger schedule
curl -X POST http://localhost:8000/api/schedules/daily-import-company123/trigger

# Pause schedule
curl -X POST http://localhost:8000/api/schedules/daily-import-company123/pause

# Delete schedule
curl -X DELETE http://localhost:8000/api/schedules/daily-import-company123
```

---

## Best Practices

### 1. Schedule Naming Convention

Use descriptive schedule IDs that include:
- Task type
- Company/entity identifier
- Purpose

**Example:** `daily-invoice-import-company123`

### 2. Timezone Considerations

- All times are in **UTC**
- Schedule runs daily at the specified UTC time
- Imports the **full previous day** (00:00:00 to 23:59:59)
- Convert local time to UTC before creating schedules

**Example:**
```
Vietnam Time (UTC+7): 8:00 AM = 1:00 AM UTC
Thailand Time (UTC+7): 8:00 AM = 1:00 AM UTC
Singapore Time (UTC+8): 9:00 AM = 1:00 AM UTC
```

### 3. Error Handling

Always handle potential errors:

```python
try:
    response = requests.post(f"{API_URL}/api/schedules/create", json=payload)
    response.raise_for_status()
    result = response.json()
    print(f"Schedule created: {result['schedule_id']}")
except requests.HTTPError as e:
    print(f"Failed to create schedule: {e.response.text}")
except Exception as e:
    print(f"Error: {str(e)}")
```

### 4. Idempotency

- Schedule IDs must be unique
- Creating a schedule with an existing ID will fail
- Use descriptive, collision-resistant IDs

### 5. Monitoring

- Check `num_actions` to verify schedule is running
- Monitor for `num_actions_skipped` to detect overlap issues
- Use `GET /api/schedules/{id}` to check execution history

---

## Monitoring Schedule Execution

### Check if Schedule Executed

After a schedule runs, check the execution count:

```bash
# Before execution
curl http://localhost:8000/api/schedules/my-schedule
# {"num_actions": 0, ...}

# After execution (wait for scheduled time)
curl http://localhost:8000/api/schedules/my-schedule
# {"num_actions": 1, ...}
```

### View Workflow Runs in Temporal UI

Access the Temporal UI to see detailed workflow execution:

```
http://localhost:8080/namespaces/default/schedules/{schedule_id}
```

---

## Troubleshooting

### Schedule Not Executing

1. **Check schedule status:**
   ```bash
   curl http://localhost:8000/api/schedules/{schedule_id}
   ```
   - Verify `paused: false`

2. **Verify time configuration:**
   - Ensure `hour` and `minute` are correct (UTC time)
   - Check current UTC time: `date -u`

3. **Check Temporal UI:**
   - Navigate to http://localhost:8080
   - Find your schedule
   - Check for errors or failed runs

### Schedule Created But Workflow Fails

1. **Check workflow parameters:**
   - Verify all required `task_params` are provided
   - Ensure credentials are valid

2. **View workflow error in Temporal UI:**
   - Go to the workflow execution
   - Check error messages and stack traces

3. **Check worker logs:**
   ```bash
   docker logs deployment-temporal-worker-1
   ```

---

## Rate Limits

*TODO: Document rate limits when implemented*

Currently, there are no rate limits on schedule creation or management.

---

## Webhook Notifications

*TODO: Document webhook integration*

Future versions will support webhook notifications for:
- Schedule execution completed
- Schedule execution failed
- Schedule paused/resumed

---

## Support

For issues or questions:
- GitHub Issues: [Create Issue](https://github.com/finizi-app/ai-core-temporal/issues)
- Documentation: [Main README](../README.md)
- Temporal UI: http://localhost:8080 (development)
