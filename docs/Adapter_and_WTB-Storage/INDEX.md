# Adapter & WTB Storage - Documentation Index

**Implementation Status: ✅ COMPLETE** (2024-12-23)

## Documents in This Folder

| Document | Purpose | Status |
|----------|---------|--------|
| [AGENTGIT_STATE_ADAPTER_DESIGN.md](./AGENTGIT_STATE_ADAPTER_DESIGN.md) | AgentGit state adapter - bridges WTB ↔ AgentGit | ✅ Implemented |
| [WTB_PERSISTENCE_DESIGN.md](./WTB_PERSISTENCE_DESIGN.md) | WTB storage abstraction (InMemory + SQLAlchemy) | ✅ Implemented |
| [ARCHITECTURE_FIX_DESIGN.md](./ARCHITECTURE_FIX_DESIGN.md) | **架构修复设计** - Outbox Pattern, IntegrityChecker, 充血模型 | 📋 Designed |

## Key Concepts

### State Adapter (IStateAdapter)
- **InMemoryStateAdapter**: For unit tests, no persistence
- **AgentGitStateAdapter**: Production, uses real AgentGit checkpoints

### WTB Persistence (IUnitOfWork)
- **InMemoryUnitOfWork**: For unit tests, Dict-based storage
- **SQLAlchemyUnitOfWork**: Production, SQLite or PostgreSQL

## Selection

```python
# Testing
state_adapter = InMemoryStateAdapter()
uow = UnitOfWorkFactory.create(mode="inmemory")

# Production
state_adapter = AgentGitStateAdapter(agentgit_db_path="data/agentgit.db")
uow = UnitOfWorkFactory.create(mode="sqlalchemy", db_url="sqlite:///data/wtb.db")
```

## Architecture Fixes (NEW)

基于架构审查发现的关键问题，设计了以下修复方案：

| 优先级 | 问题 | 解决方案 | 文档 |
|--------|------|----------|------|
| **P0** | 跨库事务一致性 | Outbox Pattern | [ARCHITECTURE_FIX_DESIGN.md](./ARCHITECTURE_FIX_DESIGN.md#2-p0-outbox-pattern-实现设计) |
| **P0** | 数据完整性 | IntegrityChecker | [ARCHITECTURE_FIX_DESIGN.md](./ARCHITECTURE_FIX_DESIGN.md#3-p0-integritychecker-设计) |
| **P1** | 领域模型贫血 | Rich Domain Model | [ARCHITECTURE_FIX_DESIGN.md](./ARCHITECTURE_FIX_DESIGN.md#4-p1-充血领域模型设计) |

### Outbox Pattern 概述

```
┌─────────────────────────────────────────────────────────────────────┐
│  业务数据 + Outbox 事件 ────────► 同一事务写入 WTB DB                │
│                                        │                            │
│                                   Outbox Processor (后台)           │
│                                        │                            │
│                                        ▼                            │
│                                   AgentGit DB                       │
│                                   (验证/同步)                        │
└─────────────────────────────────────────────────────────────────────┘
```

## See Also
- [../Project_Init/INDEX.md](../Project_Init/INDEX.md) - Main documentation index

