"""Native WTB console integration. LangGraph is neither imported nor required.

All execution transitions, checkpoint ownership and durable resume claims remain
in ExecutionController. This adapter publishes the local console read model.
"""

import copy

from wtb.application.services.console_service import WorkspaceTracker, now, uid


class NativeConsoleRuntime:
    def __init__(self, service, controller, uow, tracker):
        self.service, self.ctrl, self.uow, self.tracker = (
            service,
            controller,
            uow,
            tracker,
        )
        self.tasks = {}

    def event(self, execution, node, status, error):
        if status == "started":
            self.tasks[node] = uid()
        self.service.node_event(
            execution.id,
            {
                "type": "task" if status == "started" else "task_result",
                "step": len(execution.state.execution_path) + 1,
                "payload": {"id": self.tasks[node], "name": node, "error": error},
            },
        )

    def checkpoint(self, execution, checkpoint_id, state, name):
        current = self.service.store.get("execution", execution.id)
        self.service.record_checkpoint(
            execution.id,
            self.ctrl,
            self.tracker,
            checkpoint_id,
            state.workflow_variables,
            [state.current_node_id] if state.current_node_id else [],
            len(state.execution_path),
            parent_id=current.get("checkpointId"),
        )
        # The source execution path includes inherited nodes after a fork.
        cp = self.service.store.get("checkpoint", execution.id + ":" + checkpoint_id)
        cp.update(
            completedNodes=list(dict.fromkeys(state.execution_path)),
            lastNode=state.execution_path[-1] if state.execution_path else None,
            name=name,
        )
        self.service.store.put("checkpoint", cp)

    def work(self, e, graph, action, payload, operation):
        service, ctrl, uow = self.service, self.ctrl, self.uow
        execution = ctrl.get_status(e["id"])
        if not ctrl._activate_execution_session(execution):
            raise RuntimeError("Could not activate native execution")
        first = True

        def control(current):
            nonlocal first
            latest = service.store.get("execution", current.id)
            points = list(latest["breakpoints"])
            if first and action == "resume":
                points = [p for p in points if p != current.state.current_node_id]
            current.breakpoints = points
            first = False
            return latest.get("control")

        ctrl.observe_local_execution(
            event=self.event, checkpoint=self.checkpoint, control=control
        )
        if action in {"run", "resume"}:
            service.update(e["id"], status="running", error=None)
            service.emit(e["id"], "execution", action)
            execution = (
                ctrl.run(e["id"], graph)
                if action == "run"
                else ctrl.resume(e["id"], payload.get("state"), graph=graph)
            )
        elif action == "rollback":
            cp = service.store.get(
                "checkpoint", e["id"] + ":" + payload["checkpointId"]
            )
            # Validate even an empty file commit before moving the adapter.
            for f in self.tracker.get_tracked_files(cp["fileCommitId"]):
                self.tracker.read_file(f)
            execution = ctrl.rollback(e["id"], cp["checkpointId"])
            if not cp["state"].get("_output_files"):
                self.tracker.restore_from_checkpoint(cp["checkpointId"])
            service.update(
                e["id"],
                checkpointId=cp["checkpointId"],
                nextNodes=cp["nextNodes"],
                activeRunIds=[r["id"] for r in cp["nodeRuns"]],
                attempts=e.get("attempts", [])
                + [
                    dict(
                        id=uid(),
                        kind="rollback",
                        checkpointId=cp["checkpointId"],
                        createdAt=now(),
                    )
                ],
            )
        elif action == "fork":
            child_execution = ctrl.fork(
                e["id"], payload["checkpointId"], payload.get("state")
            )
            workspace = service.root / "workspaces" / child_execution.id
            workspace.mkdir(parents=True)
            child = copy.deepcopy(e)
            child.update(
                id=child_execution.id,
                parentId=e["id"],
                fromCheckpointId=payload["checkpointId"],
                runtime=service.runtime(workspace),
                status="paused",
                createdAt=now(),
                checkpointId=None,
                pendingOperation=None,
                control=None,
                controlOperation=None,
                nodeRuns=[],
                activeRunIds=[],
                activeNodes=[],
                error=None,
                elapsedMs=0,
                batchId=None,
                attempts=[
                    dict(
                        id=uid(),
                        kind="fork",
                        checkpointId=payload["checkpointId"],
                        createdAt=now(),
                    )
                ],
            )
            with WorkspaceTracker(
                e["storeDir"], child["runtime"]["output"]
            ) as child_tracker:
                child_tracker.restore_from_checkpoint(payload["checkpointId"])
                service.store.put("execution", child)
                ctrl._activate_execution_session(child_execution)
                self.tracker = child_tracker
                ctrl._output_dir = child["runtime"]["output"]
                ctrl._create_checkpoint(
                    child_execution,
                    child_execution.state.current_node_id or "__end__",
                    "Fork continuation",
                )
                ctrl._prepare_node_resume_claim_token(child_execution, refresh=True)
                uow.executions.update(child_execution)
                uow.commit()
            operation["resultExecutionId"] = child_execution.id
        elif action in {"edit", "checkpoint"}:
            execution.state.workflow_variables.update(payload.get("state", {}))
            ctrl._create_checkpoint(
                execution,
                execution.state.current_node_id or "__end__",
                "State edit" if action == "edit" else "Manual checkpoint",
            )
            ctrl._prepare_node_resume_claim_token(execution, refresh=True)
            uow.executions.update(execution)
            uow.commit()
        elif action == "breakpoints":
            points = payload.get("nodes", [])
            if set(points) - {n["id"] for n in e["graph"]["nodes"]}:
                raise ValueError("Unknown node")
            execution.breakpoints = points
            uow.executions.update(execution)
            uow.commit()
            service.update(e["id"], breakpoints=points)
        elif action == "stop":
            execution = ctrl.stop(e["id"])
        return execution
