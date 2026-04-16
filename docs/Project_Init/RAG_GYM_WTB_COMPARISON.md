# RAG-Gym WTB Comparison Experiment

**Last Updated:** 2026-02-02  
**Version:** 1.0.0  
**Status:** Implemented  
**Location:** `examples/bio_rag_benchmarks/rag_gym_wtb/`

---

## Overview

This document describes the three-way comparison experiment that demonstrates WTB's transaction consistency capabilities compared to vanilla RAG-Gym and LangGraph implementations.

---

## Experiment Design

### Three Implementations

| # | Implementation | Location | Failure Handling |
|---|----------------|----------|------------------|
| 1 | **Original** | `impl_original/` | None |
| 2 | **LangGraph** | `impl_langgraph/` | Basic checkpointing |
| 3 | **WTB** | `impl_wtb/` | Full ACID + scenarios A-F |

### Key Constraint

**DO NOT REFACTOR ORIGINAL CODE** - This is a comparison experiment. Original RAG-Gym code is wrapped at the node level without modification.

---

## Architecture Decisions

### Decision 1: Node-Level Script Abstraction

**Problem:** How to integrate RAG-Gym without modifying original code?

**Decision:** Create thin wrappers in `original_scripts/` that call RAG-Gym directly.

**Rationale:**
- Preserves original behavior for valid comparison
- Allows each implementation to use the same underlying logic
- Scripts can be called as functions or standalone processes

**Implementation:**
```python
# original_scripts/retrieve_node.py
def execute_retrieve(question: str, retriever: str, corpus: str, k: int) -> RetrieveResult:
    # Import and call original RAG-Gym directly
    from rag_gym.envs.IR import RetrievalSystem
    rs = RetrievalSystem(retriever=retriever, corpus=corpus)
    return rs.retrieve(question, k=k)
```

### Decision 2: State Schema Matching

**Problem:** How to ensure output parity across implementations?

**Decision:** Use identical state schema (`RAGState`) across all implementations.

**Rationale:**
- Same state structure ensures comparable outputs
- LangGraph and WTB can use the same nodes
- Parity checking is straightforward

**Implementation:**
```python
# impl_langgraph/state.py
class RAGState(TypedDict):
    question: str
    history: list[dict]  # Matches RAG-Gym History
    answer: str | None
    # ...
```

### Decision 3: WTB as Enhancement Layer

**Problem:** How should WTB integrate without changing behavior?

**Decision:** WTB wraps LangGraph nodes with infrastructure components.

**Rationale:**
- Adds capabilities (idempotency, transactions) without changing logic
- Same output, different guarantees
- Clear separation of concerns

**Implementation:**
```python
# impl_wtb/workflow.py
# Wrap base nodes with WTB enhancements
retrieve = create_idempotent_retrieve_node(...)  # Adds idempotency
agent = wrap_node_with_transaction(agent, ...)   # Adds transaction boundary
```

---

## Failure Scenario Mapping

### Scenario A: Non-Idempotent Writes

| Aspect | Original | LangGraph | WTB |
|--------|----------|-----------|-----|
| **Issue** | Each retry creates new history entry | Checkpoint replay may duplicate | Idempotency keys prevent duplicates |
| **Solution** | None | Partial (state-level) | Full (operation-level) |
| **Code** | N/A | Checkpointer | `IdempotentRetrieveNode` |

### Scenario B: Partial Commit

| Aspect | Original | LangGraph | WTB |
|--------|----------|-----------|-----|
| **Issue** | Failure leaves orphan data | State reset loses progress | Transaction rollback cleans up |
| **Solution** | None | Partial (checkpoint resume) | Full (Unit of Work) |
| **Code** | N/A | StateGraph | `TransactionalNodeWrapper` |

### Scenario C: Async Side Effects

| Aspect | Original | LangGraph | WTB |
|--------|----------|-----------|-----|
| **Issue** | Race conditions in parallel retrieval | Sequential execution only | Sequence-based ordering |
| **Solution** | None | Avoid parallelism | Full (event ordering) |

### Scenario D: Stale Reads

| Aspect | Original | LangGraph | WTB |
|--------|----------|-----------|-----|
| **Issue** | Global cache returns stale data | Per-thread state helps | Snapshot isolation |
| **Solution** | None | Partial | Full |

### Scenario E: Environment Isolation

| Aspect | Original | LangGraph | WTB |
|--------|----------|-----------|-----|
| **Issue** | All retrievers share environment | Same as original | Per-node venv via `EnvSpec` |
| **Solution** | None | None | Full |

### Scenario F: Version Drift

| Aspect | Original | LangGraph | WTB |
|--------|----------|-----------|-----|
| **Issue** | Upgrading tools breaks workflow | Same as original | Variant-based specs |
| **Solution** | None | None | Full |

---

## Project Structure

```
examples/bio_rag_benchmarks/rag_gym_wtb/
├── PLAN.md                    # Detailed implementation plan
├── README.md                  # Usage documentation
│
├── original_scripts/          # Node-level RAG-Gym wrappers
│   ├── retrieve_node.py       # RetrievalSystem wrapper
│   ├── agent_node.py          # Agent wrapper
│   ├── env_step_node.py       # Environment wrapper
│   └── answer_extract_node.py # Answer extraction wrapper
│
├── impl_original/             # Implementation 1: Direct RAG-Gym
│   └── runner.py
│
├── impl_langgraph/            # Implementation 2: LangGraph
│   ├── state.py               # RAGState schema
│   ├── nodes/                 # LangGraph nodes
│   ├── workflow.py            # StateGraph
│   └── runner.py
│
├── impl_wtb/                  # Implementation 3: WTB
│   ├── nodes/
│   │   ├── idempotent_retrieve.py
│   │   └── transactional_wrapper.py
│   ├── workflow.py
│   └── runner.py
│
├── scenarios/                 # Failure demonstrations
│   ├── base.py
│   ├── scenario_a_idempotency.py
│   └── scenario_b_partial.py
│
├── evaluation/                # Parity checking
│   ├── parity_checker.py
│   └── metrics.py
│
├── tests/                     # Unit and integration tests
└── scripts/                   # Entry points
```

---

## SOLID Compliance

| Principle | Implementation |
|-----------|----------------|
| **SRP** | Each node has single responsibility (retrieve, route, answer) |
| **OCP** | WTB extends LangGraph without modifying nodes |
| **LSP** | All implementations satisfy `runner.run()` interface |
| **ISP** | Small, focused node protocols |
| **DIP** | Nodes depend on state schema, not concrete implementations |

---

## ACID Properties

| Property | WTB Implementation |
|----------|-------------------|
| **Atomicity** | `TransactionalNodeWrapper` ensures all-or-nothing |
| **Consistency** | State schema validation at node boundaries |
| **Isolation** | `ExecutionTracker` isolates execution state |
| **Durability** | LangGraph checkpointer + outbox events |

---

## Testing Strategy

### Unit Tests
- `test_original_scripts.py`: Node script wrappers
- `test_langgraph_nodes.py`: LangGraph node behavior
- `test_wtb_nodes.py`: WTB enhancement behavior

### Integration Tests
- `test_parity.py`: Output parity across implementations
- `test_scenarios.py`: Failure scenario demonstrations

---

## Usage

```python
# Run comparison
from rag_gym_wtb.scripts.run_all import run_all

questions = [{"question": "What is diabetes?", "ground_truth": "B"}]
results = run_all(questions)

# Run scenarios
from rag_gym_wtb.scripts.run_scenarios import run_all_scenarios
reports = run_all_scenarios()
```

---

## Success Criteria

1. **Output Parity**: All implementations produce identical answers (normalized)
2. **Scenario Coverage**: All 6 scenarios demonstrate issues and WTB resolution
3. **Test Coverage**: >80% unit test coverage
4. **Performance**: WTB within 10% of LangGraph latency

---

## References

- RAG-Gym: `examples/bio_rag_benchmarks/RAG-Gym/`
- Transaction Consistency: `examples/transaction_consistency/`
- WTB Architecture: `docs/Project_Init/WORKFLOW_TEST_BENCH_ARCHITECTURE.md`
