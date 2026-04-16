# WTB Quick Start

Verify your WTB installation and run a minimal end-to-end smoke test.

## Install

```bash
# From PyPI (core only)
uv pip install wtb

# From PyPI (all optional extras)
uv pip install "wtb[all]"

# From source (editable)
cd /path/to/WTB-AgenticWorkflowTestBench
uv pip install -e ".[all]"
```

## Run the Installation Checker

```bash
python -m examples.quick_start.install_checker
```

### Expected Output

```
============================================================
  WTB Installation Checker
============================================================

  [PASS]   import wtb  -- version 0.2.0
  [PASS]   sdk imports  -- 12 symbols imported
  [PASS]   create bench  -- mode=testing
  [PASS]   run workflow  -- status=completed
  [PASS]   checkpoints  -- time_travel=True, count=4
  [PASS]   batch test  -- results=1

------------------------------------------------------------
  Total: 6  |  Passed: 6  |  Failed: 0
------------------------------------------------------------

  All checks PASSED. WTB is correctly installed.
```

Exit code `0` means all checks passed; `1` means at least one failed.

## What It Checks

| Check | What it validates |
|---|---|
| `import wtb` | Package is importable, prints version |
| `sdk imports` | All core SDK symbols (`WTBTestBench`, `WorkflowProject`, configs) resolve |
| `create bench` | `WTBTestBench.create(mode="testing")` succeeds (in-memory, no I/O) |
| `run workflow` | Registers a trivial LangGraph graph and runs it through the SDK |
| `checkpoints` | Inspects checkpoint history from the completed execution |
| `batch test` | Runs a single-variant batch test via the sequential fallback path |

All checks use **in-memory backends only** -- no database, no Ray, no API keys required.

## Next Steps

- See `examples/wtb_presentation/` for a full-featured demo with Ray, file tracking, and environment isolation.
- See the root [README.md](../../README.md) for architecture overview and core operations.
