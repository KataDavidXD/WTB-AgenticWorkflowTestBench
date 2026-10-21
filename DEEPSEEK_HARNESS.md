# DeepSeek Harness integration and operator safety

This document describes WTB `0.4.0.dev0`'s source-only DeepSeek Harness
adapter. It is an operator contract, not a claim that the current published
Harness SDK supports every capability described here.

## Ownership and protocol boundary

```text
WTB governance
  -> WorkflowRuntime / DeepSeekHarnessRuntime
    -> DeepSeek Harness Python SDK
      -> JSON-RPC runtime
        -> Cordis agent composition
```

DeepSeek Harness owns model/session creation, the agent loop, tools, and Cordis
lifecycle. WTB owns workflow execution identity, durable attempt admission,
recovery policy, workspace/checkpoint/CAS evidence, evaluation, audit, and
operator-facing lifecycle controls. WTB uses the public SDK/JSON-RPC boundary;
it does not implement a privileged agent loop inside Cordis.

## Current support state

The DeepSeek Harness operator in WTB 0.4.0.dev0 is source-only. The audited
`deepseek-harness-sdk==0.1.0rc6` wheel does not expose durable attempt
identity, exact lookup/cancel, schema-v2 runtime binding, or process-generation
admission. WTB therefore does not claim that wheel as compatible.

There is no installable `wtb[deepseek-harness]` extra. The source prerequisite
extra does not install `deepseek-harness-sdk`; it installs only the Python-side
prerequisite for a reviewed local checkout:

```bash
pip install -e ".[deepseek-harness-source]"
```

The adapter feature-detects the durable contract and fails closed before
dispatch. Missing contract fields must perform no model or tool request.

The public [DeepSeek Harness Authority](https://github.com/KataDavidXD/deepseek-harness-authority)
source snapshot supplies the base durable-attempt extension. Its optional
reference-memory extension was excluded from that snapshot, so it does not
provide the full S0/M0 APIs described below. Reference-enabled workflows need
a separately reviewed SDK/runtime checkout that accepts `references` and
`memory_mode` and returns the required capture receipts. Neither the source
extra nor a matching project name proves compatibility.

The durable extension stays in `@deepseek-ai/dsh-sdk-attempt-protocol` and uses
`session/attempt.binding`, `session/attempt.start`, `session/attempt.get`, and
`session/attempt.cancel`. The official `initialize` response remains
`{serverInfo}`; WTB does not add its fields to the official protocol.

## Release sequencing

The stable release gate remains closed while
`tool.wtb.release.deepseek-harness-distribution` is `source-only`:

1. Publish a Harness SDK/runtime version containing `durable-attempt-v2`.
2. Pin that exact distribution and execute it in CI.
3. Change WTB release metadata to `published`.
4. Only then replace `0.4.0.dev0` with a stable WTB version.

Do not invent or relax a dependency pin for an unreleased SDK.

## Minimal WTB composition

```python
import os

from wtb.infrastructure.runtimes import (
    DeepSeekHarnessRuntime,
    DeepSeekHarnessWorkflow,
)
from wtb.sdk import WorkflowProject, WTBTestBench


runtime = DeepSeekHarnessRuntime(
    session_namespace="acme-prod-wtb-v1",
    runtime_config_sha256=os.environ["WTB_DSH_DEPLOYMENT_SHA256"],
    harness_options={
        "cwd": "/absolute/workspace",
        "session_root": "/absolute/sessions",
    },
)
workflow = DeepSeekHarnessWorkflow()
project = WorkflowProject(
    name="deepseek-agent",
    graph_factory=lambda: DeepSeekHarnessWorkflow(),
)

with WTBTestBench.create_with_runtime(
    runtime,
    data_dir="/absolute/wtb-data",
    workflow=workflow,
) as bench:
    bench.attach_project(project)
    result = bench.run(
        project=project.name,
        initial_state={"prompt": "Inspect this workspace."},
    )
```

`WTBTestBench.create_with_runtime(runtime)` defaults to in-memory repositories
and is testing-only. Supplying an absolute `data_dir` persists WTB execution
records, attempt plans, outbox entries, dispatch fences, and WTB-owned state
across a reopen. It does not manufacture a rewindable Harness checkpoint.

Keep the namespace and reviewed deployment fingerprint stable across restarts.
Changing the provider, model, command, workflow mapping, Cordis contents, or
runtime roots is configuration drift and must fail before recovery or dispatch.

## Source checkout setup

On a supported POSIX host, use a Node version supported by the selected Harness
checkout (Node 22.19+ on the 22.x line, or Node 24+ for the referenced source),
install its workspace with `pnpm install`, run its build, and pass absolute paths:

```python
import os
from pathlib import Path

from wtb.infrastructure.runtimes import DeepSeekHarnessRuntime


harness_repo = Path("/absolute/deepseek-harness")
workspace = Path("/absolute/agent-workspace")
runtime = DeepSeekHarnessRuntime(
    session_namespace="acme-prod-wtb-v1",
    runtime_config_sha256=os.environ["WTB_DSH_DEPLOYMENT_SHA256"],
    sdk_source_path=harness_repo,
    runtime_command=(
        "node",
        str(harness_repo / "packages/examples/jsonrpc-demo/lib/bin.js"),
        str(harness_repo / "examples/jsonrpc-agent/cordis.yml"),
    ),
    harness_options={
        "cwd": str(workspace),
        "runtime_cwd": str(harness_repo),
        "cordis": str(harness_repo / "examples/jsonrpc-agent/cordis.yml"),
    },
)
```

The caller owns compatibility validation for the exact checkout. Review and pin
its source revision, install its workspace dependencies, and run its required
build before configuring WTB. A source path or custom command is an explicit
injection hook; WTB still validates the durable capability and binding evidence.

### Verify a source composition

Install WTB's test prerequisites from this checkout and point the tests at the
compatible Harness source. On POSIX:

```sh
python -m pip install -e ".[dev,api,deepseek-harness-source]"
export WTB_DSH_SOURCE_PATH=/absolute/deepseek-harness
export WTB_REQUIRE_DSH_REAL_E2E=1
python -m pytest tests/unit/infrastructure/test_deepseek_harness_runtime.py tests/unit/infrastructure/test_deepseek_runtime_binding.py tests/integration/test_deepseek_operator_real_e2e.py -q
```

In PowerShell, set `$env:WTB_DSH_SOURCE_PATH` and
`$env:WTB_REQUIRE_DSH_REAL_E2E = '1'` before invoking pytest. The source-path
override also works when the WTB and Harness checkouts are not siblings.
Without it, the tests look for a sibling `deepseek-harness` checkout. The real
operator suite requires the built JSON-RPC example entrypoint and a compatible
Node runtime. It uses a deterministic local HTTP endpoint for ordinary cases;
live provider tests require their separate opt-in environment and credentials.
This does not establish Windows support for the full shipped agent composition.

## Durable attempt and runtime binding

Each durable dispatch binds a `RuntimeBindingEnvelope` to the exact Harness
session, persistence instance, runtime configuration, protocol/schema versions,
input digest, and WTB attempt. Invoke, lookup, and cancel receive that same
identity. They never reconstruct a recovery target from a filesystem path.

For schema v2, the runtime fingerprint combines the reviewed operator
configuration with Harness's attestation of the exact root-config bytes read by
its loader. Missing or malformed evidence fails closed. `prepare_attempt` also
records one opaque process-generation token so an initial invoke cannot be
admitted by a different restarted process.

The persistence-instance ID proves store lineage, not freshness. A restored
older copy at the same location cannot be detected locally. Rollback-resistant
automatic recovery needs an external monotonic generation authority or an
upstream prepare/activate protocol.

## Recovery rules

The source candidate supplies exact durable lookup by session, attempt, and
input hash:

- A validated terminal receipt may be materialized without another model turn.
- `not_found`, accepted, pending, queued, running, and interrupted observations
  are outcome-unknown and never permit automatic redrive.
- Non-terminal observations perform no model or tool request and keep
  `recovery_required=true`.
- Identity, input, binding, generation, or message mismatch fails closed.

This prevents blind replay. It does not make the WTB database, Harness attempt
registry, and arbitrary external tool side effects one atomic transaction.
Exactly-once recovery remains a hold without monotonic external authority.

The current source fork exposes durable session evidence but not checkpoint
rewind. Harness-backed WTB snapshots therefore use `checkpoint_id=None`.
Resume, rollback, fork, and checkpoint-linked file recovery fail closed when
the external runtime cannot provide matching rewind semantics.

WTB's SQLite execution repository uses an atomic compare-and-swap dispatch
claim committed with its prepared outbox event. Competing workers have one
winner; reconciliation and cancellation use the same active-attempt rule.

## Reference snapshots

`DeepSeekHarnessWorkflow` can bind one to three structured source sessions:

```python
workflow = DeepSeekHarnessWorkflow(
    reference_key="memory_references",
    memory_mode="S0",
)
initial_state = {
    "prompt": "Use the captured source facts.",
    "memory_references": [
        {"sessionId": "source-session-id", "label": "source"},
    ],
}
```

Reference order, mode, and capture evidence are included in the input digest.
`S0` pins one capture per attempt. `M0` requires ordered per-step snapshots.
Harness performs the Cordis pre-step refresh; WTB validates detached proof.

## Disclosure controls

Safe defaults are `include_events=False` and `include_session_root=False`.
Enabling either may copy unredacted session events or local paths into WTB
state. Event copies are bounded by `max_event_count` and `max_event_bytes`.
Apply an application disclosure policy before exposing them through an API or
audit export.

## Platform limits

The audited runtime carrier wheels cover `manylinux_2_28_x86_64`,
`manylinux_2_28_aarch64`, and `macosx_14_0_arm64`. There is no Windows or macOS
x86_64 wheel. The shipped agent composition also needs a POSIX terminal
substrate, so WTB makes no Windows DeepSeek Harness support claim.

The source extra is platform-neutral only because it does not install a
runtime. A separately built carrier is the operator's compatibility claim.
