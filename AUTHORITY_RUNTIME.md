# Authority and runtime evidence

WTB `0.4.0.dev0` integrates framework-neutral workflow execution, durable lifecycle control, and runtime evidence with action and publication decisions. Native Python execution is the default; LangGraph and DeepSeek Harness are explicit runtime choices. The SDK and execution controller govern attempts, state, workspace/CAS evidence, cancellation, and recovery through the same runtime ports used by the console.

This is a development source snapshot. Install this checkout to use these capabilities; a package registry release can contain an earlier feature set. Reproducing an experiment also requires its separately pinned dependencies, inputs, and configuration.

## Available modules

| Module | Responsibility |
| --- | --- |
| `wtb.domain.interfaces.workflow_runtime` | Exact session, persistence instance, configuration, input, and attempt binding; terminal receipts and capability declarations |
| `wtb.application.services.execution_controller` and `wtb.sdk.test_bench` | Native and external runtime composition, lifecycle control, persisted attempt admission, recovery, and isolated variant execution |
| `wtb.infrastructure.runtimes.deepseek_harness` | Python SDK/JSON-RPC adapter with durable attempt lookup, cancellation, and S0/M0 reference receipts |
| `wtb.infrastructure.adapters.langgraph_workflow_runtime` | Optional LangGraph facade retaining native checkpoint identity |
| `wtb.application.services.evidence_collector` | Validate matching identities and collect immutable evidence |
| `wtb.infrastructure.evidence.sqlite_store` | SQLite evidence storage and transactional updates |
| `wtb.infrastructure.evidence.postgres_store` | PostgreSQL evidence storage with pooled connections |
| `wtb.domain.action_authority` and `wtb.domain.publication_authority` | Fail-closed action and multi-contributor publication decisions |
| `wtb.infrastructure.evidence.publication_sink` | Check current contributor, permission, material generation, and approval facts in the same local transaction as publication |
| `wtb.infrastructure.evidence.external_delivery` | Immutable delivery keys, cooperating receiver checks, deduplication, and conservative unknown-outcome recovery |

Compose a runtime through `WTBTestBench.create`, `WorkflowProject.runtime_backend`, or `WTBTestBench.create_with_runtime`. Evidence stores, publication sinks, and external delivery remain explicit application integrations; installing an extra does not grant publication authority or select an execution backend. See the [runtime examples](README.md) and [DeepSeek operator setup](DEEPSEEK_HARNESS.md).

```sh
pip install -e ".[dev,postgres-evidence,deepseek-harness-source]"
```

DeepSeek Harness needs a separately reviewed compatible SDK/runtime source checkout and Cordis configuration. The required source contract includes durable attempt binding and process-generation admission; S0/M0 additionally require reference capture and per-step receipts. The published SDK is not a supported substitute. The public DeepSeek Harness Authority source snapshot provides the base durable extension but does not supply its optional S0/M0 APIs; use a checkout that passes those capability gates for reference-enabled workflows. PostgreSQL tests need a dedicated test database. Provider-backed checks need user-supplied credentials; ordinary evidence and authority tests use deterministic fixtures.

The execution path remains `WTB -> Python SDK -> JSON-RPC -> Cordis`. DeepSeek Harness owns its agent loop, tools, sessions, and plugin lifecycle. WTB validates durable evidence and governs execution; it does not move its controller into Cordis.

## Guarantees and limits

A local publication transaction checks authority, consumes approval, and records the exact effect together. External delivery reserves a fixed key and payload; its receiver must atomically check its own current authority token and deduplicate that key. An unresolved external outcome remains pending until authoritative lookup resolves it.

Material generations distinguish equal-byte replacements. Scoped permission generations distinguish uninterrupted permission from revoke-and-regrant. Receipts must match the admitted attempt and input. These mechanisms rely on trusted adapters, correct actor assignment, durable stores, mediated effects, and receiver cooperation.

The DeepSeek adapter supplies durable receipt reconciliation and declares checkpoint rewind unavailable. The LangGraph facade uses a process-local provider identity; a newly constructed facade cannot infer continuity from a path. Source availability does not establish uniform restart recovery across runtimes.

For equal-byte replacement and revoke/regrant semantics, see the [material-generation migration guide](docs/e1-material-version-upgrade.md). [PostgreSQL setup](docs/POSTGRES_EVIDENCE_ZH.md) describes connection pools and transaction ownership.

## Local verification

```sh
python -m pytest tests/unit/evidence tests/unit/test_action_authority_operator.py tests/unit/test_publication_authority_operator.py tests/unit/infrastructure/test_deepseek_harness_runtime.py tests/unit/infrastructure/test_deepseek_runtime_binding.py tests/integration/test_external_delivery_crash.py tests/integration/test_publication_sink_crash.py tests/test_api/test_console_real.py -q
```

The code retains WTB's [Apache-2.0 license](LICENSE).
