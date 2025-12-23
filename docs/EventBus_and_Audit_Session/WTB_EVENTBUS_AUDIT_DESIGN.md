# WTB Event Bus & Audit Trail Design

## Executive Summary

本文档分析 AgentGit 的 Event Bus 和 Audit Trail 机制，并设计 WTB 如何与之集成，同时保持适当的边界隔离。

### 核心设计决策

| 问题 | 决策 | 理由 |
|------|------|------|
| **复用 vs 独立** | 复用 AgentGit EventBus，扩展 WTB Events | 避免重复造轮子，利用已验证的机制 |
| **Audit Trail 归属** | WTB 维护自己的 Audit，桥接 AgentGit Audit | WTB 和 AgentGit 有不同关注点 |
| **跨系统事件** | 通过 ACL 适配器桥接 | 保持边界清晰 |
| **持久化** | WTB Audit 存 WTB DB，AgentGit Audit 存 checkpoint metadata | 遵循数据归属原则 |

---

## 1. AgentGit Event Bus 分析

### 1.1 架构概览

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                          AgentGit Event Bus 架构                                 │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│   ┌──────────────────────────────────────────────────────────────────────────┐  │
│   │                         agentgit.events.event_bus                         │  │
│   │                                                                           │  │
│   │  ┌─────────────────────┐    ┌──────────────────────────────────────────┐ │  │
│   │  │    DomainEvent      │    │              EventBus                     │ │  │
│   │  │    (Abstract Base)  │    │                                          │ │  │
│   │  │                     │    │  Methods:                                │ │  │
│   │  │  • timestamp        │    │  ├── subscribe(event_type, handler)      │ │  │
│   │  │  • event_id         │    │  ├── unsubscribe(event_type, handler)    │ │  │
│   │  │                     │    │  ├── publish(event)                      │ │  │
│   │  │                     │    │  ├── publish_all(events)                 │ │  │
│   │  └─────────────────────┘    │  ├── get_event_history(event_type?)      │ │  │
│   │                             │  ├── clear_history()                     │ │  │
│   │                             │  └── get_subscriber_count(event_type?)   │ │  │
│   │                             │                                          │ │  │
│   │                             │  Internal:                               │ │  │
│   │                             │  ├── _subscribers: Dict[Type, List[Fn]]  │ │  │
│   │                             │  └── _event_history: List[DomainEvent]   │ │  │
│   │                             │      (max 100 events)                    │ │  │
│   │                             └──────────────────────────────────────────┘ │  │
│   │                                                                           │  │
│   │  Global Functions:                                                        │  │
│   │  ├── get_global_event_bus() → EventBus (singleton)                       │  │
│   │  └── set_global_event_bus(bus)                                            │  │
│   │                                                                           │  │
│   └──────────────────────────────────────────────────────────────────────────┘  │
│                                                                                  │
│   ┌──────────────────────────────────────────────────────────────────────────┐  │
│   │                      agentgit.events.agent_events                         │  │
│   │                                                                           │  │
│   │  Predefined Events (all extend DomainEvent):                             │  │
│   │                                                                           │  │
│   │  ┌─────────────────────────┐  ┌─────────────────────────────┐           │  │
│   │  │ CheckpointCreatedEvent  │  │ CheckpointDeletedEvent      │           │  │
│   │  │ • checkpoint_id         │  │ • checkpoint_id             │           │  │
│   │  │ • session_id            │  │ • session_id                │           │  │
│   │  │ • checkpoint_name       │  └─────────────────────────────┘           │  │
│   │  │ • is_auto               │                                            │  │
│   │  │ • tool_count            │  ┌─────────────────────────────┐           │  │
│   │  └─────────────────────────┘  │ ToolExecutedEvent           │           │  │
│   │                               │ • tool_name                 │           │  │
│   │  ┌─────────────────────────┐  │ • args                      │           │  │
│   │  │ SessionCreatedEvent     │  │ • result                    │           │  │
│   │  │ • session_id            │  │ • success                   │           │  │
│   │  │ • external_session_id   │  │ • error_message             │           │  │
│   │  │ • is_branch             │  │ • duration_ms               │           │  │
│   │  │ • parent_session_id     │  └─────────────────────────────┘           │  │
│   │  └─────────────────────────┘                                            │  │
│   │                               ┌─────────────────────────────┐           │  │
│   │  ┌─────────────────────────┐  │ ToolReversedEvent           │           │  │
│   │  │ RollbackPerformedEvent  │  │ • tool_name                 │           │  │
│   │  │ • checkpoint_id         │  │ • success                   │           │  │
│   │  │ • original_session_id   │  │ • error_message             │           │  │
│   │  │ • new_session_id        │  └─────────────────────────────┘           │  │
│   │  │ • tools_reversed        │                                            │  │
│   │  └─────────────────────────┘                                            │  │
│   │                                                                           │  │
│   └──────────────────────────────────────────────────────────────────────────┘  │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 设计特点分析

| 特性 | 实现 | 评价 |
|------|------|------|
| **模式** | Publish-Subscribe | ✅ 标准事件驱动 |
| **同步性** | 同步发布 | ⚠️ 订阅者阻塞发布者 |
| **错误处理** | 捕获异常，继续通知 | ✅ 容错但静默 |
| **历史** | 内存存储，最多100条 | ⚠️ 无持久化 |
| **线程安全** | 无锁设计 | ⚠️ 需要外部同步 |
| **全局单例** | 可选 | ✅ 灵活 |

### 1.3 源码分析

```python
# agentgit/events/event_bus.py (核心逻辑)

class EventBus:
    def __init__(self):
        self._subscribers: Dict[Type[DomainEvent], List[Callable]] = {}
        self._event_history: List[DomainEvent] = []
        self._max_history = 100
    
    def publish(self, event: DomainEvent):
        # 1. 存入历史
        self._event_history.append(event)
        if len(self._event_history) > self._max_history:
            self._event_history.pop(0)
        
        # 2. 通知订阅者（同步）
        event_type = type(event)
        if event_type in self._subscribers:
            for handler in self._subscribers[event_type]:
                try:
                    handler(event)
                except Exception as e:
                    # 静默处理，不中断其他订阅者
                    print(f"Error in event handler: {e}")
```

---

## 2. AgentGit Audit Trail 分析

### 2.1 架构概览

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         AgentGit Audit Trail 架构                                │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│   ┌─────────────────────────────────────────────────────────────────────────┐   │
│   │                     agentgit.audit.audit_trail                           │   │
│   │                                                                          │   │
│   │  ┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐ │   │
│   │  │    EventType     │     │  EventSeverity   │     │   AuditEvent     │ │   │
│   │  │    (Enum)        │     │     (Enum)       │     │   (Dataclass)    │ │   │
│   │  │                  │     │                  │     │                  │ │   │
│   │  │ • TOOL_START     │     │ • INFO           │     │ • timestamp      │ │   │
│   │  │ • TOOL_SUCCESS   │     │ • WARNING        │     │ • event_type     │ │   │
│   │  │ • TOOL_ERROR     │     │ • ERROR          │     │ • severity       │ │   │
│   │  │ • AGENT_DECISION │     │ • SUCCESS        │     │ • message        │ │   │
│   │  │ • LLM_CALL       │     │                  │     │ • details: Dict  │ │   │
│   │  │ • CHECKPOINT_    │     └──────────────────┘     │ • error?: str    │ │   │
│   │  │     CREATED      │                              │ • duration_ms?   │ │   │
│   │  │ • ROLLBACK       │                              │                  │ │   │
│   │  └──────────────────┘                              │ Methods:         │ │   │
│   │                                                    │ • to_dict()      │ │   │
│   │                                                    │ • format_user_   │ │   │
│   │                                                    │     friendly()   │ │   │
│   │                                                    └──────────────────┘ │   │
│   │                                                                          │   │
│   │  ┌─────────────────────────────────────────────────────────────────────┐│   │
│   │  │                         AuditTrail                                   ││   │
│   │  │                                                                      ││   │
│   │  │  Attributes:                                                        ││   │
│   │  │  ├── events: List[AuditEvent]                                       ││   │
│   │  │  └── session_id: Optional[str]                                      ││   │
│   │  │                                                                      ││   │
│   │  │  Methods:                                                           ││   │
│   │  │  ├── add_event(event)                                               ││   │
│   │  │  ├── get_errors() → List[AuditEvent]                                ││   │
│   │  │  ├── get_tool_events() → List[AuditEvent]                           ││   │
│   │  │  ├── get_summary() → Dict                                           ││   │
│   │  │  ├── format_user_display(show_all=False) → str                      ││   │
│   │  │  ├── to_dict() → Dict                                               ││   │
│   │  │  └── from_dict(data) → AuditTrail  [classmethod]                    ││   │
│   │  │                                                                      ││   │
│   │  └─────────────────────────────────────────────────────────────────────┘│   │
│   │                                                                          │   │
│   └─────────────────────────────────────────────────────────────────────────┘   │
│                                                                                  │
│   ┌─────────────────────────────────────────────────────────────────────────┐   │
│   │                    agentgit.audit.audit_callback                         │   │
│   │                                                                          │   │
│   │  ┌─────────────────────────────────────────────────────────────────────┐│   │
│   │  │              AuditTrailCallback (extends BaseCallbackHandler)        ││   │
│   │  │                                                                      ││   │
│   │  │  Purpose: LangChain 集成，自动捕获工具和 LLM 调用                      ││   │
│   │  │                                                                      ││   │
│   │  │  Events Captured:                                                   ││   │
│   │  │  ├── on_tool_start(serialized, input_str, run_id)                   ││   │
│   │  │  ├── on_tool_end(output, run_id)                                    ││   │
│   │  │  ├── on_tool_error(error, run_id)                                   ││   │
│   │  │  ├── on_llm_start(serialized, prompts, run_id)                      ││   │
│   │  │  ├── on_llm_end(response, run_id)                                   ││   │
│   │  │  └── on_llm_error(error, run_id)                                    ││   │
│   │  │                                                                      ││   │
│   │  │  Internal State:                                                    ││   │
│   │  │  ├── _tool_start_times: Dict[run_id, float]  (for duration calc)    ││   │
│   │  │  └── _llm_start_times: Dict[run_id, float]                          ││   │
│   │  │                                                                      ││   │
│   │  └─────────────────────────────────────────────────────────────────────┘│   │
│   │                                                                          │   │
│   └─────────────────────────────────────────────────────────────────────────┘   │
│                                                                                  │
│   典型使用流程:                                                                  │
│   ══════════════                                                                 │
│   1. trail = AuditTrail(session_id="session_123")                              │
│   2. callback = AuditTrailCallback(trail)                                       │
│   3. agent.run(config={"callbacks": [callback]})  # LangChain 自动捕获         │
│   4. checkpoint.metadata["audit_trail"] = trail.to_dict()  # 存入 checkpoint   │
│   5. 恢复时: trail = AuditTrail.from_dict(metadata["audit_trail"])             │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 关键设计

| 特性 | 实现 | 用途 |
|------|------|------|
| **序列化** | `to_dict()` / `from_dict()` | 可存入 checkpoint metadata |
| **用户友好展示** | `format_user_display()` | 带图标的格式化输出 |
| **统计摘要** | `get_summary()` | 工具调用成功率等 |
| **LangChain 集成** | `AuditTrailCallback` | 自动捕获无需手动记录 |

---

## 3. WTB 当前状态

### 3.1 已有的 Domain Events

```
wtb/domain/events/
├── __init__.py
├── execution_events.py    # WTBEvent, ExecutionStarted/Paused/Resumed/Completed/Failed/Cancelled
├── node_events.py         # NodeEvent, NodeStarted/Completed/Failed/Skipped
└── checkpoint_events.py   # CheckpointEvent, CheckpointCreated/RollbackPerformed/BranchCreated
```

### 3.2 当前问题

| 问题 | 描述 |
|------|------|
| **无 Event Bus** | WTB 定义了事件但没有发布机制 |
| **无 Audit Trail** | 缺少 WTB 级别的审计追踪 |
| **无桥接** | AgentGit 事件与 WTB 事件独立 |
| **无持久化** | 事件历史不持久 |

---

## 4. WTB Event Bus & Audit 设计方案

### 4.1 架构总览

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                    WTB + AgentGit Event Bus & Audit 集成架构                     │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│   ┌─────────────────────────────────────────────────────────────────────────┐   │
│   │                         应用层 (Application Layer)                        │   │
│   │                                                                           │   │
│   │   ExecutionController ─────┬───── NodeReplacer ───── BatchTestRunner     │   │
│   │         │                  │                                              │   │
│   │         │ publish          │                                              │   │
│   │         ▼                  ▼                                              │   │
│   │   ┌──────────────────────────────────────────────────────────────────┐   │   │
│   │   │                    WTB Event Bus Facade                           │   │   │
│   │   │                                                                   │   │   │
│   │   │  ┌──────────────────────────────────────────────────────────────┐│   │   │
│   │   │  │              WTBEventBus (wraps AgentGit EventBus)           ││   │   │
│   │   │  │                                                              ││   │   │
│   │   │  │  • 发布 WTB Events (Execution, Node, Checkpoint)             ││   │   │
│   │   │  │  • 桥接 AgentGit Events (通过 adapter)                        ││   │   │
│   │   │  │  • 线程安全包装                                               ││   │   │
│   │   │  │  • 可选持久化钩子                                             ││   │   │
│   │   │  │                                                              ││   │   │
│   │   │  └──────────────────────────────────────────────────────────────┘│   │   │
│   │   │                              │                                    │   │   │
│   │   │              ┌───────────────┼───────────────┐                   │   │   │
│   │   │              │               │               │                   │   │   │
│   │   │              ▼               ▼               ▼                   │   │   │
│   │   │  ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────────┐│   │   │
│   │   │  │ WTB Audit       │ │ Metrics Handler │ │ Persistence Handler ││   │   │
│   │   │  │ Listener        │ │ (future)        │ │ (future)            ││   │   │
│   │   │  └─────────────────┘ └─────────────────┘ └─────────────────────┘│   │   │
│   │   │           │                                                      │   │   │
│   │   └───────────│──────────────────────────────────────────────────────┘   │   │
│   │               │                                                           │   │
│   │               ▼                                                           │   │
│   │   ┌────────────────────────────────────────────────────────────────────┐ │   │
│   │   │                      WTB Audit Trail                                │ │   │
│   │   │                                                                     │ │   │
│   │   │  ┌───────────────────────────────────────────────────────────────┐ │ │   │
│   │   │  │                    WTBAuditTrail                               │ │ │   │
│   │   │  │                                                                │ │ │   │
│   │   │  │  关注点:                                                       │ │ │   │
│   │   │  │  • Execution 生命周期 (start/pause/resume/complete/fail)       │ │ │   │
│   │   │  │  • Node 执行详情                                               │ │ │   │
│   │   │  │  • Checkpoint/Rollback 操作                                    │ │ │   │
│   │   │  │  • Variant testing 结果                                        │ │ │   │
│   │   │  │                                                                │ │ │   │
│   │   │  │  Methods:                                                     │ │ │   │
│   │   │  │  ├── record_execution_event(event)                            │ │ │   │
│   │   │  │  ├── record_node_event(event)                                 │ │ │   │
│   │   │  │  ├── import_agentgit_audit(agentgit_audit)                    │ │ │   │
│   │   │  │  ├── to_dict() / from_dict()                                  │ │ │   │
│   │   │  │  └── format_display()                                         │ │ │   │
│   │   │  │                                                                │ │ │   │
│   │   │  └───────────────────────────────────────────────────────────────┘ │ │   │
│   │   │                               │                                     │ │   │
│   │   │                               ▼                                     │ │   │
│   │   │                 ┌─────────────────────────────┐                    │ │   │
│   │   │                 │    WTB DB: wtb_audit_log    │                    │ │   │
│   │   │                 │    (可选持久化)              │                    │ │   │
│   │   │                 └─────────────────────────────┘                    │ │   │
│   │   │                                                                     │ │   │
│   │   └─────────────────────────────────────────────────────────────────────┘ │   │
│   │                                                                           │   │
│   └───────────────────────────────────────────────────────────────────────────┘   │
│                                                                                    │
│   ┌───────────────────────────────────────────────────────────────────────────┐   │
│   │                     Anti-Corruption Layer (Adapter)                        │   │
│   │                                                                            │   │
│   │   AgentGitStateAdapter                                                     │   │
│   │   ├── 监听 AgentGit EventBus (CheckpointCreated, Rollback, etc.)          │   │
│   │   ├── 转换为 WTB Events 并发布                                             │   │
│   │   └── 传递 AgentGit AuditTrail 给 WTB                                      │   │
│   │                                                                            │   │
│   │   ┌────────────────────────────────────────────────────────────────────┐  │   │
│   │   │                    Event Bridging Strategy                          │  │   │
│   │   │                                                                     │  │   │
│   │   │   AgentGit Event              →          WTB Event                  │  │   │
│   │   │   ════════════════               ════════════════════               │  │   │
│   │   │   CheckpointCreatedEvent   →    WTB.CheckpointCreatedEvent         │  │   │
│   │   │   RollbackPerformedEvent   →    WTB.RollbackPerformedEvent         │  │   │
│   │   │   SessionCreatedEvent      →    WTB.BranchCreatedEvent             │  │   │
│   │   │   ToolExecutedEvent        →    (记录到 Audit, 不转换)              │  │   │
│   │   │                                                                     │  │   │
│   │   └────────────────────────────────────────────────────────────────────┘  │   │
│   │                                                                            │   │
│   └───────────────────────────────────────────────────────────────────────────┘   │
│                                                                                    │
│   ┌───────────────────────────────────────────────────────────────────────────┐   │
│   │                        AgentGit (Bounded Context)                          │   │
│   │                                                                            │   │
│   │   ┌──────────────────────┐      ┌──────────────────────┐                  │   │
│   │   │   AgentGit EventBus  │      │  AgentGit AuditTrail │                  │   │
│   │   │   (原生)              │      │  (per session)       │                  │   │
│   │   │                      │      │                      │                  │   │
│   │   │   • 工具级事件         │      │  • 工具执行详情       │                  │   │
│   │   │   • Checkpoint 事件   │      │  • LLM 调用           │                  │   │
│   │   │   • Session 事件      │      │  • 存入 checkpoint   │                  │   │
│   │   │                      │      │    metadata          │                  │   │
│   │   └──────────────────────┘      └──────────────────────┘                  │   │
│   │                                                                            │   │
│   └───────────────────────────────────────────────────────────────────────────┘   │
│                                                                                    │
└────────────────────────────────────────────────────────────────────────────────────┘
```

### 4.2 边界划分原则

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                            关注点分离 (Separation of Concerns)                   │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│   ┌────────────────────────────────┐    ┌────────────────────────────────────┐  │
│   │         AgentGit Audit          │    │            WTB Audit               │  │
│   │                                 │    │                                    │  │
│   │  关注:                          │    │  关注:                             │  │
│   │  ├── 单个工具的执行细节          │    │  ├── 工作流整体执行状态            │  │
│   │  ├── LLM 调用和决策过程          │    │  ├── 节点间的状态转换              │  │
│   │  ├── 单次 Checkpoint 操作        │    │  ├── 多版本测试结果对比            │  │
│   │  └── 工具反转 (rollback内部)     │    │  ├── 断点和暂停历史                │  │
│   │                                 │    │  └── 跨 session 的操作追踪         │  │
│   │  粒度: 工具级 (fine-grained)    │    │                                    │  │
│   │                                 │    │  粒度: 节点级 + 执行级              │  │
│   │  存储: checkpoint.metadata      │    │                                    │  │
│   │                                 │    │  存储: WTB DB / Execution context  │  │
│   └────────────────────────────────┘    └────────────────────────────────────┘  │
│                                                                                  │
│   ╔═══════════════════════════════════════════════════════════════════════════╗ │
│   ║                          桥接规则 (Bridging Rules)                         ║ │
│   ╠═══════════════════════════════════════════════════════════════════════════╣ │
│   ║                                                                            ║ │
│   ║  1. WTB 不直接读取 AgentGit Audit → 通过 Adapter 提供摘要                  ║ │
│   ║  2. AgentGit 不感知 WTB 存在 → 单向依赖                                    ║ │
│   ║  3. WTB 可在需要时 "导入" AgentGit Audit 快照 → 用于详细调试               ║ │
│   ║  4. 事件桥接通过 Adapter 完成 → 不污染任一边界                             ║ │
│   ║                                                                            ║ │
│   ╚═══════════════════════════════════════════════════════════════════════════╝ │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 5. 实现设计

### 5.1 WTB Event Bus Facade

```python
# wtb/infrastructure/events/event_bus.py

from typing import Type, Callable, List, Optional
from threading import Lock
from datetime import datetime

from agentgit.events.event_bus import EventBus as AgentGitEventBus, DomainEvent

from wtb.domain.events import WTBEvent


class WTBEventBus(AgentGitEventBus):
    """
    WTB Event Bus - Extends AgentGit EventBus with thread safety and WTB specifics.
    
    Design decisions:
    - Inherits from AgentGit EventBus to ensure all calls (even internal AgentGit ones)
      go through our thread-safe overrides when this is set as the global bus.
    - Adds Lock for thread safety in parallel execution.
    - Provides WTB-specific event types.
    - Optionally bridges AgentGit events to WTB listeners.
    
    Usage:
        bus = WTBEventBus()
        
        # Set as global bus for thread safety across the board
        set_global_event_bus(bus)
        
        # Subscribe to WTB events
        bus.subscribe(ExecutionStartedEvent, handle_execution_started)
    """
    
    def __init__(self, max_history: int = 1000):
        """Initialize WTB Event Bus.
        
        Args:
            max_history: Maximum events to keep in history (larger than AgentGit default)
        """
        super().__init__()
        self._max_history = max_history
        self._lock = Lock()  # Thread safety for parallel execution
        self._bridge_enabled = False
        self._bridge_handlers: List[Callable] = []

    def subscribe(
        self, 
        event_type: Type[DomainEvent], 
        handler: Callable[[DomainEvent], None]
    ) -> None:
        """Subscribe to an event type (Thread-safe)."""
        with self._lock:
            super().subscribe(event_type, handler)
    
    def unsubscribe(
        self, 
        event_type: Type[DomainEvent], 
        handler: Callable
    ) -> None:
        """Unsubscribe from an event type (Thread-safe)."""
        with self._lock:
            super().unsubscribe(event_type, handler)
    
    def publish(self, event: DomainEvent) -> None:
        """Publish an event to all subscribers (Thread-safe).
        
        Args:
            event: Event instance to publish
        """
        with self._lock:
            super().publish(event)
    
    def publish_all(self, events: List[DomainEvent]) -> None:
        """Publish multiple events atomically."""
        with self._lock:
            for event in events:
                super().publish(event)
    
    def get_history(
        self, 
        event_type: Optional[Type[DomainEvent]] = None,
        since: Optional[datetime] = None,
        limit: int = 100
    ) -> List[DomainEvent]:
        """Get event history with optional filtering."""
        with self._lock:
            events = self.get_event_history(event_type)
            
            if since:
                events = [e for e in events if e.timestamp >= since]
            
            return list(reversed(events[-limit:]))
    
    # ═══════════════════════════════════════════════════════════════
    # AgentGit Bridge
    # ═══════════════════════════════════════════════════════════════
    
    def enable_agentgit_bridge(
        self, 
        # No arg needed as we ARE the bus, but for compatibility/explicit config:
        event_mappings: Optional[dict] = None
    ) -> None:
        """Enable bridging of AgentGit-specific events to WTB-specific events.
        
        Since WTBEventBus inherits from AgentGitEventBus, it already receives all 
        AgentGit events. This method sets up the *translation* listeners that
        convert AgentGit events (e.g. CheckpointCreated) into WTB events.
        """
        if self._bridge_enabled:
            return

        from agentgit.events import (
            CheckpointCreatedEvent as AGCheckpointCreated,
            RollbackPerformedEvent as AGRollback,
            SessionCreatedEvent as AGSessionCreated,
        )
        
        # Default mappings
        default_mappings = {
            AGCheckpointCreated: self._bridge_checkpoint_created,
            AGRollback: self._bridge_rollback,
            AGSessionCreated: self._bridge_session_created,
        }
        
        mappings = event_mappings or default_mappings
        
        with self._lock:
            for ag_event_type, bridge_fn in mappings.items():
                handler = bridge_fn
                self._bridge_handlers.append((ag_event_type, handler))
                # We subscribe to ourselves to catch AG events and republish as WTB events
                self.subscribe(ag_event_type, handler)
            self._bridge_enabled = True
    
    def disable_agentgit_bridge(self) -> None:
        """Disable AgentGit event bridging."""
        with self._lock:
            if self._bridge_enabled:
                for event_type, handler in self._bridge_handlers:
                    self.unsubscribe(event_type, handler)
                self._bridge_handlers.clear()
                self._bridge_enabled = False
    
    def _bridge_checkpoint_created(self, ag_event) -> None:
        """Bridge AgentGit CheckpointCreated to WTB."""
        try:
            from wtb.domain.events import CheckpointCreatedEvent
            
            wtb_event = CheckpointCreatedEvent(
                checkpoint_id=ag_event.checkpoint_id,
                session_id=ag_event.session_id,
                checkpoint_name=ag_event.checkpoint_name,
                is_auto=ag_event.is_auto,
                tool_track_position=ag_event.tool_count,
            )
            self.publish(wtb_event)
        except Exception as e:
            # CRITICAL: Do not swallow bridge errors. Log to stderr/monitoring.
            import sys
            print(f"[CRITICAL] Bridge error in _bridge_checkpoint_created: {e}", file=sys.stderr)
    
    def _bridge_rollback(self, ag_event) -> None:
        """Bridge AgentGit Rollback to WTB."""
        try:
            from wtb.domain.events import RollbackPerformedEvent
            
            wtb_event = RollbackPerformedEvent(
                to_checkpoint_id=ag_event.checkpoint_id,
                session_id=ag_event.original_session_id,
                new_session_id=ag_event.new_session_id,
                tools_reversed=ag_event.tools_reversed,
            )
            self.publish(wtb_event)
        except Exception as e:
            import sys
            print(f"[CRITICAL] Bridge error in _bridge_rollback: {e}", file=sys.stderr)
    
    def _bridge_session_created(self, ag_event) -> None:
        """Bridge AgentGit SessionCreated (when is_branch) to WTB BranchCreated."""
        try:
            if ag_event.is_branch:
                from wtb.domain.events import BranchCreatedEvent
                
                wtb_event = BranchCreatedEvent(
                    new_session_id=ag_event.session_id,
                    parent_session_id=ag_event.parent_session_id,
                    branch_reason="rollback" if ag_event.parent_session_id else "variant_test",
                )
                self.publish(wtb_event)
        except Exception as e:
            import sys
            print(f"[CRITICAL] Bridge error in _bridge_session_created: {e}", file=sys.stderr)


# ═══════════════════════════════════════════════════════════════
# Global Instance (optional convenience)
# ═══════════════════════════════════════════════════════════════

_global_wtb_event_bus: Optional[WTBEventBus] = None


def get_wtb_event_bus() -> WTBEventBus:
    """Get global WTB event bus instance."""
    global _global_wtb_event_bus
    if _global_wtb_event_bus is None:
        _global_wtb_event_bus = WTBEventBus()
    return _global_wtb_event_bus


def set_wtb_event_bus(bus: WTBEventBus) -> None:
    """Set global WTB event bus instance."""
    global _global_wtb_event_bus
    _global_wtb_event_bus = bus
```

### 5.2 WTB Audit Trail

```python
# wtb/domain/audit/audit_trail.py

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Any, Optional
from enum import Enum

from wtb.domain.events import (
    WTBEvent,
    ExecutionStartedEvent, ExecutionCompletedEvent, ExecutionFailedEvent,
    ExecutionPausedEvent, ExecutionResumedEvent, ExecutionCancelledEvent,
    NodeStartedEvent, NodeCompletedEvent, NodeFailedEvent, NodeSkippedEvent,
    CheckpointCreatedEvent, RollbackPerformedEvent, BranchCreatedEvent,
)


class WTBAuditEventType(Enum):
    """WTB-specific audit event types."""
    # Execution lifecycle
    EXECUTION_STARTED = "execution_started"
    EXECUTION_PAUSED = "execution_paused"
    EXECUTION_RESUMED = "execution_resumed"
    EXECUTION_COMPLETED = "execution_completed"
    EXECUTION_FAILED = "execution_failed"
    EXECUTION_CANCELLED = "execution_cancelled"
    
    # Node lifecycle
    NODE_STARTED = "node_started"
    NODE_COMPLETED = "node_completed"
    NODE_FAILED = "node_failed"
    NODE_SKIPPED = "node_skipped"
    
    # Checkpoint operations
    CHECKPOINT_CREATED = "checkpoint_created"
    ROLLBACK_PERFORMED = "rollback_performed"
    BRANCH_CREATED = "branch_created"
    
    # State modifications
    STATE_MODIFIED = "state_modified"
    BREAKPOINT_HIT = "breakpoint_hit"
    
    # Variant testing
    VARIANT_STARTED = "variant_started"
    VARIANT_COMPLETED = "variant_completed"


class WTBAuditSeverity(Enum):
    """Severity levels for WTB audit events."""
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    DEBUG = "debug"


@dataclass
class WTBAuditEntry:
    """Single audit entry in WTB audit trail."""
    timestamp: datetime
    event_type: WTBAuditEventType
    severity: WTBAuditSeverity
    message: str
    execution_id: Optional[str] = None
    node_id: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    duration_ms: Optional[float] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "event_type": self.event_type.value,
            "severity": self.severity.value,
            "message": self.message,
            "execution_id": self.execution_id,
            "node_id": self.node_id,
            "details": self.details,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WTBAuditEntry":
        """Create from dictionary."""
        return cls(
            timestamp=datetime.fromisoformat(data["timestamp"]),
            event_type=WTBAuditEventType(data["event_type"]),
            severity=WTBAuditSeverity(data["severity"]),
            message=data["message"],
            execution_id=data.get("execution_id"),
            node_id=data.get("node_id"),
            details=data.get("details", {}),
            error=data.get("error"),
            duration_ms=data.get("duration_ms"),
        )
    
    def format_display(self) -> str:
        """Format for user-friendly display."""
        time_str = self.timestamp.strftime("%H:%M:%S.%f")[:-3]
        
        icons = {
            WTBAuditSeverity.INFO: "ℹ️ ",
            WTBAuditSeverity.SUCCESS: "✅",
            WTBAuditSeverity.WARNING: "⚠️ ",
            WTBAuditSeverity.ERROR: "❌",
            WTBAuditSeverity.DEBUG: "🔍",
        }
        icon = icons.get(self.severity, "  ")
        
        msg = f"[{time_str}] {icon} {self.message}"
        
        if self.node_id:
            msg += f" (node: {self.node_id})"
        
        if self.duration_ms:
            msg += f" [{self.duration_ms:.1f}ms]"
        
        if self.error:
            msg += f"\n           └─ Error: {self.error}"
        
        return msg


@dataclass
class WTBAuditTrail:
    """
    WTB Audit Trail - Tracks workflow execution at WTB abstraction level.
    
    Differs from AgentGit AuditTrail:
    - AgentGit: Tool-level, LLM calls, fine-grained
    - WTB: Node-level, execution lifecycle, checkpoint operations
    
    Memory Management:
    - This class implements a flushing mechanism to avoid memory bloat in long-running tests.
    - When used with AuditEventListener, events should be periodically flushed to IAuditLogRepository.
    """
    entries: List[WTBAuditEntry] = field(default_factory=list)
    execution_id: Optional[str] = None
    # ...
    
    def flush(self) -> List[WTBAuditEntry]:
        """Clear and return current entries for persistence."""
        current_entries = self.entries[:]
        self.entries.clear()
        return current_entries
    workflow_id: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    
    # Imported AgentGit audits (keyed by checkpoint_id or node_id)
    _agentgit_audits: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    
    def add_entry(self, entry: WTBAuditEntry) -> None:
        """Add an audit entry."""
        self.entries.append(entry)
    
    def record_event(self, event: WTBEvent) -> None:
        """Record a WTB domain event as an audit entry.
        
        Automatically maps event type to audit entry.
        """
        entry = self._event_to_entry(event)
        if entry:
            self.add_entry(entry)
    
    def _event_to_entry(self, event: WTBEvent) -> Optional[WTBAuditEntry]:
        """Convert WTB event to audit entry."""
        
        # Execution events
        if isinstance(event, ExecutionStartedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.EXECUTION_STARTED,
                severity=WTBAuditSeverity.INFO,
                message=f"Execution started for workflow '{event.workflow_name}'",
                execution_id=event.execution_id,
                details={"workflow_id": event.workflow_id, "breakpoints": event.breakpoints},
            )
        
        if isinstance(event, ExecutionCompletedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.EXECUTION_COMPLETED,
                severity=WTBAuditSeverity.SUCCESS,
                message=f"Execution completed ({event.nodes_executed} nodes, {event.checkpoints_created} checkpoints)",
                execution_id=event.execution_id,
                duration_ms=event.duration_ms,
                details={"final_state_keys": list(event.final_state.keys())},
            )
        
        if isinstance(event, ExecutionFailedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.EXECUTION_FAILED,
                severity=WTBAuditSeverity.ERROR,
                message=f"Execution failed at node '{event.failed_at_node}'",
                execution_id=event.execution_id,
                node_id=event.failed_at_node,
                error=f"{event.error_type}: {event.error_message}",
                duration_ms=event.duration_ms,
            )
        
        if isinstance(event, ExecutionPausedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.EXECUTION_PAUSED,
                severity=WTBAuditSeverity.INFO,
                message=f"Execution paused: {event.reason}",
                execution_id=event.execution_id,
                node_id=event.paused_at_node,
                details={"checkpoint_id": event.checkpoint_id},
                duration_ms=event.elapsed_time_ms,
            )
        
        if isinstance(event, ExecutionResumedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.EXECUTION_RESUMED,
                severity=WTBAuditSeverity.INFO,
                message=f"Execution resumed from node '{event.resume_from_node}'",
                execution_id=event.execution_id,
                node_id=event.resume_from_node,
                details={"state_modified": event.modified_state is not None},
            )
        
        if isinstance(event, ExecutionCancelledEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.EXECUTION_CANCELLED,
                severity=WTBAuditSeverity.WARNING,
                message=f"Execution cancelled: {event.reason}",
                execution_id=event.execution_id,
                node_id=event.cancelled_at_node,
                duration_ms=event.duration_ms,
            )
        
        # Node events
        if isinstance(event, NodeStartedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.NODE_STARTED,
                severity=WTBAuditSeverity.DEBUG,
                message=f"Node '{event.node_name}' started",
                execution_id=event.execution_id,
                node_id=event.node_id,
                details={"node_type": event.node_type, "checkpoint_id": event.entry_checkpoint_id},
            )
        
        if isinstance(event, NodeCompletedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.NODE_COMPLETED,
                severity=WTBAuditSeverity.SUCCESS,
                message=f"Node '{event.node_name}' completed ({event.tool_invocations} tool calls)",
                execution_id=event.execution_id,
                node_id=event.node_id,
                duration_ms=event.duration_ms,
                details={"checkpoint_id": event.exit_checkpoint_id},
            )
        
        if isinstance(event, NodeFailedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.NODE_FAILED,
                severity=WTBAuditSeverity.ERROR,
                message=f"Node '{event.node_name}' failed",
                execution_id=event.execution_id,
                node_id=event.node_id,
                error=f"{event.error_type}: {event.error_message}",
                duration_ms=event.duration_ms,
                details={"recoverable": event.recoverable},
            )
        
        if isinstance(event, NodeSkippedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.NODE_SKIPPED,
                severity=WTBAuditSeverity.INFO,
                message=f"Node '{event.node_name}' skipped: {event.reason}",
                execution_id=event.execution_id,
                node_id=event.node_id,
            )
        
        # Checkpoint events
        if isinstance(event, CheckpointCreatedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.CHECKPOINT_CREATED,
                severity=WTBAuditSeverity.INFO,
                message=f"Checkpoint #{event.checkpoint_id} created ({event.trigger_type})",
                execution_id=event.execution_id,
                node_id=event.node_id,
                details={
                    "checkpoint_name": event.checkpoint_name,
                    "is_auto": event.is_auto,
                    "has_file_commit": event.has_file_commit,
                },
            )
        
        if isinstance(event, RollbackPerformedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.ROLLBACK_PERFORMED,
                severity=WTBAuditSeverity.WARNING,
                message=f"Rolled back to checkpoint #{event.to_checkpoint_id} ({event.tools_reversed} tools reversed)",
                execution_id=event.execution_id,
                details={
                    "from_checkpoint": event.from_checkpoint_id,
                    "new_session_id": event.new_session_id,
                    "files_restored": event.files_restored,
                },
            )
        
        if isinstance(event, BranchCreatedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.BRANCH_CREATED,
                severity=WTBAuditSeverity.INFO,
                message=f"Branch created: session {event.new_session_id} from {event.parent_session_id}",
                execution_id=event.execution_id,
                details={"reason": event.branch_reason},
            )
        
        return None
    
    # ═══════════════════════════════════════════════════════════════
    # AgentGit Audit Integration
    # ═══════════════════════════════════════════════════════════════
    
    def import_agentgit_audit(
        self, 
        audit_dict: Dict[str, Any], 
        key: str
    ) -> None:
        """Import an AgentGit AuditTrail snapshot for detailed debugging.
        
        Args:
            audit_dict: AgentGit AuditTrail.to_dict() output
            key: Identifier (e.g., checkpoint_id or node_id) for retrieval
        """
        self._agentgit_audits[key] = audit_dict
    
    def get_agentgit_audit(self, key: str) -> Optional[Dict[str, Any]]:
        """Get imported AgentGit audit by key."""
        return self._agentgit_audits.get(key)
    
    # ═══════════════════════════════════════════════════════════════
    # Query Methods
    # ═══════════════════════════════════════════════════════════════
    
    def get_errors(self) -> List[WTBAuditEntry]:
        """Get all error entries."""
        return [e for e in self.entries if e.severity == WTBAuditSeverity.ERROR]
    
    def get_by_node(self, node_id: str) -> List[WTBAuditEntry]:
        """Get entries for a specific node."""
        return [e for e in self.entries if e.node_id == node_id]
    
    def get_by_type(self, event_type: WTBAuditEventType) -> List[WTBAuditEntry]:
        """Get entries by event type."""
        return [e for e in self.entries if e.event_type == event_type]
    
    def get_summary(self) -> Dict[str, Any]:
        """Get summary statistics."""
        return {
            "total_entries": len(self.entries),
            "execution_id": self.execution_id,
            "workflow_id": self.workflow_id,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_ms": (
                (self.ended_at - self.started_at).total_seconds() * 1000
                if self.started_at and self.ended_at else None
            ),
            "nodes_executed": len([
                e for e in self.entries 
                if e.event_type == WTBAuditEventType.NODE_COMPLETED
            ]),
            "nodes_failed": len([
                e for e in self.entries 
                if e.event_type == WTBAuditEventType.NODE_FAILED
            ]),
            "checkpoints_created": len([
                e for e in self.entries 
                if e.event_type == WTBAuditEventType.CHECKPOINT_CREATED
            ]),
            "rollbacks": len([
                e for e in self.entries 
                if e.event_type == WTBAuditEventType.ROLLBACK_PERFORMED
            ]),
            "errors": len(self.get_errors()),
            "agentgit_audits_imported": len(self._agentgit_audits),
        }
    
    # ═══════════════════════════════════════════════════════════════
    # Serialization
    # ═══════════════════════════════════════════════════════════════
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            "execution_id": self.execution_id,
            "workflow_id": self.workflow_id,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "entries": [e.to_dict() for e in self.entries],
            "agentgit_audits": self._agentgit_audits,
            "summary": self.get_summary(),
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WTBAuditTrail":
        """Create from dictionary."""
        trail = cls(
            execution_id=data.get("execution_id"),
            workflow_id=data.get("workflow_id"),
            started_at=datetime.fromisoformat(data["started_at"]) if data.get("started_at") else None,
            ended_at=datetime.fromisoformat(data["ended_at"]) if data.get("ended_at") else None,
        )
        
        for entry_data in data.get("entries", []):
            trail.entries.append(WTBAuditEntry.from_dict(entry_data))
        
        trail._agentgit_audits = data.get("agentgit_audits", {})
        
        return trail
    
    # ═══════════════════════════════════════════════════════════════
    # Display
    # ═══════════════════════════════════════════════════════════════
    
    def format_display(
        self, 
        show_all: bool = False, 
        include_debug: bool = False
    ) -> str:
        """Format for user-friendly display.
        
        Args:
            show_all: Show all entries (vs last 30)
            include_debug: Include DEBUG severity entries
        """
        if not self.entries:
            return "No audit entries recorded."
        
        output = "═══════════════════════════════════════════════════════\n"
        output += "                   WTB AUDIT TRAIL                     \n"
        output += "═══════════════════════════════════════════════════════\n\n"
        
        # Summary
        summary = self.get_summary()
        output += f"Execution: {summary['execution_id'] or 'N/A'}\n"
        output += f"Workflow:  {summary['workflow_id'] or 'N/A'}\n"
        if summary['duration_ms']:
            output += f"Duration:  {summary['duration_ms']:.0f}ms\n"
        output += "\n"
        
        output += f"Nodes: ✅ {summary['nodes_executed']} completed"
        if summary['nodes_failed']:
            output += f" | ❌ {summary['nodes_failed']} failed"
        output += "\n"
        
        output += f"Checkpoints: {summary['checkpoints_created']}"
        if summary['rollbacks']:
            output += f" | ↩️  {summary['rollbacks']} rollbacks"
        output += "\n"
        
        if summary['errors']:
            output += f"⚠️  Errors: {summary['errors']}\n"
        
        output += "\n───────────────────────────────────────────────────────\n"
        output += "                      TIMELINE                         \n"
        output += "───────────────────────────────────────────────────────\n\n"
        
        # Filter entries
        entries = self.entries
        if not include_debug:
            entries = [e for e in entries if e.severity != WTBAuditSeverity.DEBUG]
        
        if not show_all:
            entries = entries[-30:]
        
        for entry in entries:
            output += entry.format_display() + "\n"
        
        if not show_all and len(self.entries) > 30:
            output += f"\n... ({len(self.entries) - 30} more entries, use show_all=True)\n"
        
        return output
```

### 5.3 Audit Event Listener (Event Bus Integration)

```python
# wtb/infrastructure/events/audit_listener.py

from typing import Optional
from wtb.domain.events import WTBEvent
from wtb.domain.audit.audit_trail import WTBAuditTrail
from wtb.infrastructure.events.event_bus import WTBEventBus


class AuditEventListener:
    """
    Listens to WTB Event Bus and records events to Audit Trail.
    
    This provides automatic audit logging for all WTB events without
    requiring manual recording in each component.
    """
    
    def __init__(
        self, 
        event_bus: WTBEventBus, 
        audit_trail: WTBAuditTrail
    ):
        self._event_bus = event_bus
        self._audit_trail = audit_trail
        self._subscribed = False
    
    def start(self) -> None:
        """Start listening to events."""
        if self._subscribed:
            return
        
        # Subscribe to all WTB event types
        from wtb.domain.events import (
            ExecutionStartedEvent, ExecutionPausedEvent, ExecutionResumedEvent,
            ExecutionCompletedEvent, ExecutionFailedEvent, ExecutionCancelledEvent,
            NodeStartedEvent, NodeCompletedEvent, NodeFailedEvent, NodeSkippedEvent,
            CheckpointCreatedEvent, RollbackPerformedEvent, BranchCreatedEvent,
        )
        
        event_types = [
            ExecutionStartedEvent, ExecutionPausedEvent, ExecutionResumedEvent,
            ExecutionCompletedEvent, ExecutionFailedEvent, ExecutionCancelledEvent,
            NodeStartedEvent, NodeCompletedEvent, NodeFailedEvent, NodeSkippedEvent,
            CheckpointCreatedEvent, RollbackPerformedEvent, BranchCreatedEvent,
        ]
        
        for event_type in event_types:
            self._event_bus.subscribe(event_type, self._handle_event)
        
        self._subscribed = True
    
    def stop(self) -> None:
        """Stop listening to events."""
        if not self._subscribed:
            return
        
        from wtb.domain.events import (
            ExecutionStartedEvent, ExecutionPausedEvent, ExecutionResumedEvent,
            ExecutionCompletedEvent, ExecutionFailedEvent, ExecutionCancelledEvent,
            NodeStartedEvent, NodeCompletedEvent, NodeFailedEvent, NodeSkippedEvent,
            CheckpointCreatedEvent, RollbackPerformedEvent, BranchCreatedEvent,
        )
        
        event_types = [
            ExecutionStartedEvent, ExecutionPausedEvent, ExecutionResumedEvent,
            ExecutionCompletedEvent, ExecutionFailedEvent, ExecutionCancelledEvent,
            NodeStartedEvent, NodeCompletedEvent, NodeFailedEvent, NodeSkippedEvent,
            CheckpointCreatedEvent, RollbackPerformedEvent, BranchCreatedEvent,
        ]
        
        for event_type in event_types:
            self._event_bus.unsubscribe(event_type, self._handle_event)
        
        self._subscribed = False
    
    def _handle_event(self, event: WTBEvent) -> None:
        """Handle incoming event by recording to audit trail."""
        self._audit_trail.record_event(event)
    
    @property
    def audit_trail(self) -> WTBAuditTrail:
        """Get the audit trail being populated."""
        return self._audit_trail
```

---

## 6. 使用示例

### 6.1 基本使用

```python
from wtb.infrastructure.events.event_bus import WTBEventBus, get_wtb_event_bus
from wtb.domain.audit.audit_trail import WTBAuditTrail
from wtb.infrastructure.events.audit_listener import AuditEventListener
from wtb.domain.events import ExecutionStartedEvent, NodeCompletedEvent

# 1. Setup
event_bus = WTBEventBus()
audit_trail = WTBAuditTrail(execution_id="exec-001", workflow_id="wf-test")
listener = AuditEventListener(event_bus, audit_trail)
listener.start()

# 2. Publish events (typically done by ExecutionController)
event_bus.publish(ExecutionStartedEvent(
    execution_id="exec-001",
    workflow_id="wf-test",
    workflow_name="My Test Workflow",
))

event_bus.publish(NodeCompletedEvent(
    execution_id="exec-001",
    node_id="node-1",
    node_name="Process Data",
    duration_ms=150.5,
    tool_invocations=3,
))

# 3. View audit
print(audit_trail.format_display())

# Output:
# ═══════════════════════════════════════════════════════
#                    WTB AUDIT TRAIL                     
# ═══════════════════════════════════════════════════════
# 
# Execution: exec-001
# Workflow:  wf-test
# 
# Nodes: ✅ 1 completed
# Checkpoints: 0
# 
# ───────────────────────────────────────────────────────
#                       TIMELINE                         
# ───────────────────────────────────────────────────────
# 
# [14:30:45.123] ℹ️  Execution started for workflow 'My Test Workflow'
# [14:30:45.274] ✅ Node 'Process Data' completed (3 tool calls) [150.5ms]
```

### 6.2 与 AgentGit 集成

```python
from agentgit.events import get_global_event_bus as get_agentgit_bus
from agentgit.audit import AuditTrail as AgentGitAuditTrail

# Enable bridging
agentgit_bus = get_agentgit_bus()
wtb_bus = get_wtb_event_bus()
wtb_bus.enable_agentgit_bridge(agentgit_bus)

# Now AgentGit events will be bridged to WTB
# e.g., AgentGit CheckpointCreatedEvent → WTB CheckpointCreatedEvent

# Import AgentGit audit for detailed debugging
agentgit_audit = AgentGitAuditTrail(session_id="session-123")
# ... (agent runs, audit populated)

wtb_audit_trail.import_agentgit_audit(
    audit_dict=agentgit_audit.to_dict(),
    key="checkpoint-42"
)

# Later, retrieve for debugging
detailed_audit = wtb_audit_trail.get_agentgit_audit("checkpoint-42")
```

---

## 7. 实施计划

### Phase 1: Core Infrastructure (Week 1)

| Task | Priority | Status |
|------|----------|--------|
| Create `wtb/infrastructure/events/event_bus.py` | P0 | 🔲 |
| Create `wtb/domain/audit/audit_trail.py` | P0 | 🔲 |
| Create `wtb/infrastructure/events/audit_listener.py` | P0 | 🔲 |
| Unit tests for WTBEventBus | P0 | 🔲 |
| Unit tests for WTBAuditTrail | P0 | 🔲 |

### Phase 2: Integration (Week 2)

| Task | Priority | Status |
|------|----------|--------|
| Integrate EventBus into ExecutionController | P0 | 🔲 |
| Integrate EventBus into AgentGitStateAdapter | P0 | 🔲 |
| Enable AgentGit bridge in production config | P1 | 🔲 |
| Add audit persistence to WTB DB (optional) | P2 | 🔲 |

### Phase 3: Testing & Documentation (Week 3)

| Task | Priority | Status |
|------|----------|--------|
| Integration tests for event flow | P0 | 🔲 |
| Integration tests for audit trail | P0 | 🔲 |
| Update architecture docs | P1 | 🔲 |
| Performance testing (high event volume) | P2 | 🔲 |

---

## 8. 测试策略

### 8.1 Unit Tests

```python
# tests/test_wtb/test_event_bus.py

import pytest
from wtb.infrastructure.events.event_bus import WTBEventBus
from wtb.domain.events import ExecutionStartedEvent


class TestWTBEventBus:
    
    def test_subscribe_and_publish(self):
        bus = WTBEventBus()
        received = []
        
        bus.subscribe(ExecutionStartedEvent, lambda e: received.append(e))
        
        event = ExecutionStartedEvent(execution_id="test-1", workflow_id="wf-1")
        bus.publish(event)
        
        assert len(received) == 1
        assert received[0].execution_id == "test-1"
    
    def test_thread_safety(self):
        import threading
        bus = WTBEventBus()
        events_received = []
        lock = threading.Lock()
        
        def handler(e):
            with lock:
                events_received.append(e)
        
        bus.subscribe(ExecutionStartedEvent, handler)
        
        def publish_events():
            for i in range(100):
                bus.publish(ExecutionStartedEvent(execution_id=f"exec-{i}"))
        
        threads = [threading.Thread(target=publish_events) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert len(events_received) == 500
    
    def test_agentgit_bridge(self):
        from agentgit.events.event_bus import EventBus as AGEventBus
        from agentgit.events.agent_events import CheckpointCreatedEvent as AGCheckpoint
        
        ag_bus = AGEventBus()
        wtb_bus = WTBEventBus()
        
        wtb_events = []
        wtb_bus.subscribe(
            CheckpointCreatedEvent, 
            lambda e: wtb_events.append(e)
        )
        
        wtb_bus.enable_agentgit_bridge(ag_bus)
        
        # Publish AgentGit event
        from datetime import datetime
        ag_bus.publish(AGCheckpoint(
            timestamp=datetime.now(),
            checkpoint_id=42,
            session_id=1,
        ))
        
        assert len(wtb_events) == 1
        assert wtb_events[0].checkpoint_id == 42
```

### 8.2 Integration Tests

```python
# tests/test_wtb/test_audit_integration.py

import pytest
from wtb.infrastructure.events.event_bus import WTBEventBus
from wtb.domain.audit.audit_trail import WTBAuditTrail
from wtb.infrastructure.events.audit_listener import AuditEventListener
from wtb.domain.events import (
    ExecutionStartedEvent, NodeStartedEvent, NodeCompletedEvent,
    CheckpointCreatedEvent, ExecutionCompletedEvent,
)


class TestAuditIntegration:
    
    def test_full_execution_audit(self):
        # Setup
        bus = WTBEventBus()
        audit = WTBAuditTrail(execution_id="exec-1", workflow_id="wf-1")
        listener = AuditEventListener(bus, audit)
        listener.start()
        
        # Simulate execution
        bus.publish(ExecutionStartedEvent(
            execution_id="exec-1",
            workflow_id="wf-1",
            workflow_name="Test Flow",
        ))
        
        bus.publish(NodeStartedEvent(
            execution_id="exec-1",
            node_id="n1",
            node_name="Step 1",
        ))
        
        bus.publish(CheckpointCreatedEvent(
            execution_id="exec-1",
            checkpoint_id=1,
            node_id="n1",
            trigger_type="node_entry",
        ))
        
        bus.publish(NodeCompletedEvent(
            execution_id="exec-1",
            node_id="n1",
            node_name="Step 1",
            duration_ms=100,
            tool_invocations=2,
        ))
        
        bus.publish(ExecutionCompletedEvent(
            execution_id="exec-1",
            workflow_id="wf-1",
            duration_ms=500,
            nodes_executed=1,
            checkpoints_created=1,
        ))
        
        # Verify
        summary = audit.get_summary()
        assert summary["nodes_executed"] == 1
        assert summary["checkpoints_created"] == 1
        assert summary["errors"] == 0
        
        # Verify serialization round-trip
        data = audit.to_dict()
        restored = WTBAuditTrail.from_dict(data)
        assert len(restored.entries) == len(audit.entries)
```

---

## 9. 总结

### 设计要点

| 方面 | 决策 | 理由 |
|------|------|------|
| **Event Bus** | 复用 AgentGit + WTB 包装 | 避免重复，利用已验证代码 |
| **Audit Trail** | 独立 WTB 版本 | 关注点不同，粒度不同 |
| **桥接** | 通过 ACL 适配器 | 保持边界清晰 |
| **线程安全** | Lock 包装 | 支持并行执行 |
| **持久化** | 可选，Audit 可存 WTB DB | 按需启用 |

### 与 AgentGit 的关系

```
┌───────────────────────────────────────────────────────────────────┐
│                         关系总结                                   │
├───────────────────────────────────────────────────────────────────┤
│                                                                    │
│   AgentGit EventBus              WTB EventBus                     │
│   ═══════════════               ═════════════                     │
│   • 原生事件                     • 包装 + 扩展                     │
│   • 工具级粒度                   • 节点 + 执行级粒度               │
│   • 无线程安全                   • 线程安全                        │
│                                                                    │
│                    ─────── 桥接 ───────►                          │
│   AG Events ──► AG EventBus ──► Adapter ──► WTB EventBus         │
│                                                                    │
│                                                                    │
│   AgentGit AuditTrail            WTB AuditTrail                   │
│   ═══════════════════           ═══════════════                   │
│   • 工具 + LLM 调用               • 节点 + 执行生命周期            │
│   • 存入 checkpoint metadata     • 可存 WTB DB                    │
│   • LangChain callback 集成      • 事件监听器自动记录             │
│                                                                    │
│              ─────── 可选导入 ───────►                            │
│   AG Audit ──► to_dict() ──► WTB Audit.import_agentgit_audit()   │
│                                                                    │
└───────────────────────────────────────────────────────────────────┘
```

### 文件结构

```
wtb/
├── domain/
│   ├── events/
│   │   ├── __init__.py          # 现有
│   │   ├── execution_events.py  # 现有
│   │   ├── node_events.py       # 现有
│   │   └── checkpoint_events.py # 现有
│   └── audit/
│       ├── __init__.py          # 新增
│       └── audit_trail.py       # 新增: WTBAuditTrail
│
├── infrastructure/
│   └── events/
│       ├── __init__.py          # 新增
│       ├── event_bus.py         # 新增: WTBEventBus
│       └── audit_listener.py    # 新增: AuditEventListener
│
└── application/
    └── services/
        └── execution_controller.py  # 修改: 集成 EventBus
```

