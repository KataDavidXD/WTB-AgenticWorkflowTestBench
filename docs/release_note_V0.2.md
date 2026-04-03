# WTB v0.2.0 Release Notes

## Highlights

**Batch Rollback/Fork Coordination** - After running batch tests with `RayBatchTestRunner`,
you can now rollback or fork specific variant executions without losing test context.

## New Features

### 1. BatchExecutionCoordinator

Orchestrates rollback/fork operations with ACID compliance:

- Two-phase commit: state in UoW transaction, file restore post-commit
- Outbox pattern for reliable audit events

### 2. SDK Convenience Methods

```python
Run batch test
batch = wtb.run_batch_test(project="my_workflow", ...)
Rollback to last checkpoint (simple)
wtb.rollback_batch_result(batch.results[0])
Fork with modified state
fork = wtb.fork_batch_result(
batch.results[0],
new_state={"temperature": 0.7}
)
Advanced: direct coordinator access
coordinator = wtb.get_batch_coordinator()
coordinator.batch_operate([...])
```

### 3. BatchTestResult Rollback Fields

- `file_commit_id` - FileTracker commit for file restore
- `checkpoint_count` - Number of checkpoints created
- `last_checkpoint_id` - Most recent checkpoint (default for rollback)

## Architecture

- **SOLID**: SDK delegates to Application factories (DIP compliance)
- **ACID**: Each operation in isolated UoW transaction
- **Outbox**: ROLLBACK_PERFORMED, EXECUTION_FORKED events

## Breaking Changes

None - all changes are additive.

## Test Coverage

58 new tests covering coordinator, SDK integration, and Ray batch scenarios.