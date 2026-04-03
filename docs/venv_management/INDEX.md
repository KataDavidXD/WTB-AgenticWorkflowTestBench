# UV Venv Management

**Last Updated:** 2026-01-27  
**Parent:** [Project_Init/INDEX.md](../Project_Init/INDEX.md)

---

## 1. Structure

### 1.1 External Service (`uv_venv_manager/`)

```
uv_venv_manager/
├── src/
│   ├── domain/                  # SOLID interfaces
│   │   ├── interfaces.py        # IVenvExecutor, IMetadataRepository
│   │   ├── exceptions.py        # Domain exceptions
│   │   └── value_objects.py     # EnvId, EnvStatus, EnvMetadata
│   │
│   ├── services/                # Application services
│   │   ├── env_manager.py       # EnvManager (lifecycle)
│   │   ├── uv_executor.py       # UVCommandExecutor (UV CLI)
│   │   ├── dep_manager.py       # DependencyManager
│   │   └── lock_manager.py      # AsyncIO lock management
│   │
│   ├── infrastructure/          # Implementations
│   │   ├── metadata_repository.py
│   │   └── event_publisher.py
│   │
│   ├── grpc_servicer.py         # gRPC service
│   └── api.py                   # FastAPI application
│
└── docs/
    ├── ARD.md                   # Architecture Decision Record
    ├── PRD.md                   # Product Requirements
    └── TRD.md                   # Technical Requirements
```

### 1.2 WTB Integration (`wtb/infrastructure/environment/`)

```
wtb/infrastructure/environment/
└── providers.py                 # IEnvironmentProvider implementations
    ├── GrpcEnvironmentProvider  # gRPC client to UV Venv Manager
    └── InMemoryEnvironmentProvider  # Testing
```

### 1.3 Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    UV VENV MANAGER ARCHITECTURE                              │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   WTB Core                                                                   │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │   RayBatchRunner                                                     │   │
│   │          │                                                           │   │
│   │          ▼                                                           │   │
│   │   IEnvironmentProvider                                               │   │
│   │   ├── create_environment(variant_id, config) → env_path, python     │   │
│   │   ├── get_runtime_env(variant_id) → venv_path, env_vars             │   │
│   │   └── cleanup_environment(variant_id)                               │   │
│   │          │                                                           │   │
│   │          ▼                                                           │   │
│   │   GrpcEnvironmentProvider ──────────► gRPC ──────────────────────┐  │   │
│   └─────────────────────────────────────────────────────────────────┼──┘   │
│                                                                      │      │
│   ┌──────────────────────────────────────────────────────────────────▼──┐   │
│   │                    UV VENV MANAGER SERVICE                           │   │
│   │                                                                      │   │
│   │   gRPC Server                                                        │   │
│   │   ├── CreateEnv           → UV create + install                     │   │
│   │   ├── SyncEnv             → UV sync from lockfile                   │   │
│   │   ├── AddDeps             → UV add packages                         │   │
│   │   ├── DeleteEnv           → Cleanup                                 │   │
│   │   └── ...                                                           │   │
│   │                                                                      │   │
│   │   Storage:                                                           │   │
│   │   envs/{workflow}_{node}_{version}/                                 │   │
│   │   ├── pyproject.toml                                                │   │
│   │   ├── uv.lock                                                       │   │
│   │   └── .venv/                                                        │   │
│   │                                                                      │   │
│   └──────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1.4 Core Principle

```
✅ WHAT UV VENV MANAGER DOES:
• Create virtual environments with UV
• Manage dependencies (add, update, remove)
• Sync environments from lock files
• Export environment configurations
• Cleanup stale environments

❌ WHAT IT DOES NOT DO:
• Execute code (Ray's responsibility)
• Ray integration (handled by WTB)
• Node execution orchestration
```

---

## 2. Issues

### 2.1 Active Issues

| ID | Issue | Priority | Status |
|----|-------|----------|--------|
| VM-001 | None identified | - | - |

### 2.2 Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Code execution | Removed `RunCode` RPC | Nodes are complete projects, not snippets |
| Ray integration | In WTB, not UV Manager | Clear service boundaries |
| Service pattern | gRPC (like FileTracker) | Consistent integration |

---

## 3. Gap Analysis (Brief)

| Design Intent | Implementation | Gap |
|--------------|----------------|-----|
| Environment provisioning only | ✅ Implemented | None |
| gRPC service pattern | ✅ Like FileTracker | None |
| UV CLI integration | ✅ `UVCommandExecutor` | None |
| Domain events | ✅ 11 event types | None |
| WTB integration | ✅ `GrpcEnvironmentProvider` | None |

**Overall:** Full implementation, no gaps identified.

---

## 4. Domain Events

| Event | Published When |
|-------|----------------|
| `EnvironmentCreationStartedEvent` | Creation begins |
| `EnvironmentCreatedEvent` | Creation succeeds |
| `EnvironmentCreationFailedEvent` | Creation fails |
| `EnvironmentDeletedEvent` | Deleted |
| `DependenciesAddedEvent` | Packages added |
| `DependenciesUpdatedEvent` | Packages upgraded |
| `DependenciesRemovedEvent` | Packages removed |
| `EnvironmentSyncedEvent` | Sync succeeds |
| `EnvironmentCleanupCompletedEvent` | Cleanup done |

---

## 5. SOLID Compliance

| Principle | Implementation |
|-----------|----------------|
| **SRP** | EnvManager handles lifecycle; UVExecutor handles CLI |
| **OCP** | New event publishers via interface |
| **LSP** | All providers implement IEnvironmentProvider |
| **ISP** | Focused interfaces per concern |
| **DIP** | High-level depends on abstractions |

---

## 6. Related Documents

| Document | Description |
|----------|-------------|
| [../Project_Init/INDEX.md](../Project_Init/INDEX.md) | Main documentation |
| [../Ray/INDEX.md](../Ray/INDEX.md) | Ray integration |
| [uv_venv_manager/docs/ARD.md](../../uv_venv_manager/docs/ARD.md) | Architecture decisions |
