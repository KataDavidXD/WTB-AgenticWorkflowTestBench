# WTB Architecture Diagrams

This document provides visual diagrams showing how the Workflow Test Bench (WTB) works for each action.

## Table of Contents

1. [Basic Execution Flow](#1-basic-execution-flow)
2. [Checkpointing Flow](#2-checkpointing-flow)
3. [Rollback Flow](#3-rollback-flow)
4. [Fork/Branch Flow](#4-forkbranch-flow)
5. [File Tracking Flow](#5-file-tracking-flow)
6. [Ray Batch Execution Flow](#6-ray-batch-execution-flow)
7. [Component Overview](#7-component-overview)

---

## 1. Basic Execution Flow

How a workflow is executed from start to finish.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          BASIC EXECUTION FLOW                               │
└─────────────────────────────────────────────────────────────────────────────┘

    User/Test Code                 WTBTestBench                ExecutionController
         │                              │                              │
         │  bench.run(project, state)   │                              │
         │─────────────────────────────>│                              │
         │                              │                              │
         │                              │  start_execution(workflow,   │
         │                              │    initial_state)            │
         │                              │─────────────────────────────>│
         │                              │                              │
         │                              │                              │
         │                              │      ┌───────────────────────┤
         │                              │      │ 1. Create Execution   │
         │                              │      │    Entity             │
         │                              │      │ 2. Initialize         │
         │                              │      │    StateAdapter       │
         │                              │      │ 3. Save to UoW        │
         │                              │      └───────────────────────┤
         │                              │                              │
         │                              │                              │
         │                              │  ┌───────────────────────────┤
         │                              │  │     LANGGRAPH LOOP        │
         │                              │  │  ┌─────────────────────┐  │
         │                              │  │  │ For each node:      │  │
         │                              │  │  │  - Execute node     │  │
         │                              │  │  │  - Auto checkpoint  │  │
         │                              │  │  │  - Update state     │  │
         │                              │  │  └─────────────────────┘  │
         │                              │  └───────────────────────────┤
         │                              │                              │
         │                              │  completed_execution         │
         │                              │<─────────────────────────────│
         │                              │                              │
         │  Execution(id, status,       │                              │
         │    state)                    │                              │
         │<─────────────────────────────│                              │
         │                              │                              │


┌─────────────────────────────────────────────────────────────────────────────┐
│                        NODE EXECUTION DETAIL                                │
└─────────────────────────────────────────────────────────────────────────────┘

           StateAdapter                 LangGraph                    Checkpointer
                │                           │                             │
                │   graph.invoke(state)     │                             │
                │──────────────────────────>│                             │
                │                           │                             │
                │                           │   ┌─────────────────────────┤
                │                           │   │ Execute Node Function   │
                │                           │   │                         │
                │                           │   │ node_func(state) ->     │
                │                           │   │   new_state             │
                │                           │   └─────────────────────────┤
                │                           │                             │
                │                           │   put(checkpoint_data)      │
                │                           │────────────────────────────>│
                │                           │                             │
                │                           │   ┌─────────────────────────┤
                │                           │   │ Auto-checkpoint:        │
                │                           │   │ - thread_id             │
                │                           │   │ - checkpoint_id (UUID)  │
                │                           │   │ - step number           │
                │                           │   │ - state values          │
                │                           │   │ - next nodes            │
                │                           │   └─────────────────────────┤
                │                           │                             │
                │   StateSnapshot           │                             │
                │<──────────────────────────│                             │
                │                           │                             │
```

---

## 2. Checkpointing Flow

How checkpoints are created and stored at each node boundary.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         CHECKPOINTING FLOW                                  │
└─────────────────────────────────────────────────────────────────────────────┘

                              LangGraph Super-Step
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                                                                         │
    │   Step -1          Step 0           Step 1           Step 2             │
    │   ┌─────┐         ┌─────┐          ┌─────┐          ┌─────┐            │
    │   │ CP  │───▶     │ CP  │───▶      │ CP  │───▶      │ CP  │───▶ ...    │
    │   │ -1  │         │  0  │          │  1  │          │  2  │            │
    │   └─────┘         └─────┘          └─────┘          └─────┘            │
    │      │               │                │                │               │
    │      ▼               ▼                ▼                ▼               │
    │  __start__      rag_load_docs    rag_chunk_split   rag_embed_docs     │
    │                                                                         │
    └─────────────────────────────────────────────────────────────────────────┘


    Checkpoint Structure (SQLite: wtb_checkpoints.db)
    ┌─────────────────────────────────────────────────────────────────────────┐
    │  checkpoint_id:  1f0f34e8-164f-6fe7-800a-...  (UUID)                    │
    │  thread_id:      wtb-{execution_id}                                     │
    │  step:           10                                                     │
    │  metadata: {                                                            │
    │    "source": "loop",                                                    │
    │    "writes": {...},                                                     │
    │    "step": 10                                                           │
    │  }                                                                      │
    │  values: {                                                              │
    │    "query": "What is revenue?",                                         │
    │    "answer": "Revenue is...",                                           │
    │    "_output_files": {"result.txt": "..."}                               │
    │  }                                                                      │
    └─────────────────────────────────────────────────────────────────────────┘


    Checkpoint Timeline View
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                                                                         │
    │  Time ──────────────────────────────────────────────────────────────▶   │
    │                                                                         │
    │       │ CP-1 │ CP0 │ CP1 │ CP2 │ CP3 │ CP4 │ ... │ CP10 │              │
    │       └──┬───┴──┬──┴──┬──┴──┬──┴──┬──┴──┬──┴─────┴───┬──┘              │
    │          │      │     │     │     │     │            │                  │
    │          ▼      ▼     ▼     ▼     ▼     ▼            ▼                  │
    │        init   load  chunk embed query retrieve    generate             │
    │                                                   (terminal)           │
    │                                                                         │
    │   ◀─────────────── Each checkpoint is rollback target ──────────────▶   │
    │                                                                         │
    └─────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Rollback Flow

How rollback restores both workflow state AND file system state.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            ROLLBACK FLOW                                    │
└─────────────────────────────────────────────────────────────────────────────┘

   User/Test                WTBTestBench          ExecutionController        FileTracking
       │                         │                       │                       │
       │ bench.rollback(         │                       │                       │
       │   exec_id, cp_id)       │                       │                       │
       │────────────────────────>│                       │                       │
       │                         │                       │                       │
       │                         │ rollback(exec_id,     │                       │
       │                         │   checkpoint_id)      │                       │
       │                         │──────────────────────>│                       │
       │                         │                       │                       │
       │                         │                       │  ┌───────────────────┤
       │                         │                       │  │ 1. STATE ROLLBACK │
       │                         │                       │  │                   │
       │                         │                       │  │ state_adapter.    │
       │                         │                       │  │   rollback(cp_id) │
       │                         │                       │  │                   │
       │                         │                       │  │ - Load checkpoint │
       │                         │                       │  │   from SQLite     │
       │                         │                       │  │ - Restore state   │
       │                         │                       │  │   values          │
       │                         │                       │  └───────────────────┤
       │                         │                       │                       │
       │                         │                       │ restore_files_from_  │
       │                         │                       │   state(state)       │
       │                         │                       │──────────────────────>│
       │                         │                       │                       │
       │                         │                       │  ┌───────────────────┤
       │                         │                       │  │ 2. FILE ROLLBACK  │
       │                         │                       │  │                   │
       │                         │                       │  │ For each file in  │
       │                         │                       │  │ _output_files:    │
       │                         │                       │  │                   │
       │                         │                       │  │ - Read blob from  │
       │                         │                       │  │   content-addr    │
       │                         │                       │  │   storage         │
       │                         │                       │  │ - Write to disk   │
       │                         │                       │  └───────────────────┤
       │                         │                       │                       │
       │                         │                       │ {"success": true,    │
       │                         │                       │  "files_restored": N}│
       │                         │                       │<──────────────────────│
       │                         │                       │                       │
       │                         │ RollbackResult        │                       │
       │                         │<──────────────────────│                       │
       │                         │                       │                       │
       │ RollbackResult          │                       │                       │
       │<────────────────────────│                       │                       │
       │                         │                       │                       │


    Rollback State Transition
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                                                                         │
    │  BEFORE ROLLBACK:                                                       │
    │  ┌──────────────────────────────────────────────────────────────────┐   │
    │  │  Execution at Step 10 (COMPLETED)                                │   │
    │  │  Files: result.txt, answer.txt, report.pdf                       │   │
    │  │  State: {query: "...", answer: "final answer", ...}              │   │
    │  └──────────────────────────────────────────────────────────────────┘   │
    │                                                                         │
    │                              │                                          │
    │                              ▼  bench.rollback(exec_id, cp_5)           │
    │                                                                         │
    │  AFTER ROLLBACK:                                                        │
    │  ┌──────────────────────────────────────────────────────────────────┐   │
    │  │  Execution at Step 5 (PAUSED)                                    │   │
    │  │  Files: result.txt (restored to step 5 version)                  │   │
    │  │  State: {query: "...", answer: "partial answer", ...}            │   │
    │  │                                                                  │   │
    │  │  Ready to RESUME from checkpoint 5                               │   │
    │  └──────────────────────────────────────────────────────────────────┘   │
    │                                                                         │
    └─────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Fork/Branch Flow

How branching creates independent execution copies for A/B testing.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          FORK/BRANCH FLOW                                   │
└─────────────────────────────────────────────────────────────────────────────┘

    Original Execution Timeline
    ═══════════════════════════════════════════════════════════════════════════

         CP0 ─────▶ CP1 ─────▶ CP2 ─────▶ CP3 ─────▶ CP4 ─────▶ CP5 (current)
                               │
                               │  Fork Point (checkpoint 2)
                               │
                               ▼
    ═══════════════════════════════════════════════════════════════════════════


    After bench.fork(execution_id, checkpoint_id="CP2")
    ═══════════════════════════════════════════════════════════════════════════

    Original (exec-A):
         CP0 ─────▶ CP1 ─────▶ CP2 ─────▶ CP3 ─────▶ CP4 ─────▶ CP5
                               │
                               │
    Fork (exec-B):             └────────▶ CP2' ─────▶ CP3' ─────▶ ...
                                          │
                               (New thread_id: wtb-branch-{uuid})
                               (Independent checkpoint history)

    ═══════════════════════════════════════════════════════════════════════════


    Fork Implementation Detail
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                                                                         │
    │   User Code                StateAdapter              LangGraph          │
    │       │                         │                        │              │
    │       │ bench.fork(exec_id,     │                        │              │
    │       │   checkpoint_id)        │                        │              │
    │       │────────────────────────>│                        │              │
    │       │                         │                        │              │
    │       │                         │ create_branch(cp_id)   │              │
    │       │                         │───────────────────────>│              │
    │       │                         │                        │              │
    │       │                         │   ┌────────────────────┤              │
    │       │                         │   │ 1. Get state at    │              │
    │       │                         │   │    checkpoint      │              │
    │       │                         │   │                    │              │
    │       │                         │   │ 2. Create new      │              │
    │       │                         │   │    thread_id       │              │
    │       │                         │   │                    │              │
    │       │                         │   │ 3. Copy state to   │              │
    │       │                         │   │    new thread      │              │
    │       │                         │   │                    │              │
    │       │                         │   │ 4. Return new      │              │
    │       │                         │   │    session_id      │              │
    │       │                         │   └────────────────────┤              │
    │       │                         │                        │              │
    │       │ ForkResult              │                        │              │
    │       │  (fork_execution_id)    │                        │              │
    │       │<────────────────────────│                        │              │
    │                                                                         │
    └─────────────────────────────────────────────────────────────────────────┘


    A/B Testing Use Case
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                                                                         │
    │   Original Execution                                                    │
    │   ┌────────────────────┐                                                │
    │   │ query: "revenue?"  │                                                │
    │   │ retriever: BM25    │ ──────▶ CP3 (fork point)                       │
    │   │ model: gpt-4       │                                                │
    │   └────────────────────┘                                                │
    │                          │                                              │
    │            ┌─────────────┴─────────────┐                                │
    │            ▼                           ▼                                │
    │   ┌────────────────────┐     ┌────────────────────┐                     │
    │   │ Variant A:         │     │ Variant B:         │                     │
    │   │ retriever: Dense   │     │ retriever: Hybrid  │                     │
    │   │ model: gpt-4o-mini │     │ model: gpt-4o      │                     │
    │   └────────────────────┘     └────────────────────┘                     │
    │            │                           │                                │
    │            ▼                           ▼                                │
    │   ┌────────────────────┐     ┌────────────────────┐                     │
    │   │ Answer A: "..."    │     │ Answer B: "..."    │                     │
    │   │ Latency: 50ms      │     │ Latency: 120ms     │                     │
    │   │ Quality: 0.85      │     │ Quality: 0.92      │                     │
    │   └────────────────────┘     └────────────────────┘                     │
    │                                                                         │
    │            └─────────── Compare Results ───────────┘                    │
    │                                                                         │
    └─────────────────────────────────────────────────────────────────────────┘
```

---

## 5. File Tracking Flow

How file tracking enables atomic file system rollback using content-addressable storage.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         FILE TRACKING FLOW                                  │
└─────────────────────────────────────────────────────────────────────────────┘

    File Tracking Architecture
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                                                                         │
    │   Workflow State                    .filetrack/                         │
    │   ┌─────────────────┐              ┌─────────────────────────────────┐  │
    │   │ _output_files:  │              │  filetrack.db                   │  │
    │   │  {              │              │  ┌───────────────────────────┐  │  │
    │   │   "result.txt": │──────────────│──│ commits                   │  │  │
    │   │     "content"   │              │  │  - commit_id (UUID)       │  │  │
    │   │  }              │              │  │  - execution_id           │  │  │
    │   └─────────────────┘              │  │  - created_at             │  │  │
    │                                    │  └───────────────────────────┘  │  │
    │                                    │  ┌───────────────────────────┐  │  │
    │                                    │  │ files                     │  │  │
    │                                    │  │  - file_path              │  │  │
    │                                    │  │  - blob_hash (SHA256)     │  │  │
    │                                    │  │  - size                   │  │  │
    │                                    │  └───────────────────────────┘  │  │
    │                                    │                                 │  │
    │                                    │  blobs/                         │  │
    │                                    │  ┌───────────────────────────┐  │  │
    │                                    │  │ de/645e21... (content)    │  │  │
    │                                    │  │ 9d/93bb24... (content)    │  │  │
    │                                    │  │ a3/7c2f89... (content)    │  │  │
    │                                    │  └───────────────────────────┘  │  │
    │                                    └─────────────────────────────────┘  │
    │                                                                         │
    └─────────────────────────────────────────────────────────────────────────┘


    Content-Addressable Storage (CAS) Flow
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                                                                         │
    │   WRITE (After Node Execution)                                          │
    │   ═══════════════════════════════════════════════════════════════════   │
    │                                                                         │
    │   state["_output_files"] = {"result.txt": "Query: ...\nAnswer: ..."}    │
    │                                       │                                 │
    │                                       ▼                                 │
    │                              ┌────────────────┐                         │
    │                              │ SHA256(content)│                         │
    │                              │ = de645e21...  │                         │
    │                              └────────────────┘                         │
    │                                       │                                 │
    │                          ┌────────────┴────────────┐                    │
    │                          ▼                         ▼                    │
    │                 ┌────────────────┐       ┌────────────────┐             │
    │                 │ blobs/de/645e21│       │ filetrack.db   │             │
    │                 │ (write content)│       │ (record hash)  │             │
    │                 └────────────────┘       └────────────────┘             │
    │                                                                         │
    │                                                                         │
    │   READ (During Rollback)                                                │
    │   ═══════════════════════════════════════════════════════════════════   │
    │                                                                         │
    │   checkpoint.state_values["_output_files"] = {"result.txt": "..."}      │
    │                                       │                                 │
    │                                       ▼                                 │
    │                              ┌────────────────┐                         │
    │                              │ SHA256(content)│                         │
    │                              │ = de645e21...  │                         │
    │                              └────────────────┘                         │
    │                                       │                                 │
    │                                       ▼                                 │
    │                              ┌────────────────┐                         │
    │                              │ blobs/de/645e21│                         │
    │                              │ (read content) │                         │
    │                              └────────────────┘                         │
    │                                       │                                 │
    │                                       ▼                                 │
    │                              ┌────────────────┐                         │
    │                              │ outputs/       │                         │
    │                              │  result.txt    │                         │
    │                              │ (write to disk)│                         │
    │                              └────────────────┘                         │
    │                                                                         │
    └─────────────────────────────────────────────────────────────────────────┘


    Deduplication Benefit
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                                                                         │
    │   Execution 1: Creates "result.txt" with hash=abc123                    │
    │   Execution 2: Creates "result.txt" with hash=abc123 (same content)     │
    │   Execution 3: Creates "result.txt" with hash=def456 (different)        │
    │                                                                         │
    │   Blob Storage:                                                         │
    │   ┌───────────────────────────┐                                         │
    │   │ blobs/ab/c123... (1 copy) │ ◀── Exec 1 & 2 share this              │
    │   │ blobs/de/f456... (1 copy) │ ◀── Exec 3 unique                       │
    │   └───────────────────────────┘                                         │
    │                                                                         │
    │   Storage: 2 blobs instead of 3 = 33% savings                           │
    │                                                                         │
    └─────────────────────────────────────────────────────────────────────────┘
```

---

## 6. Ray Batch Execution Flow

How Ray enables parallel execution of multiple workflow variants.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      RAY BATCH EXECUTION FLOW                               │
└─────────────────────────────────────────────────────────────────────────────┘

    Batch Test Submission
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                                                                         │
    │   User Code           RayBatchTestRunner         Ray Cluster            │
    │       │                      │                        │                 │
    │       │ run_batch_test(      │                        │                 │
    │       │   batch_test)        │                        │                 │
    │       │─────────────────────>│                        │                 │
    │       │                      │                        │                 │
    │       │                      │ ray.init(address)      │                 │
    │       │                      │───────────────────────>│                 │
    │       │                      │                        │                 │
    │       │                      │   ┌────────────────────┤                 │
    │       │                      │   │ Create ActorPool   │                 │
    │       │                      │   │ (VariantExecution  │                 │
    │       │                      │   │  Actors)           │                 │
    │       │                      │   └────────────────────┤                 │
    │       │                      │                        │                 │
    │       │                      │ Submit variants        │                 │
    │       │                      │ (with backpressure)    │                 │
    │       │                      │───────────────────────>│                 │
    │       │                      │                        │                 │
    │       │                      │                        │                 │
    │       │                      │ Aggregate results      │                 │
    │       │                      │<───────────────────────│                 │
    │       │                      │                        │                 │
    │       │ BatchTestResult      │                        │                 │
    │       │<─────────────────────│                        │                 │
    │                                                                         │
    └─────────────────────────────────────────────────────────────────────────┘


    Parallel Execution Timeline
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                                                                         │
    │   SEQUENTIAL (without Ray):                                             │
    │                                                                         │
    │   Time ──────────────────────────────────────────────────────────────▶  │
    │   ┌───────────┐┌───────────┐┌───────────┐┌───────────┐┌───────────┐    │
    │   │  Query 1  ││  Query 2  ││  Query 3  ││  Query 4  ││  Query 5  │    │
    │   │   100ms   ││   100ms   ││   100ms   ││   100ms   ││   100ms   │    │
    │   └───────────┘└───────────┘└───────────┘└───────────┘└───────────┘    │
    │                                                                         │
    │   Total: 500ms                                                          │
    │   ═══════════════════════════════════════════════════════════════════   │
    │                                                                         │
    │   PARALLEL (with Ray, 5 actors):                                        │
    │                                                                         │
    │   Time ────────────────────────▶                                        │
    │   Actor 1: ┌───────────┐                                                │
    │            │  Query 1  │                                                │
    │            │   100ms   │                                                │
    │   Actor 2: ┌───────────┐                                                │
    │            │  Query 2  │                                                │
    │            │   100ms   │                                                │
    │   Actor 3: ┌───────────┐                                                │
    │            │  Query 3  │                                                │
    │            │   100ms   │                                                │
    │   Actor 4: ┌───────────┐                                                │
    │            │  Query 4  │                                                │
    │            │   100ms   │                                                │
    │   Actor 5: ┌───────────┐                                                │
    │            │  Query 5  │                                                │
    │            │   100ms   │                                                │
    │            └───────────┘                                                │
    │                                                                         │
    │   Total: ~100ms (5x speedup!)                                           │
    │                                                                         │
    └─────────────────────────────────────────────────────────────────────────┘


    Actor Pool Architecture
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                                                                         │
    │                     RayBatchTestRunner                                  │
    │                           │                                             │
    │            ┌──────────────┼──────────────┐                              │
    │            │              │              │                              │
    │            ▼              ▼              ▼                              │
    │   ┌─────────────┐ ┌─────────────┐ ┌─────────────┐                       │
    │   │  Actor 1    │ │  Actor 2    │ │  Actor 3    │  ...                  │
    │   │ ┌─────────┐ │ │ ┌─────────┐ │ │ ┌─────────┐ │                       │
    │   │ │State    │ │ │ │State    │ │ │ │State    │ │                       │
    │   │ │Adapter  │ │ │ │Adapter  │ │ │ │Adapter  │ │                       │
    │   │ └─────────┘ │ │ └─────────┘ │ │ └─────────┘ │                       │
    │   │ ┌─────────┐ │ │ ┌─────────┐ │ │ ┌─────────┐ │                       │
    │   │ │UoW      │ │ │ │UoW      │ │ │ │UoW      │ │                       │
    │   │ └─────────┘ │ │ └─────────┘ │ │ └─────────┘ │                       │
    │   │ ┌─────────┐ │ │ ┌─────────┐ │ │ ┌─────────┐ │                       │
    │   │ │File     │ │ │ │File     │ │ │ │File     │ │                       │
    │   │ │Tracking │ │ │ │Tracking │ │ │ │Tracking │ │                       │
    │   │ └─────────┘ │ │ └─────────┘ │ │ └─────────┘ │                       │
    │   └─────────────┘ └─────────────┘ └─────────────┘                       │
    │                                                                         │
    │   Each actor has isolated:                                              │
    │   - Database connections (connection pooling per actor)                 │
    │   - State adapter instances                                             │
    │   - File tracking service                                               │
    │   - Workspace isolation (optional)                                      │
    │                                                                         │
    └─────────────────────────────────────────────────────────────────────────┘


    Resource Allocation per Node
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                                                                         │
    │   node_resources = {                                                    │
    │       "rag_embed_docs": NodeResourceConfig(num_cpus=2, memory="2GB"),   │
    │       "rag_retrieve":   NodeResourceConfig(num_cpus=2, memory="1GB"),   │
    │       "rag_generate":   NodeResourceConfig(num_cpus=1, memory="1GB"),   │
    │   }                                                                     │
    │                                                                         │
    │   Ray Scheduler:                                                        │
    │   ┌─────────────────────────────────────────────────────────────────┐   │
    │   │                                                                 │   │
    │   │   Available: 32 CPUs, 64GB RAM                                  │   │
    │   │                                                                 │   │
    │   │   ┌───────────┐ ┌───────────┐ ┌───────────┐                     │   │
    │   │   │ embed_1   │ │ embed_2   │ │ retrieve_1│ ...                 │   │
    │   │   │ 2CPU/2GB  │ │ 2CPU/2GB  │ │ 2CPU/1GB  │                     │   │
    │   │   └───────────┘ └───────────┘ └───────────┘                     │   │
    │   │                                                                 │   │
    │   │   Ray automatically schedules tasks based on:                   │   │
    │   │   - Resource requirements                                       │   │
    │   │   - Available cluster capacity                                  │   │
    │   │   - Locality preferences                                        │   │
    │   │                                                                 │   │
    │   └─────────────────────────────────────────────────────────────────┘   │
    │                                                                         │
    └─────────────────────────────────────────────────────────────────────────┘
```

---

## 7. Component Overview

High-level WTB architecture showing all components and their relationships.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        WTB COMPONENT ARCHITECTURE                           │
└─────────────────────────────────────────────────────────────────────────────┘

    ┌─────────────────────────────────────────────────────────────────────────┐
    │                            USER / TEST CODE                             │
    │                                                                         │
    │   bench = WTBTestBench.create(mode="development", data_dir="./data")    │
    │   result = bench.run(project="my_workflow", initial_state={...})        │
    │   bench.rollback(execution_id, checkpoint_id)                           │
    │   bench.fork(execution_id, checkpoint_id)                               │
    │                                                                         │
    └────────────────────────────────────┬────────────────────────────────────┘
                                         │
                                         ▼
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                            SDK LAYER                                    │
    │  ┌──────────────────────────────────────────────────────────────────┐   │
    │  │                        WTBTestBench                              │   │
    │  │  - run()                                                         │   │
    │  │  - rollback()                                                    │   │
    │  │  - fork()                                                        │   │
    │  │  - get_checkpoints()                                             │   │
    │  │  - get_state()                                                   │   │
    │  └──────────────────────────────────────────────────────────────────┘   │
    │                                                                         │
    │  ┌──────────────────────────────────────────────────────────────────┐   │
    │  │                      WorkflowProject                             │   │
    │  │  - FileTrackingConfig                                            │   │
    │  │  - EnvironmentConfig (venv per node)                             │   │
    │  │  - ExecutionConfig (Ray, resources)                              │   │
    │  │  - PauseStrategyConfig                                           │   │
    │  └──────────────────────────────────────────────────────────────────┘   │
    └────────────────────────────────────┬────────────────────────────────────┘
                                         │
                                         ▼
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                        APPLICATION LAYER                                │
    │                                                                         │
    │  ┌────────────────────────────────────────────────────────────────────┐ │
    │  │                    ExecutionController                             │ │
    │  │  - Orchestrates workflow execution                                 │ │
    │  │  - Manages checkpoints                                             │ │
    │  │  - Handles rollback/fork                                           │ │
    │  │  - Coordinates file tracking                                       │ │
    │  └────────────────────────────────────────────────────────────────────┘ │
    │                                                                         │
    │  ┌────────────────────────────────────────────────────────────────────┐ │
    │  │                   RayBatchTestRunner                               │ │
    │  │  - Parallel variant execution                                      │ │
    │  │  - ActorPool management                                            │ │
    │  │  - Result aggregation                                              │ │
    │  └────────────────────────────────────────────────────────────────────┘ │
    │                                                                         │
    └──────────────┬────────────────────────────────┬─────────────────────────┘
                   │                                │
                   ▼                                ▼
    ┌──────────────────────────────┐  ┌──────────────────────────────────────┐
    │     INFRASTRUCTURE LAYER     │  │          EXTERNAL SERVICES           │
    │                              │  │                                      │
    │  ┌────────────────────────┐  │  │  ┌────────────────────────────────┐  │
    │  │  LangGraphStateAdapter │  │  │  │         LangGraph              │  │
    │  │  - Session management  │  │  │  │  - StateGraph compilation      │  │
    │  │  - Checkpoint ops      │  │  │  │  - Node execution              │  │
    │  │  - ID mapping          │  │  │  │  - Super-step checkpoints      │  │
    │  └────────────────────────┘  │  │  └────────────────────────────────┘  │
    │                              │  │                                      │
    │  ┌────────────────────────┐  │  │  ┌────────────────────────────────┐  │
    │  │ SqliteFileTracking     │  │  │  │           Ray                  │  │
    │  │  - Content-addressable │  │  │  │  - Distributed execution       │  │
    │  │  - Blob storage        │  │  │  │  - Actor pool                  │  │
    │  │  - Commit tracking     │  │  │  │  - Resource scheduling         │  │
    │  └────────────────────────┘  │  │  └────────────────────────────────┘  │
    │                              │  │                                      │
    │  ┌────────────────────────┐  │  │  ┌────────────────────────────────┐  │
    │  │   SQLiteUnitOfWork     │  │  │  │       UV Venv Manager          │  │
    │  │  - Transaction mgmt    │  │  │  │  - Per-node environments       │  │
    │  │  - Repository access   │  │  │  │  - Dependency isolation        │  │
    │  └────────────────────────┘  │  │  └────────────────────────────────┘  │
    │                              │  │                                      │
    └──────────────────────────────┘  └──────────────────────────────────────┘
                   │                                │
                   ▼                                ▼
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                          PERSISTENCE LAYER                              │
    │                                                                         │
    │  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────┐   │
    │  │   wtb.db         │  │ wtb_checkpoints  │  │ .filetrack/          │   │
    │  │                  │  │     .db          │  │   filetrack.db       │   │
    │  │ - executions     │  │                  │  │   blobs/             │   │
    │  │ - workflows      │  │ - checkpoints    │  │     de/645e21...     │   │
    │  │ - batch_tests    │  │ - thread_id      │  │     9d/93bb24...     │   │
    │  │ - outbox         │  │ - step           │  │                      │   │
    │  │                  │  │ - values         │  │                      │   │
    │  └──────────────────┘  └──────────────────┘  └──────────────────────┘   │
    │                                                                         │
    └─────────────────────────────────────────────────────────────────────────┘
```

---

## Summary

| Action | Key Components | Data Flow |
|--------|---------------|-----------|
| **Execute** | WTBTestBench → ExecutionController → LangGraph | State flows through nodes, auto-checkpoints |
| **Checkpoint** | LangGraph → Checkpointer → SQLite | Thread-scoped, step-indexed snapshots |
| **Rollback** | StateAdapter → FileTracking | Restore state + files atomically |
| **Fork** | StateAdapter.create_branch | New thread_id with copied state |
| **File Track** | SqliteFileTrackingService | Content-addressed dedup storage |
| **Ray Batch** | RayBatchTestRunner → ActorPool | Parallel isolated executions |

---

*Generated: 2026-01-17*
*WTB Version: 0.1.0*
