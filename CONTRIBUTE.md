# Contributing to Temporal Task System

Thank you for your interest in contributing to the Finizi Temporal Task System! This document provides guidelines and conventions for contributing to this project.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Development Setup](#development-setup)
- [Project Structure](#project-structure)
- [Coding Standards](#coding-standards)
- [Adding New Task Types](#adding-new-task-types)
- [Testing Guidelines](#testing-guidelines)
- [Documentation](#documentation)
- [Pull Request Process](#pull-request-process)
- [Release Process](#release-process)

## Code of Conduct

This project follows the [Contributor Covenant Code of Conduct](https://www.contributor-covenant.org/version/2/1/code_of_conduct/). Please read and follow these guidelines to ensure a welcoming environment for all contributors.

## Development Setup

### Prerequisites

- Python 3.11+
- Docker and Docker Compose
- [uv](https://github.com/astral-sh/uv) for package management
- Git

### Local Development

1. **Clone the repository**
   ```bash
   git clone https://github.com/finizi-app/ai-core-temporal.git
   cd ai-core-temporal
   ```

2. **Install dependencies**
   ```bash
   uv pip install -e .
   ```

3. **Install development dependencies**
   ```bash
   uv pip install -e ".[dev]"
   ```

4. **Start local services**
   ```bash
   cd deployment
   docker-compose up -d
   ```

5. **Run tests**
   ```bash
   pytest
   ```

6. **Code formatting and linting**
   ```bash
   black .
   ruff check .
   mypy .
   ```

### Environment Configuration

Copy `.env.example` to `.env` and configure as needed:

```bash
cp .env.example .env
```

Key environment variables:
- `TEMPORAL_HOST`: Temporal server address
- `GCP_PROJECT_ID`: Google Cloud project ID
- `GOOGLE_APPLICATION_CREDENTIALS`: Path to service account key

## Project Structure

```
.
├── app/                          # FastAPI application
│   ├── main.py                   # Main API application
│   ├── config.py                 # Configuration management
│   └── models.py                 # Pydantic models
├── temporal_app/                 # Temporal workflows and activities
│   ├── workflows/                # Workflow definitions
│   ├── activities/               # Activity implementations
│   ├── models.py                 # Temporal data models
│   └── worker.py                 # Worker configuration
├── deployment/                   # Deployment configurations
├── docs/                         # Documentation
├── tests/                        # Test suite
└── scripts/                      # Utility scripts
```

## Coding Standards

### Code Style

We use the following tools to maintain code quality:

- **Black**: Code formatting (line length: 100)
- **Ruff**: Linting and code analysis
- **MyPy**: Static type checking

### Python Conventions

1. **Type Hints**: All functions must have type hints
   ```python
   async def process_invoice(
       invoice: GdtInvoice, 
       session: GdtSession
   ) -> InvoiceFetchResult:
   ```

2. **Docstrings**: Use Google-style docstrings
   ```python
   def fetch_invoice_data(invoice_id: str) -> dict:
       """Fetch invoice data from external API.
       
       Args:
           invoice_id: Unique identifier for the invoice
           
       Returns:
           Dictionary containing invoice data
           
       Raises:
           InvoiceNotFoundError: If invoice is not found
       """
   ```

3. **Error Handling**: Use custom exceptions with proper hierarchy
   ```python
   class GDTError(Exception):
       """Base exception for GDT-related errors."""
       pass
   
   class GDTAuthError(GDTError):
       """Authentication-related errors."""
       pass
   ```

4. **Logging**: Use structured logging with appropriate levels
   ```python
   import logging
   
   logger = logging.getLogger(__name__)
   
   def process_data():
       logger.info("Starting data processing")
       try:
           # Processing logic
           logger.info("Processing completed successfully")
       except Exception as e:
           logger.error(f"Processing failed: {e}")
           raise
   ```

### Temporal-Specific Conventions

1. **Workflows**: Keep workflows deterministic and focused on orchestration
   ```python
   @workflow.defn
   class ExampleWorkflow:
       @workflow.run
       async def run(self, params: dict) -> dict:
           # Orchestrating activities, not doing I/O
           result = await workflow.execute_activity(
               some_activity,
               params,
               start_to_close_timeout=timedelta(minutes=5)
           )
           return result
   ```

2. **Activities**: Handle external interactions and implement business logic
   ```python
   @activity.defn
   async def some_activity(params: dict) -> dict:
       # External API calls, file operations, etc.
       activity.logger.info(f"Processing {params}")
       return {"status": "completed"}
   ```

3. **Retry Policies**: Configure appropriate retry strategies
   ```python
   retry_policy = RetryPolicy(
       initial_interval=timedelta(seconds=10),
       maximum_interval=timedelta(minutes=2),
       maximum_attempts=5,
       backoff_coefficient=2.0,
   )
   ```

## Adding New Task Types

### 1. Define Task Type

Add to [`app/models.py`](app/models.py):

```python
class TaskType(str, Enum):
    GDT_INVOICE_IMPORT = "gdt_invoice_import"
    YOUR_NEW_TASK = "your_new_task"  # Add new task type
```

### 2. Create Workflow

Create new file in [`temporal_app/workflows/`](temporal_app/workflows/):

```python
# temporal_app/workflows/your_new_task.py
from temporalio import workflow
from temporalio.common import RetryPolicy

@workflow.defn
class YourNewTaskWorkflow:
    @workflow.run
    async def run(self, params: dict) -> dict:
        # Implement workflow logic
        result = await workflow.execute_activity(
            your_activity,
            params,
            start_to_close_timeout=timedelta(minutes=30),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=10),
                maximum_attempts=3,
            ),
        )
        return result
```

### 3. Create Activities

Create new file in [`temporal_app/activities/`](temporal_app/activities/):

```python
# temporal_app/activities/your_new_task.py
from temporalio import activity

@activity.defn
async def your_activity(params: dict) -> dict:
    """Implement your activity logic here."""
    activity.logger.info(f"Processing {params}")
    # Your implementation
    return {"status": "completed"}
```

### 4. Register in Worker

Update [`temporal_app/worker.py`](temporal_app/worker.py):

```python
from temporal_app.workflows.your_new_task import YourNewTaskWorkflow
from temporal_app.activities.your_new_task import your_activity

# Add to workflows list
workflows=[
    GdtInvoiceImportWorkflow,
    YourNewTaskWorkflow,  # Add your workflow
],

# Add to activities list
activities=[
    login_to_gdt,
    discover_invoices,
    # ... existing activities
    your_activity,  # Add your activity
],
```

### 5. Update API

Update [`app/main.py`](app/main.py):

```python
from temporal_app.workflows.your_new_task import YourNewTaskWorkflow

def _get_workflow_class(task_type: TaskType) -> Any:
    workflow_mapping = {
        TaskType.GDT_INVOICE_IMPORT: GdtInvoiceImportWorkflow,
        TaskType.YOUR_NEW_TASK: YourNewTaskWorkflow,  # Add mapping
    }
    # ... rest of function
```

### 6. Update Activities Init

Update [`temporal_app/activities/__init__.py`](temporal_app/activities/__init__.py):

```python
from temporal_app.activities.your_new_task import your_activity

__all__ = [
    "login_to_gdt",
    "discover_invoices",
    # ... existing activities
    "your_activity",  # Add your activity
]
```

## Testing Guidelines

### Test Structure

```
tests/
├── unit/                          # Unit tests
│   ├── test_activities.py
│   ├── test_workflows.py
│   └── test_models.py
├── integration/                   # Integration tests
│   ├── test_api.py
│   └── test_end_to_end.py
└── fixtures/                      # Test data
    └── sample_data.json
```

### Writing Tests

1. **Unit Tests**: Test individual functions and classes
   ```python
   import pytest
   from temporal_app.activities.your_activity import your_activity
   
   @pytest.mark.asyncio
   async def test_your_activity_success():
       params = {"test": "data"}
       result = await your_activity(params)
       assert result["status"] == "completed"
   ```

2. **Integration Tests**: Test component interactions
   ```python
   import pytest
   from temporalio.client import Client
   
   @pytest.mark.asyncio
   async def test_workflow_execution():
       client = await Client.connect("localhost:7233")
       result = await client.execute_workflow(
           YourNewTaskWorkflow.run,
           {"test": "data"},
           id="test-workflow",
           task_queue="test-queue",
       )
       assert result["status"] == "completed"
   ```

3. **Mock External Dependencies**
   ```python
   from unittest.mock import AsyncMock, patch
   
   @pytest.mark.asyncio
   async def test_activity_with_external_api():
       with patch('httpx.AsyncClient.get', new_callable=AsyncMock) as mock_get:
           mock_get.return_value.json.return_value = {"data": "test"}
           result = await your_activity({"id": "123"})
           assert result["data"] == "test"
   ```

### Running Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=temporal_app --cov=app

# Run specific test file
pytest tests/unit/test_activities.py

# Run with verbose output
pytest -v
```

## Documentation

### Documentation Standards

1. **Code Documentation**: All public functions must have docstrings
2. **API Documentation**: FastAPI auto-generates API docs at `/docs`
3. **Architecture Docs**: Keep [`docs/SYSTEM_ARCHITECTURE.md`](docs/SYSTEM_ARCHITECTURE.md) updated
4. **README**: Update README.md for user-facing changes

### Writing Documentation

- Use clear, concise language
- Include code examples
- Provide step-by-step instructions
- Keep documentation in sync with code changes

## Pull Request Process

### Before Submitting

1. **Create a feature branch**
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **Make your changes**
   - Follow coding standards
   - Add tests for new functionality
   - Update documentation

3. **Run quality checks**
   ```bash
   black .
   ruff check .
   mypy .
   pytest
   ```

4. **Commit your changes**
   ```bash
   git add .
   git commit -m "feat: add your new feature"
   ```

### Pull Request Guidelines

1. **Title**: Use conventional commit format
   - `feat:` for new features
   - `fix:` for bug fixes
   - `docs:` for documentation changes
   - `refactor:` for code refactoring
   - `test:` for test changes

2. **Description**: Include:
   - Problem being solved
   - Approach taken
   - Testing done
   - Breaking changes (if any)

3. **PR Template**:
   ```markdown
   ## Description
   Brief description of changes
   
   ## Type of Change
   - [ ] Bug fix
   - [ ] New feature
   - [ ] Breaking change
   - [ ] Documentation update
   
   ## Testing
   - [ ] Unit tests pass
   - [ ] Integration tests pass
   - [ ] Manual testing completed
   
   ## Checklist
   - [ ] Code follows style guidelines
   - [ ] Self-review completed
   - [ ] Documentation updated
   - [ ] Tests added/updated
   ```

### Review Process

1. **Automated Checks**: CI/CD pipeline runs tests and quality checks
2. **Code Review**: At least one maintainer must review
3. **Approval**: Required before merging
4. **Merge**: Squash and merge to maintain clean history

## Release Process

### Version Management

We use semantic versioning (SemVer):
- `MAJOR.MINOR.PATCH`
- MAJOR: Breaking changes
- MINOR: New features (backward compatible)
- PATCH: Bug fixes (backward compatible)

### Release Steps

1. **Update version** in `pyproject.toml`
2. **Update CHANGELOG.md** with release notes
3. **Create release tag**
   ```bash
   git tag v1.2.3
   git push origin v1.2.3
   ```
4. **Deploy to production** (automated via CI/CD)

## Getting Help

- **Issues**: Report bugs or request features via GitHub Issues
- **Discussions**: Use GitHub Discussions for questions
- **Maintainers**: Tag maintainainers for urgent issues

## Additional Resources

- [Temporal.io Documentation](https://docs.temporal.io/)
- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [Python Type Hints](https://docs.python.org/3/library/typing.html)
- [pytest Documentation](https://docs.pytest.org/)

Thank you for contributing to the Temporal Task System!