# Event Bus & Audit Session Documentation

## Overview

本目录包含 WTB 与 AgentGit 的 Event Bus 和 Audit Trail 集成设计文档。

## Documents

| Document | Purpose | Status |
|----------|---------|--------|
| [WTB_EVENTBUS_AUDIT_DESIGN.md](./WTB_EVENTBUS_AUDIT_DESIGN.md) | 完整设计文档：AgentGit分析 + WTB集成方案 | ✅ Complete |

## Quick Navigation

### AgentGit 分析

- [§1. AgentGit Event Bus 分析](./WTB_EVENTBUS_AUDIT_DESIGN.md#1-agentgit-event-bus-分析) - 架构、设计特点、源码分析
- [§2. AgentGit Audit Trail 分析](./WTB_EVENTBUS_AUDIT_DESIGN.md#2-agentgit-audit-trail-分析) - AuditEvent、AuditTrail、LangChain集成

### WTB 设计

- [§4. WTB Event Bus & Audit 设计方案](./WTB_EVENTBUS_AUDIT_DESIGN.md#4-wtb-event-bus--audit-设计方案) - 架构总览、边界划分
- [§5. 实现设计](./WTB_EVENTBUS_AUDIT_DESIGN.md#5-实现设计) - WTBEventBus、WTBAuditTrail、AuditEventListener 代码
- [§6. 使用示例](./WTB_EVENTBUS_AUDIT_DESIGN.md#6-使用示例) - 基本使用、AgentGit集成

### 实施

- [§7. 实施计划](./WTB_EVENTBUS_AUDIT_DESIGN.md#7-实施计划) - 三阶段计划
- [§8. 测试策略](./WTB_EVENTBUS_AUDIT_DESIGN.md#8-测试策略) - 单元测试、集成测试

## Key Design Decisions

| 决策点 | 选择 | 理由 |
|--------|------|------|
| **Event Bus 复用** | 复用 AgentGit EventBus + WTB 包装 | 避免重复造轮子 |
| **Audit Trail 独立** | WTB 维护独立 Audit，可导入 AgentGit Audit | 关注点不同 |
| **事件桥接** | 通过 ACL 适配器 | 保持边界清晰 |
| **线程安全** | WTBEventBus 加锁 | 支持并行执行 |

## Architecture Summary

```
┌─────────────────────────────────────────────────────────────────┐
│                    Event Flow                                    │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│   ExecutionController ──► WTBEventBus ──► AuditEventListener    │
│          │                    │                   │              │
│          │                    │                   ▼              │
│          │                    │           WTBAuditTrail          │
│          │                    │                                  │
│          │              AgentGit Bridge                          │
│          │                    │                                  │
│          ▼                    ▼                                  │
│   AgentGitStateAdapter ◄── AgentGit EventBus                    │
│          │                                                       │
│          ▼                                                       │
│   AgentGit AuditTrail (stored in checkpoint.metadata)           │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## File Structure (Planned)

```
wtb/
├── domain/
│   ├── events/           # 现有 WTB Events
│   └── audit/            # 新增
│       └── audit_trail.py
│
├── infrastructure/
│   └── events/           # 新增
│       ├── event_bus.py
│       └── audit_listener.py
```

## Related Documents

- [../Project_Init/WORKFLOW_TEST_BENCH_ARCHITECTURE.md](../Project_Init/WORKFLOW_TEST_BENCH_ARCHITECTURE.md) - WTB 总体架构
- [../Adapter_and_WTB-Storage/ARCHITECTURE_FIX_DESIGN.md](../Adapter_and_WTB-Storage/ARCHITECTURE_FIX_DESIGN.md) - Outbox Pattern, IntegrityChecker

