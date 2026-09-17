"""Local, durable WTB console. Graph execution and recovery remain in WTB controllers."""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import platform
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import cloudpickle

from wtb.application.factories import ExecutionControllerFactory
from wtb.application.services.project_service import WorkflowConversionService
from wtb.config import WTBConfig
from wtb.domain.interfaces.file_tracking import FileRestoreResult
from wtb.domain.models.workflow import ExecutionStatus
from wtb.infrastructure.database.console_store import ConsoleStore
from wtb.infrastructure.file_tracking.sqlite_service import SqliteFileTrackingService


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return str(uuid.uuid4())


class Conflict(ValueError):
    pass


class WorkspaceTracker(SqliteFileTrackingService):
    """Restore CAS bytes only into this execution, including nested relative paths."""
    def __init__(self, root, output):
        super().__init__(Path(root))
        self.output = Path(output).resolve()

    def read_file(self, file):
        digest = file.file_hash
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("Invalid CAS hash")
        path = self._blob_dir / digest[:2] / digest[2:]
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("CAS content hash mismatch")
        return content

    @staticmethod
    def relative(file):
        parts = Path(file.file_path).parts
        if "outputs" not in parts:
            raise ValueError("File is outside managed outputs")
        return Path(*parts[parts.index("outputs") + 1:])

    def restore_from_checkpoint(self, checkpoint_id):
        commit = self.get_commit_for_checkpoint(checkpoint_id)
        if not commit:
            raise ValueError("Checkpoint has no file commit")
        files = self.get_tracked_files(commit)
        prepared = []
        for file in files:
            target = (self.output / self.relative(file)).resolve()
            target.relative_to(self.output)
            prepared.append((target, self.read_file(file)))
        # Validate every blob before changing any output.
        self.output.mkdir(parents=True, exist_ok=True)
        existing = {p.resolve(): p.read_bytes() for p in self.output.rglob("*") if p.is_file()}
        try:
            for target, content in prepared:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            keep = {p for p, _ in prepared}
            for path in existing:
                if path not in keep:
                    path.unlink()
        except BaseException:
            for target, _ in prepared:
                if target not in existing and target.exists():
                    target.unlink()
            for path, content in existing.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            raise
        return FileRestoreResult(commit_id=commit, files_restored=len(prepared), total_size_bytes=sum(len(c) for _, c in prepared), restored_paths=[str(p) for p, _ in prepared], success=True)


class ConsoleService:
    def __init__(self, data_dir, config_path=None):
        self.root = Path(data_dir).resolve()
        self.store = ConsoleStore(self.root / "console.db")
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="wtb-console")
        self.closed = False
        self.notify = lambda event: None
        entries = [{"factory": "wtb.testing.console_project:create_project", "initial_state": {"text": "WTB", "repeat": 2, "delay": 0.2}}]
        if config_path:
            entries = json.loads(Path(config_path).read_text(encoding="utf-8"))["projects"]
        self.projects = {}
        for entry in entries:
            module, name = entry["factory"].split(":", 1)
            project = getattr(importlib.import_module(module), name)()
            if project.name in self.projects:
                raise ValueError("Duplicate project name")
            self.projects[project.name] = (project, entry.get("initial_state", {}))
        for e in self.store.list("execution"):
            if e["status"] in {"queued", "running", "pausing", "stopping"} or e.get("pendingOperation"):
                e.update(status="failed", error="服务在执行期间退出；可从最后有效检查点回退", activeNodes=[], pendingOperation=None)
                self.store.put("execution", e)
        for op in self.store.list("operation"):
            if op["status"] in {"queued", "running"}:
                op.update(status="failed", error="服务重启中断了操作")
                self.store.put("operation", op)

    def emit(self, execution_id, kind, message):
        event = dict(id=uid(), executionId=execution_id, kind=kind, message=message, time=now())
        event["seq"] = self.store.event(event)
        self.notify(event)

    def update(self, id, **fields):
        with self.lock:
            e = self.store.get("execution", id)
            e.update(fields)
            self.store.put("execution", e)
        return e

    def catalog(self):
        projects, variants, components = [], [], []
        for name, (p, initial) in self.projects.items():
            projects.append(dict(id=name, name=name, description=p.description, initialState=initial, nodeVariants=p.list_variants()))
            choices = [("default", None, {})]
            choices += [(n, n, {}) for n in p.list_workflow_variants()]
            choices += [(f"{node}:{v}", None, {node: v}) for node, vs in p.list_variants().items() for v in vs]
            for label, workflow_variant, node_variants in choices:
                graph = p.build_graph(node_variants, workflow_variant)
                compiled = graph if hasattr(graph, "get_graph") else graph.compile()
                g = compiled.get_graph()
                nodes = []
                for i, n in enumerate(g.nodes.values()):
                    component_id = f"{name}/{label}/{n.id}"
                    nodes.append(dict(id=n.id, componentId=component_id, implementation=node_variants.get(n.id, n.name), x=(i % 4)*240, y=(i//4)*170))
                    components.append(dict(id=component_id, name=n.name, description="已注册图节点", version=None, tags=[]))
                variants.append(dict(id=f"{name}/{label}", projectId=name, name=label, description=p.description, nodes=nodes,
                                     edges=[dict(id=str(i), source=e.source, target=e.target, conditional=e.conditional, label=str(e.data or "")) for i, e in enumerate(g.edges)],
                                     workflowVariant=workflow_variant, nodeVariants=node_variants))
        return dict(projects=projects, variants=variants, components=components, capabilities={"local": True, "ray": False, "venv": False, "evaluation": False, "manualCheckpoint": True}, unavailable={"ray": "本阶段未接入 Ray", "venv": "本阶段使用服务进程解释器", "evaluation": "未配置评估器"})

    @contextmanager
    def context(self, e):
        config = WTBConfig.for_development(e["storeDir"])
        config.log_sql = False
        tracker = WorkspaceTracker(e["storeDir"], e["runtime"]["output"])
        managed = ExecutionControllerFactory(config).create_isolated(file_tracking_service=tracker, output_dir=e["runtime"]["output"])
        try:
            with managed:
                managed.controller.set_deferred_commit(False)
                yield managed.controller, managed.uow, tracker
        finally:
            tracker.close()

    def start(self, variant_id, state, breakpoints=None, node_variants=None, batch_id=None):
        if self.closed:
            raise Conflict("服务正在关闭")
        catalog = self.catalog()
        variant = next((v for v in catalog["variants"] if v["id"] == variant_id), None)
        if not variant:
            raise ValueError("Unknown variant")
        p, _ = self.projects[variant["projectId"]]
        overrides = dict(variant["nodeVariants"])
        overrides.update(node_variants or {})
        for node, value in overrides.items():
            if value not in p.list_variants().get(node, []):
                raise ValueError(f"Unknown implementation {node}:{value}")
        graph = p.build_graph(overrides, variant["workflowVariant"])
        known = {n["id"] for n in variant["nodes"]}
        if set(breakpoints or []) - known:
            raise ValueError("Unknown breakpoint node")
        id = uid()
        workspace = self.root / "workspaces" / id
        workspace.mkdir(parents=True)
        graph = getattr(graph, "builder", graph)
        (workspace / "graph.pkl").write_bytes(cloudpickle.dumps(graph))
        variant = copy.deepcopy(variant)
        variant["nodeVariants"] = overrides
        for node in variant["nodes"]:
            node["implementation"] = overrides.get(node["id"], node["implementation"])
        e = dict(id=id, variantId=variant_id, graph=variant, projectId=variant["projectId"], status="queued", createdAt=now(), state=state,
                 nodeRuns=[], activeRunIds=[], activeNodes=[], nextNodes=[], checkpointId=None, breakpoints=breakpoints or [], elapsedMs=0,
                 storeDir=str(workspace), graphPath=str(workspace / "graph.pkl"), batchId=batch_id, parentId=None, fromCheckpointId=None,
                 runtime=self.runtime(workspace), pendingOperation=None, control=None)
        with self.context(e) as (ctrl, uow, _):
            workflow = WorkflowConversionService().convert_from_project(p)
            uow.workflows.add(workflow)
            ctrl.create_execution(workflow, state, breakpoints=breakpoints or [], execution_id=id)
        self.store.put("execution", e)
        return self.submit(id, "run", {})

    def runtime(self, workspace):
        return dict(mode="local", host=socket.gethostname(), pid=os.getpid(), workspace=str(workspace), output=str(workspace / "outputs"),
                    interpreter=__import__("sys").executable, pythonVersion=platform.python_version(), environment="服务进程环境", pathLocation="后端主机")

    def submit(self, id, action, payload):
        with self.lock:
            e = self.store.get("execution", id)
            if e.get("pendingOperation"):
                raise Conflict("该执行已有操作处理中")
            op = dict(id=uid(), executionId=id, type=action, status="queued", createdAt=now())
            self.store.put("operation", op)
            self.update(id, pendingOperation=op["id"])
            self.pool.submit(self.work, id, op["id"], action, payload)
        return dict(executionId=id, operationId=op["id"])

    def command(self, id, action, payload):
        with self.lock:
            e = self.store.get("execution", id)
            if action in {"pause", "stop"} and e["status"] in {"running", "queued", "pausing"}:
                if e.get("control"):
                    raise Conflict("控制请求正在处理中")
                op = dict(id=uid(), executionId=id, type=action, status="queued", createdAt=now())
                self.store.put("operation", op)
                self.update(id, control=action, controlOperation=op["id"], status="pausing" if action == "pause" else "stopping")
                self.emit(id, "control", action + " requested")
                return dict(executionId=id, operationId=op["id"])
            if e["status"] in {"running", "queued", "pausing", "stopping"} or e.get("pendingOperation"):
                raise Conflict("请等待执行到达暂停边界")
            if action in {"resume", "edit", "checkpoint", "breakpoints"} and e["status"] != "paused":
                raise Conflict("该操作要求执行已暂停")
            if action in {"rollback", "fork"}:
                if not payload.get("checkpointId"):
                    raise ValueError("请选择检查点")
                self.store.get("checkpoint", id + ":" + payload["checkpointId"])
            if action not in {"resume", "stop", "rollback", "fork", "edit", "checkpoint", "breakpoints"}:
                raise ValueError("Unsupported operation")
            return self.submit(id, action, payload)

    def boundary(self, id, ctrl, tracker, snapshot):
        cp_id = snapshot.config["configurable"]["checkpoint_id"]
        state = snapshot.values or {}
        paths = ctrl._write_output_files(state.get("_output_files", {}))
        # Even an empty checkpoint receives a real commit, so later files can be removed on restore.
        tracked = tracker.track_files(paths, message=f"Boundary {cp_id}")
        tracker.link_to_checkpoint(cp_id, tracked.commit_id)
        current = self.store.get("execution", id)
        cp = dict(id=id + ":" + cp_id, checkpointId=cp_id, executionId=id, createdAt=snapshot.created_at or now(), state=state,
                  nextNodes=list(snapshot.next), nodeRuns=[r for r in current["nodeRuns"] if r["id"] in current.get("activeRunIds", [n["id"] for n in current["nodeRuns"]])], step=(snapshot.metadata or {}).get("step", 0),
                  fileCommitId=tracked.commit_id, parentCheckpointId=(snapshot.parent_config or {}).get("configurable", {}).get("checkpoint_id"))
        self.store.put("checkpoint", cp)
        self.update(id, checkpointId=cp_id, state=state, nextNodes=list(snapshot.next), activeNodes=[])
        self.emit(id, "checkpoint", f"Checkpoint {cp_id}")

    def node_event(self, id, event):
        if event["type"] not in {"task", "task_result"}:
            return
        p = event["payload"]
        if p["name"].startswith("__"):
            return
        with self.lock:
            e = self.store.get("execution", id)
            runs = e["nodeRuns"]
            if event["type"] == "task":
                runs.append(dict(id=uid(), taskId=p["id"], nodeId=p["name"], status="running", startedAt=now(), elapsedMs=0, step=event["step"]))
                e.setdefault("activeRunIds", []).append(runs[-1]["id"])
            else:
                run = next((r for r in reversed(runs) if r["taskId"] == p["id"] and r["status"] == "running"), None)
                if run:
                    run.update(status="failed" if p.get("error") else "completed", error=p.get("error"), elapsedMs=int((datetime.now(timezone.utc)-datetime.fromisoformat(run["startedAt"])).total_seconds()*1000))
            self.update(id, nodeRuns=runs, activeRunIds=e.get("activeRunIds", []), activeNodes=[r["nodeId"] for r in runs if r["status"] == "running"])
        self.emit(id, "node", f"{p['name']} {event['type']}")

    def work(self, id, op_id, action, payload):
        started = time.monotonic()
        op = self.store.get("operation", op_id)
        op["status"] = "running"
        self.store.put("operation", op)
        try:
            e = self.store.get("execution", id)
            with self.context(e) as (ctrl, uow, tracker):
                adapter = ctrl._state_adapter
                graph = cloudpickle.loads(Path(e["graphPath"]).read_bytes())
                adapter.set_workflow_graph(graph)
                execution = ctrl.get_status(id)
                ctrl._activate_execution_session(execution)
                adapter.configure_runtime(event=lambda event: self.node_event(id, event),
                    boundary=lambda snap: self.boundary(id, ctrl, tracker, snap),
                    control=lambda: self.store.get("execution", id).get("control"),
                    breakpoints=lambda: self.store.get("execution", id)["breakpoints"])
                if action in {"run", "resume"}:
                    if e.get("control") == "stop":
                        execution = ctrl.stop(id)
                    else:
                        self.update(id, status="running", error=None)
                        self.emit(id, "execution", action)
                        execution = ctrl.run(id, graph) if action == "run" else ctrl.resume(id, payload.get("state"))
                elif action == "rollback":
                    # WTB validates the checkpoint and restores the state; tracker confines files to this workspace.
                    execution = ctrl.rollback(id, payload["checkpointId"])
                    cp = self.store.get("checkpoint", id + ":" + payload["checkpointId"])
                    if not cp["state"].get("_output_files"):
                        tracker.restore_from_checkpoint(payload["checkpointId"])
                    self.update(id, checkpointId=cp["checkpointId"], state=cp["state"], nextNodes=cp["nextNodes"], activeRunIds=[r["id"] for r in cp["nodeRuns"]])
                elif action == "fork":
                    fork = ctrl.fork(id, payload["checkpointId"], payload.get("state"))
                    workspace = self.root / "workspaces" / fork.id
                    workspace.mkdir(parents=True)
                    child = copy.deepcopy(e)
                    child.update(id=fork.id, parentId=id, fromCheckpointId=payload["checkpointId"], runtime=self.runtime(workspace), status="paused", createdAt=now(),
                                 pendingOperation=None, control=None, nodeRuns=[], activeRunIds=[], activeNodes=[], error=None, elapsedMs=0, batchId=None)
                    child_tracker = WorkspaceTracker(e["storeDir"], child["runtime"]["output"])
                    try:
                        child_tracker.restore_from_checkpoint(payload["checkpointId"])
                        fork.status = ExecutionStatus.PAUSED
                        uow.executions.update(fork)
                        uow.commit()
                        self.store.put("execution", child)
                        ctrl._activate_execution_session(fork)
                        saved_output = ctrl._output_dir
                        ctrl._output_dir = child["runtime"]["output"]
                        self.boundary(fork.id, ctrl, child_tracker, adapter.get_compiled_graph().get_state(adapter.get_config()))
                        ctrl._output_dir = saved_output
                    finally:
                        child_tracker.close()
                    op["resultExecutionId"] = fork.id
                elif action in {"edit", "checkpoint"}:
                    config = adapter.get_config(checkpoint_id=e.get("checkpointId"))
                    config["configurable"]["checkpoint_ns"] = ""
                    new_config = adapter.get_compiled_graph().update_state(config, payload.get("state", {}))
                    snap = adapter.get_compiled_graph().get_state(new_config)
                    self.boundary(id, ctrl, tracker, snap)
                    execution.state.workflow_variables = dict(snap.values)
                    execution.checkpoint_id = snap.config["configurable"]["checkpoint_id"]
                    execution.metadata["resume_checkpoint_id"] = execution.checkpoint_id
                    uow.executions.update(execution)
                    uow.commit()
                elif action == "breakpoints":
                    points = payload.get("nodes", [])
                    if set(points) - {n["id"] for n in e["graph"]["nodes"]}:
                        raise ValueError("Unknown node")
                    execution.breakpoints = points
                    uow.executions.update(execution)
                    uow.commit()
                    self.update(id, breakpoints=points)
                elif action == "stop":
                    execution = ctrl.stop(id)
                if action != "fork":
                    self.update(id, status=execution.status.value, state=execution.state.workflow_variables, error=execution.error_message,
                                elapsedMs=e["elapsedMs"] + int((time.monotonic()-started)*1000), activeNodes=[])
                    if execution.status == ExecutionStatus.FAILED:
                        raise RuntimeError(execution.error_message or "Execution failed")
            op["status"] = "completed"
            self.emit(id, "control", action + " completed")
        except Exception as error:
            op.update(status="failed", error=str(error))
            fields = {"operationError": str(error)}
            if action in {"run", "resume"}:
                fields.update(status="failed", error=str(error), activeNodes=[])
            self.update(id, **fields)
            self.emit(id, "error", str(error))
        finally:
            op["finishedAt"] = now()
            e = self.store.get("execution", id)
            if e.get("controlOperation"):
                control = self.store.get("operation", e["controlOperation"])
                control.update(status="completed" if e["status"] in {"paused", "cancelled"} else "failed", finishedAt=now())
                self.store.put("operation", control)
            self.update(id, pendingOperation=None, control=None, controlOperation=None)
            # Completion must be observable only after the execution is ready
            # to accept its next command.
            self.store.put("operation", op)
            self.emit(id, "execution", "updated")

    def checkpoints(self, id, limit=50, offset=0):
        self.store.get("execution", id)
        items = [c for c in self.store.list("checkpoint") if c["executionId"] == id]
        return dict(items=[{k: v for k, v in c.items() if k not in {"state", "nodeRuns"}} for c in items[offset:offset+limit]], total=len(items))

    def files(self, id, checkpoint_id, path=None):
        e = self.store.get("execution", id)
        cp = self.store.get("checkpoint", id + ":" + checkpoint_id)
        with WorkspaceTracker(e["storeDir"], e["runtime"]["output"]) as tracker:
            files = tracker.get_tracked_files(cp["fileCommitId"])
            if path is None:
                return [dict(path=str(tracker.relative(f)).replace("\\", "/"), hash=f.file_hash, size=f.size_bytes) for f in files]
            file = next((f for f in files if str(tracker.relative(f)).replace("\\", "/") == path), None)
            if not file:
                raise KeyError(path)
            if file.size_bytes > 256*1024:
                return dict(content=None, reason="文件超过 256 KiB 预览限制")
            raw = tracker.read_file(file)
            try:
                content = raw.decode("utf-8")
                if "\x00" in content:
                    raise UnicodeError()
                return dict(content=content, reason=None)
            except UnicodeError:
                return dict(content=None, reason="二进制文件，仅展示元数据")

    def batch(self, variants, inputs):
        if not variants or not inputs or len(variants)*len(inputs) > 40:
            raise ValueError("批量任务需要 1–40 个组合")
        known = {v["id"] for v in self.catalog()["variants"]}
        if set(variants) - known or any(not isinstance(s, dict) for s in inputs):
            raise ValueError("Invalid variants or initial states")
        batch = dict(id=uid(), createdAt=now(), executionIds=[])
        self.store.put("batch", batch)
        for variant in variants:
            for state in inputs:
                result = self.start(variant, state, batch_id=batch["id"])
                batch["executionIds"].append(result["executionId"])
                self.store.put("batch", batch)
        return batch

    def system(self):
        return dict(storage=str(self.root), database=str(self.store.path), maxWorkers=2,
                    cache=None, outbox=None, integrity=self.store.list("integrity"),
                    unavailable="当前控制台不启用 Outbox 后台处理器；缓存命中率未采集")

    def check_integrity(self):
        findings = []
        for cp in self.store.list("checkpoint"):
            try:
                e = self.store.get("execution", cp["executionId"])
                with WorkspaceTracker(e["storeDir"], e["runtime"]["output"]) as tracker:
                    if tracker.get_commit_for_checkpoint(cp["checkpointId"]) != cp["fileCommitId"]:
                        raise ValueError("Checkpoint file link mismatch")
                    for file in tracker.get_tracked_files(cp["fileCommitId"]):
                        tracker.read_file(file)
            except Exception as error:
                findings.append(f"{cp['id']}: {error}")
        report = dict(id="latest", checkedAt=now(), findings=findings, status="issues" if findings else "healthy", scope="控制台检查点文件链接与 CAS 内容哈希")
        self.store.put("integrity", report)
        return report

    def close(self):
        self.closed = True
        for e in self.store.list("execution"):
            if e.get("pendingOperation"):
                self.update(e["id"], control="pause")
        self.pool.shutdown(wait=True)
