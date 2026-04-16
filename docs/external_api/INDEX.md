# External API & Observability

**Last Updated:** 2026-01-27  
**Parent:** [Project_Init/INDEX.md](../Project_Init/INDEX.md)

---

## 1. Structure

### 1.1 API Layer (`wtb/api/`)

```
wtb/api/
├── __init__.py
├── rest/                        # REST API (FastAPI)
│   ├── app.py                  # Application factory
│   ├── models.py               # Pydantic schemas
│   ├── dependencies.py         # Dependency injection
│   └── routes/
│       ├── workflows.py        # Workflow CRUD
│       ├── executions.py       # Execution lifecycle
│       ├── checkpoints.py      # Checkpoint operations
│       ├── batch.py            # Batch test endpoints
│       ├── variants.py         # Node variants
│       └── health.py           # Health checks
├── websocket/
│   └── handlers.py             # Real-time event streaming
└── grpc/
    └── protos/
        └── wtb.proto           # gRPC service definition
```

### 1.2 SDK Layer (`wtb/sdk/`)

```
wtb/sdk/
├── __init__.py
├── test_bench.py               # TestBench high-level API
└── workflow_project.py         # WorkflowProject configuration
```

### 1.3 Protocol Summary

| Protocol | Port | Use Case | Status |
|----------|------|----------|--------|
| REST (FastAPI) | 8000 | Web UI, CRUD operations | ✅ Implemented |
| WebSocket | 8000/ws | Real-time events | ✅ Implemented |
| gRPC | 50051 | Internal services, streaming | 🔨 Proto defined |

### 1.4 SDK Usage

```python
from wtb.sdk import TestBench, WorkflowProject

project = WorkflowProject(
    name="my-workflow",
    db_path="./data/wtb.db",
    output_dir="./outputs",
)

bench = TestBench(project)
result = bench.run(graph, initial_state={"query": "hello"})
```

---

## 2. Issues

### 2.1 Active Issues

| ID | Issue | Priority | Status |
|----|-------|----------|--------|
| API-001 | gRPC implementation pending | P2 | Backlog |
| API-002 | API test coverage ~70% | P2 | Open |
| API-003 | OpenTelemetry integration pending | P3 | Backlog |

### 2.2 API-001: gRPC Implementation

**Status:** Proto file defined, servicer implementation pending.

**Impact:** Internal service communication relies on REST.

### 2.3 API-002: Test Coverage

**Current:** ~70% coverage for REST routes.

**Gap:** WebSocket streaming tests incomplete.

---

## 3. Gap Analysis (Brief)

| Design Intent | Implementation | Gap |
|--------------|----------------|-----|
| REST API (FastAPI) | ✅ Full CRUD + lifecycle | None |
| WebSocket streaming | ✅ Event bridge | None |
| gRPC service | 🔨 Proto only | Minor - impl pending |
| SDK (TestBench) | ✅ High-level API | None |
| WorkflowProject config | ✅ Full configuration | None |
| OpenTelemetry | 🔨 Pending | Minor - observability |

**Overall:** Core API fully implemented; gRPC and observability pending.

---

## 4. Test Coverage

| Test File | Tests | Status |
|-----------|-------|--------|
| `test_rest_models.py` | 40+ | ✅ |
| `test_rest_integration.py` | 20+ | ✅ |
| `test_external_control.py` | 15+ | ✅ |
| `test_workflow_submission_integration.py` | 10+ | ✅ |

---

## 5. Related Documents

| Document | Description |
|----------|-------------|
| [../Project_Init/INDEX.md](../Project_Init/INDEX.md) | Main documentation |
| [../Project_Init/ARCHITECTURE_STRUCTURE.md](../Project_Init/ARCHITECTURE_STRUCTURE.md) | Full architecture |
