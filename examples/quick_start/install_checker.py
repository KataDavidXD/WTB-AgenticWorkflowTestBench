"""
WTB Installation Checker & Smoke Test.

Validates that the ``wtb`` package is correctly installed and that
core SDK operations work end-to-end.  Tests are grouped into tiers:

  Tier 1 (always): import, bench creation, run, checkpoint, rollback, fork, batch
  Tier 2 (if ray): Ray-distributed batch execution, actor-local cache metadata,
                    cache-aware rollback/fork in Ray batch mode
  Tier 3 (if grpc): GrpcEnvironmentProvider (venv service connectivity)

Usage:
    python -m examples.quick_start.install_checker
    python -m examples.quick_start.install_checker --skip-ray
    python -m examples.quick_start.install_checker --grpc-url localhost:50051

Exit code 0 = all attempted checks passed, 1 = at least one failure.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.engine.Engine").setLevel(logging.WARNING)

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

results: List[Tuple[str, str, str]] = []


def record(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))
    tag = f"[{status}]"
    msg = f"  {tag:8s} {name}"
    if detail:
        msg += f"  -- {detail}"
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Graph factories (SDK-only, minimal LangGraph)
# ---------------------------------------------------------------------------

def _create_linear_graph():
    """3-node linear graph: A -> B -> C."""
    from typing import TypedDict
    from langgraph.graph import StateGraph, END

    class St(TypedDict):
        messages: list
        count: int
        result: str

    def node_a(state: Dict[str, Any]) -> dict:
        return {"messages": state.get("messages", []) + ["A"],
                "count": state.get("count", 0) + 1}

    def node_b(state: Dict[str, Any]) -> dict:
        return {"messages": state.get("messages", []) + ["B"],
                "count": state.get("count", 0) + 1}

    def node_c(state: Dict[str, Any]) -> dict:
        msgs = state.get("messages", []) + ["C"]
        return {"messages": msgs, "count": state.get("count", 0) + 1,
                "result": ",".join(msgs)}

    g = StateGraph(St)
    g.add_node("node_a", node_a)
    g.add_node("node_b", node_b)
    g.add_node("node_c", node_c)
    g.add_edge("__start__", "node_a")
    g.add_edge("node_a", "node_b")
    g.add_edge("node_b", "node_c")
    g.add_edge("node_c", END)
    return g


_INIT_STATE: Dict[str, Any] = {"messages": [], "count": 0, "result": ""}


# ═══════════════════════════════════════════════════════════════════════════════
# Tier 1 -- always runs (in-memory, zero external deps beyond langgraph)
# ═══════════════════════════════════════════════════════════════════════════════

def check_import() -> None:
    try:
        import wtb
        record("import wtb", PASS, f"version {wtb.__version__}")
    except Exception as exc:
        record("import wtb", FAIL, str(exc))


def check_sdk_imports() -> None:
    try:
        from wtb.sdk import (  # noqa: F401
            WTBTestBench, WorkflowProject,
            FileTrackingConfig, EnvironmentConfig, ExecutionConfig,
            EnvSpec, RayConfig, NodeResourceConfig,
            WorkspaceIsolationConfig, PauseStrategyConfig,
            RollbackResult, ForkResult,
            BatchRollbackResult, BatchForkResult,
        )
        record("sdk imports", PASS, "14 symbols")
    except Exception as exc:
        record("sdk imports", FAIL, str(exc))


def check_create_bench() -> None:
    try:
        from wtb.sdk import WTBTestBench
        bench = WTBTestBench.create(mode="testing")
        assert bench is not None
        record("create bench", PASS, "mode=testing")
    except Exception as exc:
        record("create bench", FAIL, str(exc))


def check_run_workflow() -> Optional[Any]:
    """Run a workflow end-to-end and return (bench, execution) for later checks."""
    try:
        from wtb.sdk import WTBTestBench, WorkflowProject

        bench = WTBTestBench.create(mode="testing")
        project = WorkflowProject(name="smoke", graph_factory=_create_linear_graph)
        bench.register_project(project)

        execution = bench.run(project="smoke", initial_state=dict(_INIT_STATE))
        assert execution.status.value == "completed", f"status={execution.status}"
        record("run workflow", PASS, f"status={execution.status.value}")
        return bench, execution
    except Exception as exc:
        record("run workflow", FAIL, str(exc))
        traceback.print_exc()
        return None


def check_checkpoints(ctx: Optional[Any]) -> Optional[list]:
    if ctx is None:
        record("checkpoints", SKIP, "workflow run failed")
        return None
    bench, execution = ctx
    try:
        cps = bench.get_checkpoints(execution.id)
        record("checkpoints", PASS,
               f"time_travel={bench.supports_time_travel()}, count={len(cps)}")
        return cps
    except Exception as exc:
        record("checkpoints", FAIL, str(exc))
        return None


def check_rollback(ctx: Optional[Any], cps: Optional[list]) -> None:
    if ctx is None or not cps:
        record("rollback", SKIP, "no context/checkpoints")
        return
    bench, execution = ctx
    try:
        cp_id = str(cps[0].id)
        result = bench.rollback(execution.id, checkpoint_id=cp_id)
        assert result.success, f"rollback error: {result.error}"
        record("rollback", PASS, f"to checkpoint step={cps[0].step}")
    except Exception as exc:
        record("rollback", FAIL, str(exc))


def check_fork(ctx: Optional[Any], cps: Optional[list]) -> None:
    if ctx is None or not cps:
        record("fork", SKIP, "no context/checkpoints")
        return
    bench, execution = ctx
    try:
        cp_id = str(cps[0].id)
        fork_result = bench.fork(
            execution.id,
            checkpoint_id=cp_id,
            new_initial_state={"messages": ["forked"], "count": 99, "result": ""},
        )
        assert fork_result.fork_execution_id, "fork_execution_id is empty"
        assert fork_result.fork_execution_id != execution.id
        record("fork", PASS, f"new_exec={fork_result.fork_execution_id[:12]}...")
    except Exception as exc:
        record("fork", FAIL, str(exc))


def check_batch_sequential() -> None:
    """Batch test via sequential fallback (no Ray)."""
    try:
        from wtb.sdk import WTBTestBench, WorkflowProject

        bench = WTBTestBench.create(mode="testing")
        project = WorkflowProject(name="batch_seq", graph_factory=_create_linear_graph)
        bench.register_project(project)

        batch = bench.run_batch_test(
            project="batch_seq",
            variant_matrix=[
                {"node_b": "default"},
                {"node_b": "alt"},
            ],
            test_cases=[
                dict(_INIT_STATE),
                {"messages": ["x"], "count": 5, "result": ""},
            ],
        )
        ok = sum(1 for r in batch.results if r.success)
        record("batch sequential", PASS,
               f"variants=2, cases=2, passed={ok}/{len(batch.results)}")
    except Exception as exc:
        record("batch sequential", FAIL, str(exc))


def check_batch_rollback_and_fork() -> None:
    """Rollback and fork batch results via the SDK convenience API.

    Uses SQLite (development mode) because batch rollback/fork requires
    shared persistent storage -- the BatchExecutionCoordinator creates its
    own controller/UoW, and in-memory UoWs are not shared across instances.
    """
    import tempfile, os

    tmp = tempfile.mkdtemp(prefix="wtb_check_")
    try:
        from wtb.sdk import WTBTestBench, WorkflowProject

        bench = WTBTestBench.create(mode="development", data_dir=tmp)
        project = WorkflowProject(name="br_test", graph_factory=_create_linear_graph)
        bench.register_project(project)

        batch = bench.run_batch_test(
            project="br_test",
            variant_matrix=[{"node_b": "default"}],
            test_cases=[dict(_INIT_STATE)],
        )
        result = batch.results[0]
        if not result.execution_id:
            record("batch rollback", SKIP, "no execution_id")
            record("batch fork", SKIP, "no execution_id")
            return

        cps = bench.get_batch_result_checkpoints(result)
        if not cps:
            record("batch rollback", SKIP, "no checkpoints")
            record("batch fork", SKIP, "no checkpoints")
            return

        rb = bench.rollback_batch_result(result, checkpoint_id=str(cps[0].id))
        assert rb.success, f"rollback error: {rb.error}"
        record("batch rollback", PASS, f"to step={cps[0].step}")

        fork = bench.fork_batch_result(
            result,
            checkpoint_id=str(cps[0].id),
            new_state={"messages": ["forked"], "count": 42, "result": ""},
        )
        assert fork.fork_execution_id, f"fork error: {fork.error}"
        record("batch fork", PASS,
               f"forked {fork.fork_execution_id[:12]}... from step={cps[0].step}")

        bench.close()
    except Exception as exc:
        record("batch rollback", FAIL, str(exc))
        record("batch fork", SKIP, "blocked by rollback failure")
        traceback.print_exc()
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Tier 2 -- Ray distributed batch
# ═══════════════════════════════════════════════════════════════════════════════

def check_ray_batch(skip: bool = False) -> None:
    if skip:
        record("ray batch", SKIP, "skipped via --skip-ray")
        return

    try:
        import ray
    except ImportError:
        record("ray batch", SKIP, "ray not installed")
        return

    import tempfile, shutil

    tmp = tempfile.mkdtemp(prefix="wtb_ray_")
    try:
        from wtb.sdk import WTBTestBench, WorkflowProject, ExecutionConfig, RayConfig

        if not ray.is_initialized():
            ray.init(num_cpus=2, ignore_reinit_error=True, log_to_driver=False)

        bench = WTBTestBench.create(
            mode="development",
            data_dir=tmp,
            enable_ray=True,
        )
        project = WorkflowProject(
            name="ray_smoke",
            graph_factory=_create_linear_graph,
            execution=ExecutionConfig(
                batch_executor="ray",
                ray_config=RayConfig(address="auto", max_retries=1),
            ),
        )
        bench.register_project(project)

        t0 = time.time()
        batch = bench.run_batch_test(
            project=project.name,
            variant_matrix=[{"node_b": "v0"}, {"node_b": "v1"}],
            test_cases=[dict(_INIT_STATE)],
        )
        elapsed = time.time() - t0
        ok = sum(1 for r in batch.results if r.success)
        record("ray batch", PASS,
               f"results={len(batch.results)}, passed={ok}, {elapsed:.1f}s")
        bench.close()
    except Exception as exc:
        record("ray batch", FAIL, str(exc))
        traceback.print_exc()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_ray_batch_cache_metadata(skip: bool = False) -> None:
    """Verify that Ray batch results carry actor-local cache metadata."""
    if skip:
        record("ray cache metadata", SKIP, "skipped via --skip-ray")
        return

    try:
        import ray
    except ImportError:
        record("ray cache metadata", SKIP, "ray not installed")
        return

    import tempfile, shutil

    tmp = tempfile.mkdtemp(prefix="wtb_ray_cache_")
    try:
        from wtb.sdk import WTBTestBench, WorkflowProject, ExecutionConfig, RayConfig

        if not ray.is_initialized():
            ray.init(num_cpus=2, ignore_reinit_error=True, log_to_driver=False)

        bench = WTBTestBench.create(
            mode="development",
            data_dir=tmp,
            enable_ray=True,
        )
        project = WorkflowProject(
            name="ray_cache_meta",
            graph_factory=_create_linear_graph,
            execution=ExecutionConfig(
                batch_executor="ray",
                ray_config=RayConfig(address="auto", max_retries=1),
            ),
        )
        bench.register_project(project)

        batch = bench.run_batch_test(
            project=project.name,
            variant_matrix=[{"node_b": "v0"}, {"node_b": "v1"}],
            test_cases=[dict(_INIT_STATE)],
        )

        checkpoint_paths = set()
        for r in batch.results:
            if not r.execution_id:
                record("ray cache metadata", FAIL, "result missing execution_id")
                return
            meta = bench.get_execution(r.execution_id).metadata or {}
            assert meta.get("actor_id"), f"missing actor_id in {r.combination_name}"
            assert str(meta.get("checkpoint_db_path", "")).endswith("wtb_checkpoints.db"), \
                f"bad checkpoint_db_path: {meta.get('checkpoint_db_path')}"
            assert str(meta.get("llm_cache_path", "")).endswith("llm_response_cache.db"), \
                f"bad llm_cache_path: {meta.get('llm_cache_path')}"
            assert meta.get("cache_storage_scope") == "actor_local", \
                f"bad scope: {meta.get('cache_storage_scope')}"
            checkpoint_paths.add(meta["checkpoint_db_path"])

        assert len(checkpoint_paths) >= 1, "no checkpoint_db_path found in metadata"
        isolation_note = (
            f"unique_paths={len(checkpoint_paths)}"
            if len(checkpoint_paths) > 1
            else "single_actor (ok for small batch)"
        )
        record("ray cache metadata", PASS,
               f"{isolation_note}, scope=actor_local")
        bench.close()
    except Exception as exc:
        record("ray cache metadata", FAIL, str(exc))
        traceback.print_exc()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_ray_batch_rollback_fork_cache(skip: bool = False) -> None:
    """Verify rollback/fork of Ray batch results preserves cache metadata."""
    if skip:
        record("ray cache rollback", SKIP, "skipped via --skip-ray")
        record("ray cache fork", SKIP, "skipped via --skip-ray")
        return

    try:
        import ray
    except ImportError:
        record("ray cache rollback", SKIP, "ray not installed")
        record("ray cache fork", SKIP, "ray not installed")
        return

    import tempfile, shutil

    tmp = tempfile.mkdtemp(prefix="wtb_ray_rbfk_")
    try:
        from wtb.sdk import WTBTestBench, WorkflowProject, ExecutionConfig, RayConfig

        if not ray.is_initialized():
            ray.init(num_cpus=2, ignore_reinit_error=True, log_to_driver=False)

        bench = WTBTestBench.create(
            mode="development",
            data_dir=tmp,
            enable_ray=True,
        )
        project = WorkflowProject(
            name="ray_rbfk",
            graph_factory=_create_linear_graph,
            execution=ExecutionConfig(
                batch_executor="ray",
                ray_config=RayConfig(address="auto", max_retries=1),
            ),
        )
        bench.register_project(project)

        batch = bench.run_batch_test(
            project=project.name,
            variant_matrix=[{"node_b": "default"}],
            test_cases=[dict(_INIT_STATE)],
        )
        result = batch.results[0]
        if not result.execution_id:
            record("ray cache rollback", SKIP, "no execution_id")
            record("ray cache fork", SKIP, "no execution_id")
            return

        src_meta = bench.get_execution(result.execution_id).metadata or {}
        cps = bench.get_batch_result_checkpoints(result)
        if not cps:
            record("ray cache rollback", SKIP, "no checkpoints")
            record("ray cache fork", SKIP, "no checkpoints")
            return

        cp_id = str(cps[0].id)

        # ── Rollback ──
        rb = bench.rollback_batch_result(result, checkpoint_id=cp_id)
        assert rb.success, f"rollback error: {rb.error}"
        rb_meta = bench.get_execution(result.execution_id).metadata or {}
        for key in ("actor_id", "checkpoint_db_path", "llm_cache_path"):
            assert rb_meta.get(key), f"rollback lost metadata key: {key}"
        assert rb_meta.get("cache_storage_scope") == "actor_local"
        record("ray cache rollback", PASS,
               f"step={cps[0].step}, actor={rb_meta['actor_id']}")

        # ── Fork ──
        fork = bench.fork_batch_result(
            result,
            checkpoint_id=cp_id,
            new_state={"messages": ["forked"], "count": 42, "result": ""},
        )
        assert fork.fork_execution_id, f"fork error: {fork.error}"
        fk_meta = bench.get_execution(fork.fork_execution_id).metadata or {}
        for key in ("actor_id", "checkpoint_db_path", "llm_cache_path", "cache_storage_scope"):
            assert fk_meta.get(key), f"fork lost metadata key: {key}"
        assert fk_meta["forked_from"] == result.execution_id
        assert "requested_execution_id" not in fk_meta
        assert fk_meta["checkpoint_db_path"] == src_meta.get("checkpoint_db_path"), \
            "fork changed checkpoint_db_path -- should inherit from source"
        record("ray cache fork", PASS,
               f"forked={fork.fork_execution_id[:12]}..., inherits_cache=True")

        bench.close()
    except Exception as exc:
        record("ray cache rollback", FAIL, str(exc))
        record("ray cache fork", SKIP, "blocked by rollback failure")
        traceback.print_exc()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)




# ═══════════════════════════════════════════════════════════════════════════════
# Tier 3 -- Venv service (GrpcEnvironmentProvider)
# ═══════════════════════════════════════════════════════════════════════════════

def check_venv_provider(grpc_url: Optional[str]) -> None:
    if grpc_url is None:
        record("venv provider", SKIP, "no --grpc-url provided")
        return

    try:
        import grpc  # noqa: F401
    except ImportError:
        record("venv provider", SKIP, "grpcio not installed")
        return

    try:
        from wtb.infrastructure.environment.providers import GrpcEnvironmentProvider

        provider = GrpcEnvironmentProvider(grpc_address=grpc_url)

        env = provider.create_environment("smoke-variant", {
            "workflow_id": "install_check",
            "node_id": "smoke_node",
            "packages": ["requests"],
            "python_version": "3.12",
        })

        assert env.get("env_path") or env.get("type"), f"unexpected env: {env}"
        record("venv create", PASS,
               f"type={env.get('type')}, path={env.get('env_path', 'n/a')}")

        rt = provider.get_runtime_env("smoke-variant")
        has_path = bool(rt and (rt.get("python_path") or rt.get("py_executable")))
        record("venv runtime_env", PASS if has_path else FAIL,
               f"python_path={'found' if has_path else 'missing'}")

        provider.cleanup_environment("smoke-variant")
        record("venv cleanup", PASS)

        provider.close()
    except Exception as exc:
        record("venv provider", FAIL, str(exc))
        traceback.print_exc()


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> int:
    parser = argparse.ArgumentParser(description="WTB Installation Checker")
    parser.add_argument("--skip-ray", action="store_true",
                        help="Skip Ray-dependent checks")
    parser.add_argument("--grpc-url", type=str, default=None,
                        help="UV Venv Manager gRPC address (e.g. localhost:50051)")
    args = parser.parse_args()

    print("=" * 64)
    print("  WTB Installation Checker")
    print("=" * 64)

    # ── Tier 1: Core (always) ────────────────────────────────────────────
    print("\n  --- Tier 1: Core SDK ---\n")
    check_import()
    check_sdk_imports()
    check_create_bench()
    ctx = check_run_workflow()
    cps = check_checkpoints(ctx)
    check_rollback(ctx, cps)
    check_fork(ctx, cps)
    check_batch_sequential()
    check_batch_rollback_and_fork()

    # ── Tier 2: Ray ──────────────────────────────────────────────────────
    print("\n  --- Tier 2: Ray Distributed ---\n")
    check_ray_batch(skip=args.skip_ray)
    check_ray_batch_cache_metadata(skip=args.skip_ray)
    check_ray_batch_rollback_fork_cache(skip=args.skip_ray)

    # ── Tier 3: Venv Service ─────────────────────────────────────────────
    print("\n  --- Tier 3: Venv Service ---\n")
    check_venv_provider(args.grpc_url)

    # ── Summary ──────────────────────────────────────────────────────────
    print()
    print("-" * 64)
    passed = sum(1 for _, s, _ in results if s == PASS)
    failed = sum(1 for _, s, _ in results if s == FAIL)
    skipped = sum(1 for _, s, _ in results if s == SKIP)
    print(f"  Total: {len(results)}  |  Passed: {passed}"
          f"  |  Failed: {failed}  |  Skipped: {skipped}")
    print("-" * 64)

    if failed:
        print("\n  Some checks FAILED. See details above.\n")
        return 1

    print("\n  All attempted checks PASSED.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
