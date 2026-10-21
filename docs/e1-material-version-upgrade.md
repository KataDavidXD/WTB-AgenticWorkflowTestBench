# Mandatory material-generation binding in SQL publication

`SQLPublicationSink.approve(target)` binds the current target's stored material
generation automatically, in the same transaction as the approval. Applications
use the existing `record_material`, `approve`, and `publish` interfaces. An
application-side approval/version map is no longer needed for this constraint.

The stored generation distinguishes successive material records even when their
payload bytes, mutation observations, and execution receipts are identical.
`verify` checks this binding before permitting a fresh local effect or an external
reservation. The same check protects an explicit redrive after authoritative
absence. Snapshot content equivalence alone does not grant permission to reuse an
approval across material generations.

The trusted material owner must record each new generation through
`record_material`, even when its bytes and observations match the previous one.
The method increments the stored version on every successful call;
`expected_version` checks the prior generation and does not request content-based
deduplication. A wrapper that returns early without calling this port suppresses
the state transition itself. Approval verification can only enforce transitions
recorded by the trusted writer.

## Upgrade behavior

- The library creates `publication_approval_material_versions` on SQLite and
  PostgreSQL. New approvals receive an immutable `(approval, target, version)` row.
- A pending legacy approval without this binding is rejected with
  `approval_version_missing`; obtain a fresh approval for current material.
  The upgrade does not infer historical identity from the current version.
- Existing committed local effects remain replayable without creating a new
  effect. Unknown external effects remain lookup-first; reconciliation of an
  already committed effect continues even after material changes.
- Change every process that can create a fresh effect to the upgraded library.
  Mixed deployment with the old binary retains the old binary's behavior.
  All material and publication writers continue to share the backend's
  transaction boundary; direct SQL mutation bypasses this trusted API contract.
- The receiver's authorization and idempotency requirements remain unchanged.
  This change does not add global atomicity between a local reservation and a
  remote receiver commit.

## Regression scope

The regression tests cover identical material bytes across generations, process restart,
fresh approval, missing legacy bindings, unrelated targets, completed effect
replay, external reservation, absence-based redrive, and unknown-effect recovery.
The same contract tests are imported by the real PostgreSQL suite.

From the repository root, run the deterministic SQLite checks with:

```sh
python -m pip install -e ".[dev]"
python -m pytest -q tests/unit/evidence/test_material_version_binding.py tests/unit/evidence/test_publication_sink.py tests/unit/evidence/test_external_delivery.py
```

For PostgreSQL setup and the corresponding real-database checks, see
[PostgreSQL evidence storage](POSTGRES_EVIDENCE_ZH.md). A missing database or
runtime prerequisite can skip an integration test; a skip is not a passed check.
The runtime ownership and publication guarantees are described in the
[execution evidence architecture](RUNTIME_EVIDENCE_ARCHITECTURE_ZH.md).
