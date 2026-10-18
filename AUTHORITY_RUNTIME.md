# Authority and runtime evidence

WTB `0.4.0.dev0` adds opt-in modules for binding runtime evidence to publication decisions. Existing execution-controller, SDK, console, and web defaults retain their `0.3.1` behavior.

This is a development source snapshot. It is not a PyPI release or a frozen experiment revision. Reproducing a recorded experiment requires that experiment's separately pinned dependencies, inputs, and configuration.

## Available modules

| Module | Responsibility |
| --- | --- |
| `wtb.domain.interfaces.workflow_runtime` | Exact session, persistence instance, configuration, input, and attempt binding; terminal receipts and capability declarations |
| `wtb.infrastructure.runtimes.deepseek_harness` | Opt-in Python SDK/JSON-RPC adapter with durable attempt lookup and cancellation |
| `wtb.infrastructure.adapters.langgraph_workflow_runtime` | Opt-in LangGraph facade retaining native checkpoint identity |
| `wtb.application.services.evidence_collector` | Validate matching identities and collect immutable evidence |
| `wtb.infrastructure.evidence.sqlite_store` | SQLite evidence storage and transactional updates |
| `wtb.infrastructure.evidence.postgres_store` | PostgreSQL evidence storage with pooled connections |
| `wtb.domain.action_authority` and `wtb.domain.publication_authority` | Fail-closed action and multi-contributor publication decisions |
| `wtb.infrastructure.evidence.publication_sink` | Check current contributor, permission, material generation, and approval facts in the same local transaction as publication |
| `wtb.infrastructure.evidence.external_delivery` | Immutable delivery keys, cooperating receiver checks, deduplication, and conservative unknown-outcome recovery |

Import these modules directly. Installing an optional runtime does not change the existing controller's execution backend. The DeepSeek adapter expects a compatible source checkout, such as [DeepSeek Harness Authority](https://github.com/KataDavidXD/deepseek-harness-authority); the upstream package published under a similar name may expose different APIs.

```sh
pip install -e ".[postgres-evidence,deepseek-harness-source]"
```

The runtime adapter needs the separate SDK/runtime source and configuration. PostgreSQL tests need a dedicated test database. Provider-backed checks need user-supplied credentials; the ordinary evidence and authority unit tests use deterministic fixtures.

## Guarantees and limits

A local publication transaction checks authority, consumes approval, and records the exact effect together. External delivery reserves a fixed key and payload; its receiver must atomically check its own current authority token and deduplicate that key. An unresolved external outcome remains pending until authoritative lookup resolves it.

Material generations distinguish equal-byte replacements. Scoped permission generations distinguish uninterrupted permission from revoke-and-regrant. Receipts must match the admitted attempt and input. These mechanisms rely on trusted adapters, correct actor assignment, durable stores, mediated effects, and receiver cooperation.

The DeepSeek adapter supplies durable receipt reconciliation and declares checkpoint rewind unavailable. The LangGraph facade uses a process-local provider identity; a newly constructed facade cannot infer continuity from a path. Source availability does not establish uniform restart recovery across runtimes.

## Local verification

```sh
python -m pytest tests/unit/evidence tests/unit/test_action_authority_operator.py tests/unit/test_publication_authority_operator.py tests/unit/infrastructure/test_deepseek_harness_runtime.py tests/unit/infrastructure/test_deepseek_runtime_binding.py tests/integration/test_external_delivery_crash.py tests/integration/test_publication_sink_crash.py tests/test_api/test_console_real.py -q
```

The code retains WTB's [Apache-2.0 license](LICENSE).
