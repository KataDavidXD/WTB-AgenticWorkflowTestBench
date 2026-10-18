"""Framework-neutral runtime facade for the legacy LangGraph state adapter."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any
import uuid

from wtb.domain.interfaces.workflow_runtime import (
    RuntimeAttemptPlan,
    RuntimeBindingEnvelope,
    RuntimeBindingMismatchError,
    RuntimeOutcomeReceipt,
    RuntimePreDispatchError,
    RuntimeSnapshot,
    RuntimeTerminalOutcome,
)
from wtb.domain.runtime_identity import canonical_json_sha256


class LangGraphWorkflowRuntime:
    """Preserve LangGraph behavior behind :class:`WorkflowRuntime`.

    This facade is intentionally small. The underlying state adapter continues
    to own sessions and checkpoint persistence while the facade owns workflow
    binding and invocation.
    """

    def __init__(self, state_adapter: Any) -> None:
        self._state_adapter = state_adapter
        self._attempt_receipts: dict[str, RuntimeOutcomeReceipt] = {}
        self._attempt_bindings: dict[str, RuntimeBindingEnvelope] = {}
        # This compatibility adapter cannot prove a durable provider identity
        # across process restart. A process-local opaque identity therefore
        # makes stale recovery fail closed instead of guessing from a path.
        self._persistence_instance_id = uuid.uuid4().hex
        self._runtime_config_sha256 = canonical_json_sha256(
            {
                "adapter": "wtb.langgraph.compatibility.v1",
                "state_adapter_type": (
                    f"{type(state_adapter).__module__}."
                    f"{type(state_adapter).__qualname__}"
                ),
                "persistence_instance_id": self._persistence_instance_id,
            }
        )

    def supports_checkpoint_recovery(self) -> bool:
        """LangGraph checkpoints can be pinned and invoked again."""
        return True

    def supports_forking(self) -> bool:
        """WTB can clone LangGraph checkpoint state into a new thread."""
        return True

    def shares_checkpoint_store(self, state_adapter: Any) -> bool:
        """Prove that pause receipts and controller state use one adapter."""
        return state_adapter is self._state_adapter

    def for_state_adapter(self, state_adapter: Any) -> LangGraphWorkflowRuntime:
        """Return an equivalent facade bound to an execution-local adapter."""
        return type(self)(state_adapter)

    def is_bound(self) -> bool:
        return bool(self._state_adapter.has_graph())

    def bind(self, workflow: Any, *, force_rebind: bool = True) -> None:
        self._state_adapter.set_workflow_graph(
            workflow,
            force_recompile=force_rebind,
        )

    def prepare_attempt(
        self,
        execution_id: str,
        attempt_id: str,
        *,
        input_sha256: str,
        checkpoint_id: str | None = None,
    ) -> RuntimeAttemptPlan:
        binding = RuntimeBindingEnvelope(
            exact_session_id=f"wtb-{execution_id}",
            persistence_instance_id=self._persistence_instance_id,
            runtime_config_sha256=self._runtime_config_sha256,
            protocol_version=1,
            schema_version=1,
            input_sha256=input_sha256,
            attempt_id=attempt_id,
        )
        existing_binding = self._attempt_bindings.get(attempt_id)
        if existing_binding is not None and existing_binding != binding:
            raise RuntimePreDispatchError(
                "LangGraph attempt identity is already bound to another input"
            )
        self._attempt_bindings[attempt_id] = binding
        return RuntimeAttemptPlan(
            execution_id=execution_id,
            attempt_id=attempt_id,
            session_id=f"wtb-{execution_id}",
            binding=binding,
            metadata={
                "input_sha256": input_sha256,
                "checkpoint_id": checkpoint_id,
            },
        )

    def invoke(
        self,
        input_state: Mapping[str, Any] | None,
        *,
        execution_id: str,
        checkpoint_id: str | None = None,
        attempt_id: str | None = None,
        binding: RuntimeBindingEnvelope | None = None,
        attempt_metadata: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        del attempt_metadata
        attempt_binding: RuntimeBindingEnvelope | None = None
        if attempt_id is not None:
            attempt_binding = self._attempt_bindings.get(attempt_id)
            if (
                attempt_binding is None
                or binding != attempt_binding
                or binding.exact_session_id != f"wtb-{execution_id}"
            ):
                raise RuntimePreDispatchError(
                    "LangGraph attempt was not prepared for this execution"
                )
        if checkpoint_id is not None:
            set_resume_checkpoint = getattr(
                self._state_adapter,
                "set_resume_checkpoint",
                None,
            )
            if not callable(set_resume_checkpoint):
                raise RuntimeError(
                    "LangGraph state adapter cannot pin a resume checkpoint"
                )
            set_resume_checkpoint(checkpoint_id)
        result = self._state_adapter.execute(
            None if input_state is None else dict(input_state)
        )
        if not isinstance(result, Mapping):
            raise TypeError("Workflow runtime result must be a mapping")
        if attempt_id is not None:
            snapshot = self.snapshot(execution_id)
            self._attempt_receipts[attempt_id] = RuntimeOutcomeReceipt(
                execution_id=execution_id,
                attempt_id=attempt_id,
                input_sha256=attempt_binding.input_sha256,
                outcome=RuntimeTerminalOutcome.COMPLETED,
                binding=attempt_binding,
                values=dict(result),
                receipt=(
                    {
                        "checkpoint_id": snapshot.checkpoint_id,
                        **dict(snapshot.metadata),
                    }
                    if snapshot is not None
                    else {}
                ),
                session_id=f"wtb-{execution_id}",
            )
        return result

    def reconcile(
        self,
        execution_id: str,
        attempt_id: str,
        *,
        binding: RuntimeBindingEnvelope,
        input_state: Mapping[str, Any] | None = None,
    ) -> RuntimeOutcomeReceipt | None:
        del input_state
        expected = self._attempt_bindings.get(attempt_id)
        if expected != binding or binding.exact_session_id != f"wtb-{execution_id}":
            raise RuntimeBindingMismatchError(
                "LangGraph runtime binding differs from the persisted fence"
            )
        receipt = self._attempt_receipts.get(attempt_id)
        if receipt is None or receipt.execution_id != execution_id:
            return None
        return receipt

    def cancel(
        self,
        execution_id: str,
        attempt_id: str,
        *,
        binding: RuntimeBindingEnvelope,
        input_state: Mapping[str, Any] | None = None,
        input_sha256: str | None = None,
    ) -> RuntimeOutcomeReceipt:
        del input_state
        expected = self._attempt_bindings.get(attempt_id)
        if (
            expected != binding
            or binding.exact_session_id != f"wtb-{execution_id}"
            or (input_sha256 is not None and input_sha256 != binding.input_sha256)
        ):
            raise RuntimeBindingMismatchError(
                "LangGraph runtime binding differs from the persisted fence"
            )
        raise RuntimeError("LangGraph runtime has no verified in-flight cancel port")

    def snapshot(
        self,
        execution_id: str,
        checkpoint_id: str | None = None,
    ) -> RuntimeSnapshot | None:
        history = self.history(execution_id)
        if checkpoint_id is None:
            return history[0] if history else None
        return next(
            (item for item in history if item.checkpoint_id == checkpoint_id),
            None,
        )

    def history(self, execution_id: str) -> Sequence[RuntimeSnapshot]:
        current_session = getattr(
            self._state_adapter,
            "get_current_session_id",
            lambda: None,
        )()
        if current_session != f"wtb-{execution_id}":
            return ()
        snapshots: list[RuntimeSnapshot] = []
        for item in self._state_adapter.get_checkpoint_history() or []:
            checkpoint_id = item.get("checkpoint_id") or item.get("id")
            metadata = {
                key: deepcopy(value)
                for key, value in item.items()
                if key not in {"checkpoint_id", "id", "values"}
            }
            snapshots.append(
                RuntimeSnapshot(
                    execution_id=execution_id,
                    checkpoint_id=(
                        str(checkpoint_id) if checkpoint_id is not None else None
                    ),
                    values=deepcopy(item.get("values") or {}),
                    metadata=metadata,
                )
            )
        return tuple(snapshots)

    def patch_checkpoint(
        self,
        execution_id: str,
        checkpoint_id: str,
        values: Mapping[str, Any],
        *,
        as_node: str | None = None,
    ) -> str:
        """Map the neutral patch port to LangGraph's active-thread update."""
        snapshot = self.snapshot(execution_id, checkpoint_id)
        if snapshot is None:
            raise RuntimeError(
                f"Checkpoint {checkpoint_id!r} does not belong to {execution_id!r}"
            )
        update_checkpoint_state = getattr(
            self._state_adapter,
            "update_checkpoint_state",
            None,
        )
        if not callable(update_checkpoint_state):
            raise RuntimeError("LangGraph adapter cannot patch an exact checkpoint")
        return update_checkpoint_state(
            checkpoint_id,
            dict(values),
            as_node=as_node,
        )
