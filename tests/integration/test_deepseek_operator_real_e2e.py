"""Real WTB -> Python SDK -> Loader/stdio -> Harness operator gates.

These tests deliberately use a deterministic local OpenAI-compatible HTTP
endpoint, but never replace the Harness, Python SDK, JSON-RPC server, Loader,
or persistence provider with an inline fake.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

from wtb.domain.models.outbox import OutboxEventType
from wtb.domain.runtime_identity import canonical_json_sha256
from wtb.domain.models.workflow import ExecutionStatus
from wtb.infrastructure.database.unit_of_work import SQLAlchemyUnitOfWork
from wtb.infrastructure.runtimes.deepseek_harness import (
    DeepSeekHarnessRuntime,
    DeepSeekHarnessWorkflow,
)
from wtb.domain.interfaces.workflow_runtime import RuntimeBindingMismatchError
from wtb.domain.interfaces.workflow_runtime import RuntimeOutcomeUnknownError
from wtb.sdk import WTBTestBench
from wtb.sdk.workflow_project import WorkflowProject


_REQUIRE_ENV = "WTB_REQUIRE_DSH_REAL_E2E"
_NODE_ENV = "WTB_DSH_NODE"


class _CompletionHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    response_text = "real Loader and Python SDK terminal receipt"

    def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP hook
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("authorization"),
                "body": body,
            }
        )
        frames = [
            {
                "choices": [
                    {
                        "delta": {
                            "role": "assistant",
                            "content": type(self).response_text,
                        }
                    }
                ]
            },
            {
                "choices": [
                    {"delta": {"content": ""}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 7, "completion_tokens": 9},
            },
        ]
        payload = "".join(
            f"data: {json.dumps(frame, separators=(',', ':'))}\n\n"
            for frame in frames
        ) + "data: [DONE]\n\n"
        try:
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(payload.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(payload.encode("utf-8"))
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _BashRoundTripHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    effect_file: Path | None = None
    response_text = "real bash tool round trip completed"
    hold_final = False
    hold_tool_call = False
    first_request_seen = threading.Event()
    release_tool_call = threading.Event()
    second_request_seen = threading.Event()
    release_final = threading.Event()

    def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP hook
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).requests.append(body)
        if len(type(self).requests) == 1:
            type(self).first_request_seen.set()
            if type(self).hold_tool_call:
                type(self).release_tool_call.wait(timeout=30)
            effect_file = type(self).effect_file
            assert effect_file is not None
            target = effect_file.as_posix().replace("'", "'\\''")
            arguments = json.dumps(
                {
                    "command": f"printf 'wtb-effect\\n' >> '{target}'",
                    "description": "Write one durable WTB tool-effect marker.",
                },
                separators=(",", ":"),
            )
            frames = [
                {
                    "choices": [
                        {
                            "delta": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "wtb-real-bash-call",
                                        "type": "function",
                                        "function": {
                                            "name": "bash",
                                            "arguments": arguments,
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {"delta": {}, "finish_reason": "tool_calls"}
                    ]
                },
            ]
        else:
            type(self).second_request_seen.set()
            if type(self).hold_final:
                type(self).release_final.wait(timeout=30)
            frames = [
                {
                    "choices": [
                        {
                            "delta": {
                                "role": "assistant",
                                "content": type(self).response_text,
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {"delta": {"content": ""}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 5},
                },
            ]
        payload = "".join(
            f"data: {json.dumps(frame, separators=(',', ':'))}\n\n"
            for frame in frames
        ) + "data: [DONE]\n\n"
        try:
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(payload.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(payload.encode("utf-8"))
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _BlockingCompletionHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    request_seen = threading.Event()
    release = threading.Event()

    def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP hook
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).requests.append(body)
        type(self).request_seen.set()
        type(self).release.wait(timeout=30)
        payload = (
            'data: {"choices":[{"delta":{"role":"assistant",'
            '"content":"late"}}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n"
        )
        try:
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(payload.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(payload.encode("utf-8"))
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def _completion_server() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    _CompletionHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CompletionHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield (
            f"http://127.0.0.1:{server.server_address[1]}",
            _CompletionHandler.requests,
        )
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@contextmanager
def _bash_server(
    effect_file: Path,
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    _BashRoundTripHandler.requests = []
    _BashRoundTripHandler.effect_file = effect_file
    _BashRoundTripHandler.hold_final = False
    _BashRoundTripHandler.hold_tool_call = False
    _BashRoundTripHandler.first_request_seen = threading.Event()
    _BashRoundTripHandler.release_tool_call = threading.Event()
    _BashRoundTripHandler.second_request_seen = threading.Event()
    _BashRoundTripHandler.release_final = threading.Event()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BashRoundTripHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield (
            f"http://127.0.0.1:{server.server_address[1]}",
            _BashRoundTripHandler.requests,
        )
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@contextmanager
def _held_bash_server(
    effect_file: Path,
) -> Iterator[
    tuple[
        str,
        list[dict[str, Any]],
        threading.Event,
        threading.Event,
    ]
]:
    _BashRoundTripHandler.requests = []
    _BashRoundTripHandler.effect_file = effect_file
    _BashRoundTripHandler.hold_final = True
    _BashRoundTripHandler.hold_tool_call = False
    _BashRoundTripHandler.first_request_seen = threading.Event()
    _BashRoundTripHandler.release_tool_call = threading.Event()
    _BashRoundTripHandler.second_request_seen = threading.Event()
    _BashRoundTripHandler.release_final = threading.Event()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BashRoundTripHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield (
            f"http://127.0.0.1:{server.server_address[1]}",
            _BashRoundTripHandler.requests,
            _BashRoundTripHandler.second_request_seen,
            _BashRoundTripHandler.release_final,
        )
    finally:
        _BashRoundTripHandler.release_final.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@contextmanager
def _held_tool_call_server(
    effect_file: Path,
) -> Iterator[tuple[str, list[dict[str, Any]], threading.Event]]:
    _BashRoundTripHandler.requests = []
    _BashRoundTripHandler.effect_file = effect_file
    _BashRoundTripHandler.hold_final = False
    _BashRoundTripHandler.hold_tool_call = True
    _BashRoundTripHandler.first_request_seen = threading.Event()
    _BashRoundTripHandler.release_tool_call = threading.Event()
    _BashRoundTripHandler.second_request_seen = threading.Event()
    _BashRoundTripHandler.release_final = threading.Event()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BashRoundTripHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield (
            f"http://127.0.0.1:{server.server_address[1]}",
            _BashRoundTripHandler.requests,
            _BashRoundTripHandler.release_tool_call,
        )
    finally:
        _BashRoundTripHandler.release_tool_call.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@contextmanager
def _blocking_completion_server() -> Iterator[
    tuple[str, list[dict[str, Any]], threading.Event]
]:
    _BlockingCompletionHandler.requests = []
    _BlockingCompletionHandler.request_seen = threading.Event()
    _BlockingCompletionHandler.release = threading.Event()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BlockingCompletionHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield (
            f"http://127.0.0.1:{server.server_address[1]}",
            _BlockingCompletionHandler.requests,
            _BlockingCompletionHandler.request_seen,
        )
    finally:
        _BlockingCompletionHandler.release.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def _dsh_checkout() -> Path:
    explicit = os.environ.get("WTB_DSH_SOURCE_PATH")
    checkout = (
        Path(explicit).expanduser().resolve()
        if explicit
        else Path(__file__).resolve().parents[3] / "deepseek-harness"
    )
    required = (
        checkout / "python" / "sdk" / "src" / "deepseek_harness" / "__init__.py",
        checkout / "packages" / "examples" / "jsonrpc-demo" / "lib" / "bin.js",
        checkout / "examples" / "jsonrpc-agent" / "cordis.yml",
    )
    missing = [str(path) for path in required if not path.is_file()]
    node = _node_binary()
    if not missing and node is not None:
        return checkout
    detail = f"missing={missing!r}, node={node!r}"
    if os.environ.get(_REQUIRE_ENV) == "1":
        pytest.fail(f"real DeepSeek operator E2E prerequisites absent: {detail}")
    pytest.skip(f"real DeepSeek operator E2E prerequisites absent: {detail}")


def _node_binary() -> str | None:
    explicit = os.environ.get(_NODE_ENV)
    if explicit:
        candidates = [Path(explicit)]
    else:
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        candidates = sorted(
            local.glob("OpenAI/Codex/runtimes/cua_node/*/bin/node.exe"),
            reverse=True,
        )
        resolved = shutil.which("node")
        if resolved is not None:
            candidates.append(Path(resolved))
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            version = subprocess.run(
                (str(candidate), "--version"),
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip().removeprefix("v")
            major, minor, *_ = (int(part) for part in version.split("."))
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        if (major, minor) >= (22, 19):
            return str(candidate)
    return None


def _runtime(
    *,
    checkout: Path,
    base_url: str,
    session_root: Path,
    workspace: Path,
    namespace: str = "real-e2e",
    cordis: Path | None = None,
    runtime_wrapper: Path | None = None,
    model: str = "wtb-real-e2e-model",
    api_key: str = "wtb-real-e2e-no-secret",
    request_timeout_seconds: float = 30.0,
) -> DeepSeekHarnessRuntime:
    config = cordis or checkout / "examples" / "jsonrpc-agent" / "cordis.yml"
    entry = checkout / "packages" / "examples" / "jsonrpc-demo" / "lib" / "bin.js"
    child_path = os.environ.get("PATH", "")
    git_bash = Path("C:/Program Files/Git/bin")
    if git_bash.is_dir():
        child_path = os.pathsep.join((str(git_bash), child_path))
    inherited_environment = {
        key: hashlib.sha256(value.encode("utf-8")).hexdigest()
        for key, value in sorted(os.environ.items())
    }
    deployment_fingerprint = canonical_json_sha256(
        {
            "authority": "wtb-real-e2e-controlled-host-environment-v1",
            "inherited_environment": inherited_environment,
        }
    )
    runtime_command = (
        (_node_binary() or "node", str(entry), str(config))
        if runtime_wrapper is None
        else (
            _node_binary() or "node",
            str(runtime_wrapper),
            str(entry),
            str(config),
        )
    )
    return DeepSeekHarnessRuntime(
        sdk_source_path=checkout,
        runtime_command=runtime_command,
        session_namespace=namespace,
        runtime_config_sha256=deployment_fingerprint,
        harness_options={
            "provider": "deepseek-official",
            "model": model,
            "cwd": str(workspace),
            "runtime_cwd": str(checkout),
            "session_root": str(session_root),
            "cordis": str(config),
            "api_key": api_key,
            "base_url": base_url,
            "request_timeout_seconds": request_timeout_seconds,
            "shutdown_timeout_seconds": 3.0,
            "env": {
                "PATH": child_path,
                "DSH_SNAPSHOT": "1",
                "DSH_SYSTEM_PROMPT": "WTB deterministic operator test",
            },
        },
    )


def _sqlite_cordis(checkout: Path, target: Path, database: Path) -> Path:
    source = checkout / "examples" / "jsonrpc-agent" / "cordis.yml"
    text = source.read_text(encoding="utf-8")
    sqlite_path = database.as_posix().replace("'", "''")
    replacement = (
        "- id: sessions\n"
        "  name: '@deepseek-ai/dsh-session-persistence-sqlite'\n"
        "  config:\n"
        f"    path: '{sqlite_path}'\n"
        "    journalMode: wal\n\n"
    )
    configured, count = re.subn(
        r"(?ms)^- id: sessions\n.*?(?=^- id: session-checkpoints)",
        replacement,
        text,
    )
    assert count == 1
    target.write_text(configured, encoding="utf-8")
    return target


def _project(name: str) -> WorkflowProject:
    return WorkflowProject(
        name=name,
        graph_factory=lambda: DeepSeekHarnessWorkflow(),
    )


def _assert_dsh_receipt(
    result: Any,
    *,
    execution_id: str,
    answer: str,
) -> None:
    assert result.state.workflow_variables["answer"] == answer
    receipt = result.state.workflow_variables["_dsh"]
    assert receipt["protocol"] == "deepseek-harness-sdk-jsonrpc"
    assert receipt["execution_id"] == execution_id
    assert receipt["session_id"].startswith("wtb-real-e2e-")
    assert receipt["attempt_id"]
    assert receipt["attempt_status"] == "terminal"
    assert receipt["finish_reason"] == "completed"
    binding = receipt["binding"]
    assert binding["exact_session_id"] == receipt["session_id"]
    assert binding["attempt_id"] == receipt["attempt_id"]
    assert binding["input_sha256"] == receipt["input_sha256"]
    assert binding["persistence_instance_id"]


@pytest.mark.skipif(
    os.environ.get("WTB_REQUIRE_DSH_LIVE_E2E") != "1",
    reason="set WTB_REQUIRE_DSH_LIVE_E2E=1 to call the live DeepSeek endpoint",
)
def test_live_deepseek_endpoint_preserves_wtb_reference_receipts(
    tmp_path: Path,
) -> None:
    """Run the real cloud endpoint without weakening the durable-attempt seam."""
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    model = (
        os.environ.get("DSH_MODEL")
        or os.environ.get("DEEPSEEK_MODEL")
        or os.environ.get("model")
    )
    if not api_key:
        pytest.fail("DEEPSEEK_API_KEY must be non-empty for the live E2E")
    if not model:
        pytest.fail("DSH_MODEL, DEEPSEEK_MODEL, or model must select the live model")

    checkout = _dsh_checkout()
    workspace = tmp_path / "workspace"
    session_root = tmp_path / "dsh-jsonl"
    source_data = tmp_path / "wtb-source"
    target_data = tmp_path / "wtb-target"
    for directory in (workspace, session_root, source_data, target_data):
        directory.mkdir()

    runtime_options = {
        "checkout": checkout,
        "base_url": os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "session_root": session_root,
        "workspace": workspace,
        "namespace": "live-deepseek-e2e",
        "model": model,
        "api_key": api_key,
        "request_timeout_seconds": 180.0,
    }
    source_project = _project("live-deepseek-source")
    source_bench = WTBTestBench.create_with_runtime(
        _runtime(**runtime_options),
        data_dir=str(source_data),
        workflow=DeepSeekHarnessWorkflow(),
    )
    source_bench.attach_project(source_project)
    try:
        source = source_bench.run(
            project=source_project.name,
            initial_state={
                "prompt": "Remember this exact token for the next agent: CAVC-LIVE-7F3A. Reply briefly."
            },
        )
    finally:
        source_bench.close()

    assert source.status is ExecutionStatus.COMPLETED
    source_receipt = source.state.workflow_variables["_dsh"]
    source_session_id = source_receipt["session_id"]
    assert source.state.workflow_variables["answer"]
    assert source_receipt["attempt_status"] == "terminal"
    assert source_receipt["finish_reason"] == "completed"

    target_workflow = DeepSeekHarnessWorkflow(
        reference_key="memory_references",
        memory_mode="S0",
    )
    target_project = WorkflowProject(
        name="live-deepseek-target",
        graph_factory=lambda: target_workflow,
    )
    target_bench = WTBTestBench.create_with_runtime(
        _runtime(**runtime_options),
        data_dir=str(target_data),
        workflow=target_workflow,
    )
    target_bench.attach_project(target_project)
    try:
        target = target_bench.run(
            project=target_project.name,
            initial_state={
                "prompt": "Report the exact token supplied by the captured source session.",
                "memory_references": [
                    {"sessionId": source_session_id, "label": "source"}
                ],
            },
        )
    finally:
        target_bench.close()

    assert target.status is ExecutionStatus.COMPLETED, target.error_message
    assert "CAVC-LIVE-7F3A" in target.state.workflow_variables["answer"]
    target_receipt = target.state.workflow_variables["_dsh"]
    assert target_receipt["attempt_status"] == "terminal"
    assert target_receipt["finish_reason"] == "completed"
    assert target_receipt["memory_mode"] == "S0"
    snapshot = target_receipt["reference_snapshot"]
    assert snapshot["contentSha256"]
    assert snapshot["references"][0]["sessionId"] == source_session_id
    assert snapshot["references"][0]["capturedThroughSeq"] is not None

    receipt_path = os.environ.get("CAVC_LIVE_RECEIPT_PATH")
    if receipt_path:
        Path(receipt_path).write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "provider": "deepseek-official",
                    "model": model,
                    "source": source_receipt,
                    "target": target_receipt,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )


def test_real_wtb_reference_capture_flows_through_loader_and_target_log(
    tmp_path: Path,
) -> None:
    checkout = _dsh_checkout()
    workspace = tmp_path / "workspace"
    session_root = tmp_path / "dsh-jsonl"
    source_data = tmp_path / "wtb-source"
    target_data = tmp_path / "wtb-target"
    for directory in (workspace, session_root, source_data, target_data):
        directory.mkdir()

    with _completion_server() as (base_url, requests):
        source_project = _project("real-reference-source")
        source_bench = WTBTestBench.create_with_runtime(
            _runtime(
                checkout=checkout,
                base_url=base_url,
                session_root=session_root,
                workspace=workspace,
                namespace="reference-e2e",
            ),
            data_dir=str(source_data),
            workflow=DeepSeekHarnessWorkflow(),
        )
        source_bench.attach_project(source_project)
        try:
            source = source_bench.run(
                project=source_project.name,
                initial_state={"prompt": "source fact alpha is durable"},
            )
            source_session_id = source.state.workflow_variables["_dsh"]["session_id"]
        finally:
            source_bench.close()

        target_workflow = DeepSeekHarnessWorkflow(
            reference_key="memory_references",
            memory_mode="S0",
        )
        target_project = WorkflowProject(
            name="real-reference-target",
            graph_factory=lambda: target_workflow,
        )
        target_bench = WTBTestBench.create_with_runtime(
            _runtime(
                checkout=checkout,
                base_url=base_url,
                session_root=session_root,
                workspace=workspace,
                namespace="reference-e2e",
            ),
            data_dir=str(target_data),
            workflow=target_workflow,
        )
        target_bench.attach_project(target_project)
        try:
            target = target_bench.run(
                project=target_project.name,
                initial_state={
                    "prompt": "answer using the captured source",
                    "memory_references": [
                        {"sessionId": source_session_id, "label": "source"}
                    ],
                },
            )
        finally:
            target_bench.close()

    assert target.status is ExecutionStatus.COMPLETED, target.error_message
    receipt = target.state.workflow_variables["_dsh"]
    assert receipt["memory_mode"] == "S0"
    snapshot = receipt["reference_snapshot"]
    assert snapshot["contentSha256"]
    assert snapshot["references"][0]["sessionId"] == source_session_id
    assert snapshot["references"][0]["capturedThroughSeq"] is not None
    assert len(requests) == 2
    target_request = json.dumps(requests[1]["body"], ensure_ascii=False)
    assert "Referenced sessions" in target_request
    assert "source fact alpha is durable" in target_request
    assert "answer using the captured source" in target_request


def test_real_wtb_m0_refreshes_each_tool_step_without_target_accumulation(
    tmp_path: Path,
) -> None:
    checkout = _dsh_checkout()
    workspace = tmp_path / "workspace"
    session_root = tmp_path / "dsh-jsonl"
    source_data = tmp_path / "wtb-source"
    target_data = tmp_path / "wtb-target"
    effect_file = workspace / "tool-effect.log"
    for directory in (workspace, session_root, source_data, target_data):
        directory.mkdir()

    with _completion_server() as (source_url, _source_requests):
        source_project = _project("real-m0-source")
        source_bench = WTBTestBench.create_with_runtime(
            _runtime(
                checkout=checkout,
                base_url=source_url,
                session_root=session_root,
                workspace=workspace,
                namespace="m0-e2e",
            ),
            data_dir=str(source_data),
            workflow=DeepSeekHarnessWorkflow(),
        )
        source_bench.attach_project(source_project)
        try:
            source = source_bench.run(
                project=source_project.name,
                initial_state={"prompt": "M0 source fact is current"},
            )
            source_session_id = source.state.workflow_variables["_dsh"]["session_id"]
        finally:
            source_bench.close()

    workflow = DeepSeekHarnessWorkflow(
        reference_key="memory_references",
        memory_mode="M0",
    )
    project = WorkflowProject(name="real-m0-target", graph_factory=lambda: workflow)
    with _bash_server(effect_file) as (target_url, requests):
        target_bench = WTBTestBench.create_with_runtime(
            _runtime(
                checkout=checkout,
                base_url=target_url,
                session_root=session_root,
                workspace=workspace,
                namespace="m0-e2e",
            ),
            data_dir=str(target_data),
            workflow=workflow,
        )
        target_bench.attach_project(project)
        try:
            target = target_bench.run(
                project=project.name,
                initial_state={
                    "prompt": "Use bash once, then answer from the current source.",
                    "memory_references": [
                        {"sessionId": source_session_id, "label": "source"}
                    ],
                },
            )
        finally:
            target_bench.close()

    assert target.status is ExecutionStatus.COMPLETED
    receipt = target.state.workflow_variables["_dsh"]
    assert receipt["memory_mode"] == "M0"
    captures = receipt["reference_snapshots"]
    assert [(capture["turn"], capture["step"]) for capture in captures] == [
        (1, 1),
        (1, 2),
    ]
    assert receipt["reference_snapshot"]["contentSha256"] == captures[-1][
        "contentSha256"
    ]
    assert len(requests) == 2
    for request in requests:
        serialized = json.dumps(request, ensure_ascii=False)
        assert serialized.count("## Referenced sessions") == 1
        assert "M0 source fact is current" in serialized


def test_real_two_session_w0_manifest_binds_git_attempt_capture_and_schedule(
    tmp_path: Path,
) -> None:
    checkout = _dsh_checkout()
    pytest.importorskip(
        "cavc",
        reason="cross-repository manifest gate requires the CAVC experiment package",
    )
    from cavc.adapters import GitSourceAuthority, WTBTwoSessionPort
    from cavc.runner import W0Coordinator

    workspace = tmp_path / "workspace"
    session_root = tmp_path / "dsh-jsonl"
    source_data = tmp_path / "wtb-source"
    target_data = tmp_path / "wtb-target"
    for directory in (workspace, session_root, source_data, target_data):
        directory.mkdir()
    subprocess.run(("git", "init", "-q"), cwd=workspace, check=True)
    subprocess.run(
        ("git", "config", "user.email", "cavc@example.invalid"),
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ("git", "config", "user.name", "CAVC E2E"),
        cwd=workspace,
        check=True,
    )
    (workspace / "source.txt").write_text("authoritative source v1\n", encoding="utf-8")
    subprocess.run(("git", "add", "source.txt"), cwd=workspace, check=True)
    subprocess.run(
        ("git", "commit", "-q", "-m", "authoritative source"),
        cwd=workspace,
        check=True,
    )

    with _completion_server() as (base_url, requests):
        source_project = _project("w0-source")
        source_bench = WTBTestBench.create_with_runtime(
            _runtime(
                checkout=checkout,
                base_url=base_url,
                session_root=session_root,
                workspace=workspace,
                namespace="w0-e2e",
            ),
            data_dir=str(source_data),
            workflow=DeepSeekHarnessWorkflow(),
        )
        source_bench.attach_project(source_project)
        target_workflow = DeepSeekHarnessWorkflow(
            reference_key="memory_references",
            memory_mode="S0",
        )
        target_project = WorkflowProject(
            name="w0-target",
            graph_factory=lambda: target_workflow,
        )
        target_bench = WTBTestBench.create_with_runtime(
            _runtime(
                checkout=checkout,
                base_url=base_url,
                session_root=session_root,
                workspace=workspace,
                namespace="w0-e2e",
            ),
            data_dir=str(target_data),
            workflow=target_workflow,
        )
        target_bench.attach_project(target_project)
        try:
            coordinator = W0Coordinator(
                port=WTBTwoSessionPort(
                    source_bench=source_bench,
                    source_project=source_project.name,
                    target_bench=target_bench,
                    target_project=target_project.name,
                ),
                source_authority=GitSourceAuthority(workspace),
            )
            manifest = coordinator.run(
                run_id="real-w0-e2e",
                source_prompt="publish authoritative source v1",
                target_prompt="use the authoritative source",
            )
        finally:
            target_bench.close()
            source_bench.close()

    assert manifest.condition == "W0"
    assert manifest.source.session_id != manifest.target.session_id
    assert manifest.source_authority.attempt_id == manifest.source.attempt_id
    assert manifest.capture.references[0]["sessionId"] == manifest.source.session_id
    assert manifest.capture.references[0]["capturedThroughSeq"] >= (
        manifest.source.last_event_seq
    )
    assert all(receipt.released for receipt in manifest.schedule)
    assert len(requests) == 2


@pytest.mark.parametrize("persistence_kind", ["jsonl", "sqlite"])
def test_real_terminal_survives_wtb_final_commit_loss_and_reconcile(
    tmp_path: Path,
    persistence_kind: str,
    request: pytest.FixtureRequest,
) -> None:
    checkout = _dsh_checkout()
    workspace = tmp_path / "workspace"
    session_root = tmp_path / "dsh-jsonl"
    data_dir = tmp_path / "wtb"
    workspace.mkdir()
    session_root.mkdir()
    data_dir.mkdir()
    cordis = None
    if persistence_kind == "sqlite":
        config_dir = (
            checkout
            / "python"
            / "sdk-runtime"
            / f".wtb-e2e-{os.getpid()}-{tmp_path.name}"
        )
        config_dir.mkdir()
        request.addfinalizer(lambda: shutil.rmtree(config_dir, ignore_errors=True))
        cordis = _sqlite_cordis(
            checkout,
            config_dir / "cordis.yml",
            tmp_path / "dsh-sessions.db",
        )
    db_url = f"sqlite:///{(data_dir / 'wtb.db').as_posix()}"
    operation_uows: list[SQLAlchemyUnitOfWork] = []

    class FailOnlyTerminalCommitUoW(SQLAlchemyUnitOfWork):
        def __init__(self) -> None:
            super().__init__(db_url)
            self.commit_calls = 0

        def commit(self) -> None:
            self.commit_calls += 1
            if self.commit_calls == 3:
                self.rollback()
                raise RuntimeError("real E2E terminal WTB commit lost")
            super().commit()

    def operation_uow_factory() -> SQLAlchemyUnitOfWork:
        uow = FailOnlyTerminalCommitUoW()
        operation_uows.append(uow)
        return uow

    with _completion_server() as (base_url, requests):
        first_runtime = _runtime(
            checkout=checkout,
            base_url=base_url,
            session_root=session_root,
            workspace=workspace,
            cordis=cordis,
        )
        project = _project("real-jsonl-terminal")
        first = WTBTestBench.create_with_runtime(
            first_runtime,
            data_dir=str(data_dir),
            workflow=DeepSeekHarnessWorkflow(),
            operation_uow_factory=operation_uow_factory,
        )
        first.attach_project(project)
        try:
            with pytest.raises(
                RuntimeError,
                match="real E2E terminal WTB commit lost",
            ) as caught:
                first.run(
                    project=project.name,
                    initial_state={"prompt": "return one deterministic answer"},
                )
            execution_id = caught.value.wtb_execution_id
            assert operation_uows[0].commit_calls == 3
            assert len(requests) == 1
            fenced = first.get_execution(execution_id)
            assert fenced.status is ExecutionStatus.RUNNING
            assert fenced.metadata["recovery_reason"] == "runtime_dispatch_inflight"
        finally:
            first.close()

        restarted_runtime = _runtime(
            checkout=checkout,
            base_url=base_url,
            session_root=session_root,
            workspace=workspace,
            cordis=cordis,
        )
        restarted = WTBTestBench.create_with_runtime(
            restarted_runtime,
            data_dir=str(data_dir),
            workflow=DeepSeekHarnessWorkflow(),
        )
        restarted.attach_project(project)
        try:
            recovered = restarted.reconcile_runtime_outcome(execution_id)
            assert recovered.status is ExecutionStatus.COMPLETED
            _assert_dsh_receipt(
                recovered,
                execution_id=execution_id,
                answer=_CompletionHandler.response_text,
            )
            assert len(requests) == 1
            events = restarted.list_outbox_events(execution_id=execution_id)
            assert sum(
                event.event_type is OutboxEventType.EXECUTION_DISPATCH_PREPARED
                for event in events
            ) == 1
            assert sum(
                event.event_type is OutboxEventType.EXECUTION_RECONCILED
                for event in events
            ) == 1
            assert sum(
                event.event_type is OutboxEventType.EXECUTION_COMPLETED
                for event in events
            ) == 1
        finally:
            restarted.close()

    if persistence_kind == "jsonl":
        assert list(session_root.rglob("*.jsonl"))
    else:
        assert (tmp_path / "dsh-sessions.db").is_file()


def test_real_fence_commit_before_dsh_admission_stays_fail_closed(
    tmp_path: Path,
) -> None:
    checkout = _dsh_checkout()
    workspace = tmp_path / "workspace"
    session_root = tmp_path / "dsh-jsonl"
    data_dir = tmp_path / "wtb"
    workspace.mkdir()
    session_root.mkdir()
    data_dir.mkdir()
    db_url = f"sqlite:///{(data_dir / 'wtb.db').as_posix()}"

    class FailAfterFenceCommitUoW(SQLAlchemyUnitOfWork):
        def __init__(self) -> None:
            super().__init__(db_url)
            self.commit_calls = 0

        def commit(self) -> None:
            self.commit_calls += 1
            super().commit()
            if self.commit_calls == 2:
                raise RuntimeError("real E2E failed after durable fence")

    with _completion_server() as (base_url, requests):
        project = _project("real-fence-before-admission")
        first = WTBTestBench.create_with_runtime(
            _runtime(
                checkout=checkout,
                base_url=base_url,
                session_root=session_root,
                workspace=workspace,
            ),
            data_dir=str(data_dir),
            workflow=DeepSeekHarnessWorkflow(),
            operation_uow_factory=FailAfterFenceCommitUoW,
        )
        first.attach_project(project)
        try:
            with pytest.raises(
                RuntimeError,
                match="real E2E failed after durable fence",
            ) as caught:
                first.run(
                    project=project.name,
                    initial_state={"prompt": "redrive only after exact lookup"},
                )
            execution_id = caught.value.wtb_execution_id
            assert requests == []
            fenced = first.get_execution(execution_id)
            assert fenced.status is ExecutionStatus.RUNNING
            assert fenced.metadata["runtime_attempt"]["binding"]
        finally:
            first.close()

        restarted = WTBTestBench.create_with_runtime(
            _runtime(
                checkout=checkout,
                base_url=base_url,
                session_root=session_root,
                workspace=workspace,
            ),
            data_dir=str(data_dir),
            workflow=DeepSeekHarnessWorkflow(),
        )
        restarted.attach_project(project)
        try:
            with pytest.raises(RuntimeOutcomeUnknownError, match="not found"):
                restarted.reconcile_runtime_outcome(execution_id)
            recovered = restarted.get_execution(execution_id)
            assert recovered.status is ExecutionStatus.RUNNING
            assert recovered.metadata["recovery_required"] is True
            assert requests == []
            events = restarted.list_outbox_events(execution_id=execution_id)
            assert sum(
                event.event_type is OutboxEventType.EXECUTION_DISPATCH_PREPARED
                for event in events
            ) == 1
            assert not any(
                event.event_type is OutboxEventType.EXECUTION_RECONCILED
                for event in events
            )
        finally:
            restarted.close()


def test_real_loader_executes_bash_once_before_terminal_receipt(tmp_path: Path) -> None:
    checkout = _dsh_checkout()
    workspace = tmp_path / "workspace"
    session_root = tmp_path / "dsh-jsonl"
    data_dir = tmp_path / "wtb"
    effect_file = workspace / "tool-effect.log"
    workspace.mkdir()
    session_root.mkdir()
    data_dir.mkdir()

    with _bash_server(effect_file) as (base_url, requests):
        project = _project("real-bash-round-trip")
        bench = WTBTestBench.create_with_runtime(
            _runtime(
                checkout=checkout,
                base_url=base_url,
                session_root=session_root,
                workspace=workspace,
            ),
            data_dir=str(data_dir),
            workflow=DeepSeekHarnessWorkflow(),
        )
        bench.attach_project(project)
        try:
            completed = bench.run(
                project=project.name,
                initial_state={"prompt": "Use bash exactly once, then finish."},
            )
            assert completed.status is ExecutionStatus.COMPLETED
            _assert_dsh_receipt(
                completed,
                execution_id=completed.id,
                answer=_BashRoundTripHandler.response_text,
            )
        finally:
            bench.close()

    assert len(requests) == 2
    assert effect_file.read_text(encoding="utf-8").splitlines() == ["wtb-effect"]
    assert "tool" in json.dumps(requests[1]).lower()


def test_real_cloned_store_uuid_at_new_root_fails_closed_before_lookup(
    tmp_path: Path,
) -> None:
    checkout = _dsh_checkout()
    workspace = tmp_path / "workspace"
    original_root = tmp_path / "dsh-original"
    cloned_root = tmp_path / "dsh-clone"
    data_dir = tmp_path / "wtb"
    workspace.mkdir()
    original_root.mkdir()
    data_dir.mkdir()
    db_url = f"sqlite:///{(data_dir / 'wtb.db').as_posix()}"

    class FailAfterFenceCommitUoW(SQLAlchemyUnitOfWork):
        def __init__(self) -> None:
            super().__init__(db_url)
            self.commit_calls = 0

        def commit(self) -> None:
            self.commit_calls += 1
            super().commit()
            if self.commit_calls == 2:
                raise RuntimeError("copy store after durable fence")

    with _completion_server() as (base_url, requests):
        project = _project("real-store-clone")
        first = WTBTestBench.create_with_runtime(
            _runtime(
                checkout=checkout,
                base_url=base_url,
                session_root=original_root,
                workspace=workspace,
            ),
            data_dir=str(data_dir),
            workflow=DeepSeekHarnessWorkflow(),
            operation_uow_factory=FailAfterFenceCommitUoW,
        )
        first.attach_project(project)
        try:
            with pytest.raises(RuntimeError, match="copy store after durable fence") as caught:
                first.run(
                    project=project.name,
                    initial_state={"prompt": "must not redrive from clone"},
                )
            execution_id = caught.value.wtb_execution_id
            assert requests == []
        finally:
            first.close()

        identity_file = original_root / ".persistence-instance.json"
        assert identity_file.is_file()
        shutil.copytree(original_root, cloned_root)
        assert (cloned_root / identity_file.name).read_bytes() == identity_file.read_bytes()

        restarted = WTBTestBench.create_with_runtime(
            _runtime(
                checkout=checkout,
                base_url=base_url,
                session_root=cloned_root,
                workspace=workspace,
            ),
            data_dir=str(data_dir),
            workflow=DeepSeekHarnessWorkflow(),
        )
        restarted.attach_project(project)
        try:
            with pytest.raises(
                RuntimeBindingMismatchError,
                match="operator config fingerprint drifted from the dispatch fence",
            ):
                restarted.reconcile_runtime_outcome(execution_id)
            assert requests == []
            fenced = restarted.get_execution(execution_id)
            assert fenced.status is ExecutionStatus.RUNNING
            assert fenced.metadata["recovery_required"] is True
        finally:
            restarted.close()


def test_real_python_sdk_restarts_dead_child_in_same_bench(tmp_path: Path) -> None:
    checkout = _dsh_checkout()
    workspace = tmp_path / "workspace"
    session_root = tmp_path / "dsh-jsonl"
    data_dir = tmp_path / "wtb"
    workspace.mkdir()
    session_root.mkdir()
    data_dir.mkdir()

    with _completion_server() as (base_url, requests):
        runtime = _runtime(
            checkout=checkout,
            base_url=base_url,
            session_root=session_root,
            workspace=workspace,
        )
        project = _project("real-child-restart")
        bench = WTBTestBench.create_with_runtime(
            runtime,
            data_dir=str(data_dir),
            workflow=DeepSeekHarnessWorkflow(),
        )
        bench.attach_project(project)
        try:
            first = bench.run(
                project=project.name,
                initial_state={"prompt": "first child generation"},
            )
            first_receipt = first.state.workflow_variables["_dsh"]
            harness = runtime._harness
            assert harness is not None
            old_process = harness.client._proc
            assert old_process is not None
            old_pid = old_process.pid
            old_process.kill()
            assert old_process.wait(timeout=10) is not None

            second = bench.run(
                project=project.name,
                initial_state={"prompt": "second child generation"},
            )
            second_receipt = second.state.workflow_variables["_dsh"]
            new_process = harness.client._proc
            assert new_process is not None
            assert new_process.pid != old_pid
            assert (
                first_receipt["binding"]["persistence_instance_id"]
                == second_receipt["binding"]["persistence_instance_id"]
            )
            _assert_dsh_receipt(
                second,
                execution_id=second.id,
                answer=_CompletionHandler.response_text,
            )
            assert len(requests) == 2
        finally:
            bench.close()


def test_real_fenced_dispatch_never_restarts_dead_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """A child lost after local validation cannot reload changed Cordis."""
    checkout = _dsh_checkout()
    workspace = tmp_path / "workspace"
    session_root = tmp_path / "dsh-jsonl"
    data_dir = tmp_path / "wtb"
    config_dir = (
        checkout
        / "python"
        / "sdk-runtime"
        / f".wtb-generation-{os.getpid()}-{tmp_path.name}"
    )
    cordis = config_dir / "cordis.yml"
    workspace.mkdir()
    session_root.mkdir()
    data_dir.mkdir()
    config_dir.mkdir()
    request.addfinalizer(lambda: shutil.rmtree(config_dir, ignore_errors=True))
    source_cordis = checkout / "examples" / "jsonrpc-agent" / "cordis.yml"
    cordis.write_text(source_cordis.read_text(encoding="utf-8"), encoding="utf-8")

    with _completion_server() as (base_url, requests):
        runtime = _runtime(
            checkout=checkout,
            base_url=base_url,
            session_root=session_root,
            workspace=workspace,
            cordis=cordis,
        )
        project = _project("real-generation-pin")
        bench = WTBTestBench.create_with_runtime(
            runtime,
            data_dir=str(data_dir),
            workflow=DeepSeekHarnessWorkflow(),
        )
        bench.attach_project(project)
        original_validate = runtime._validate_binding_envelope
        killed: dict[str, Any] = {}

        def validate_then_kill(binding, **kwargs):
            original_validate(binding, **kwargs)
            if kwargs.get("verify_current_binding") is not False or killed:
                return
            harness = runtime._harness
            assert harness is not None
            client = harness.client
            process = client._proc
            assert process is not None
            killed.update(client=client, process=process, pid=process.pid)
            process.kill()
            process.wait(timeout=10)
            cordis.write_text(
                cordis.read_text(encoding="utf-8")
                + "\n# changed after the fenced generation was validated\n",
                encoding="utf-8",
            )

        monkeypatch.setattr(
            runtime,
            "_validate_binding_envelope",
            validate_then_kill,
        )
        try:
            failed = bench.run(
                project=project.name,
                initial_state={"prompt": "must never reach the model"},
            )
            assert failed.status is ExecutionStatus.FAILED
            assert "process generation changed" in (failed.error_message or "")
            assert requests == []
            harness = runtime._harness
            assert harness is not None
            assert harness.client is killed["client"]
            assert harness.client._proc is killed["process"]
            assert harness.client._proc.poll() is not None
            terminal = failed.metadata["last_runtime_attempt"]
            assert terminal["runtime_metadata"]["process_generation"]
            assert terminal["receipt"] == {"dispatched": False}
            events = bench.list_outbox_events(execution_id=failed.id)
            assert sum(
                event.event_type is OutboxEventType.EXECUTION_DISPATCH_PREPARED
                for event in events
            ) == 1
        finally:
            bench.close()


def test_real_loader_root_attestation_rejects_config_aba_before_dispatch(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """Operator sees A before/after initialize while Loader actually mounted B."""
    checkout = _dsh_checkout()
    workspace = tmp_path / "workspace"
    session_root = tmp_path / "dsh-jsonl"
    data_dir = tmp_path / "wtb"
    config_dir = (
        checkout
        / "python"
        / "sdk-runtime"
        / f".wtb-aba-{os.getpid()}-{tmp_path.name}"
    )
    workspace.mkdir()
    session_root.mkdir()
    data_dir.mkdir()
    config_dir.mkdir()
    request.addfinalizer(lambda: shutil.rmtree(config_dir, ignore_errors=True))

    source = checkout / "examples" / "jsonrpc-agent" / "cordis.yml"
    config_a = source.read_text(encoding="utf-8")
    cordis = config_dir / "cordis.yml"
    cordis_a = config_dir / "cordis.a.yml"
    cordis_b = config_dir / "cordis.b.yml"
    restore_plugin = config_dir / "restore-root.mjs"
    wrapper = config_dir / "aba-wrapper.mjs"
    cordis.write_text(config_a, encoding="utf-8")
    cordis_a.write_text(config_a, encoding="utf-8")
    restore_plugin.write_text(
        "import { copyFileSync } from 'node:fs'\n"
        "export function apply() {\n"
        "  copyFileSync(new URL('./cordis.a.yml', import.meta.url), "
        "new URL('./cordis.yml', import.meta.url))\n"
        "}\n",
        encoding="utf-8",
    )
    cordis_b.write_text(
        "- id: restore-operator-visible-root\n"
        "  name: ./restore-root.mjs\n"
        + config_a,
        encoding="utf-8",
    )
    wrapper.write_text(
        "import { copyFileSync } from 'node:fs'\n"
        "import { spawn } from 'node:child_process'\n"
        "const [entry, config] = process.argv.slice(2)\n"
        "copyFileSync(new URL('./cordis.b.yml', import.meta.url), config)\n"
        "const child = spawn(process.execPath, [entry, config], {\n"
        "  cwd: process.cwd(), env: process.env, stdio: 'inherit'\n"
        "})\n"
        "for (const signal of ['SIGINT', 'SIGTERM']) {\n"
        "  process.on(signal, () => child.kill(signal))\n"
        "}\n"
        "child.on('exit', code => process.exit(code ?? 1))\n",
        encoding="utf-8",
    )

    with _completion_server() as (base_url, requests):
        runtime = _runtime(
            checkout=checkout,
            base_url=base_url,
            session_root=session_root,
            workspace=workspace,
            cordis=cordis,
            runtime_wrapper=wrapper,
        )
        project = _project("real-config-aba")
        bench = WTBTestBench.create_with_runtime(
            runtime,
            data_dir=str(data_dir),
            workflow=DeepSeekHarnessWorkflow(),
        )
        bench.attach_project(project)
        try:
            with pytest.raises(
                RuntimeBindingMismatchError,
                match="Loader root config bytes differ",
            ) as caught:
                bench.run(
                    project=project.name,
                    initial_state={"prompt": "must never reach the model"},
                )
            execution_id = caught.value.wtb_execution_id
            assert requests == []
            assert cordis.read_text(encoding="utf-8") == config_a
            created = bench.get_execution(execution_id)
            assert created.status is ExecutionStatus.PENDING
            events = bench.list_outbox_events(execution_id=execution_id)
            assert [event.event_type for event in events] == [
                OutboxEventType.EXECUTION_CREATED
            ]
        finally:
            bench.close()


def test_real_tool_effect_then_child_kill_remains_unknown_without_redrive(
    tmp_path: Path,
) -> None:
    checkout = _dsh_checkout()
    workspace = tmp_path / "workspace"
    session_root = tmp_path / "dsh-jsonl"
    data_dir = tmp_path / "wtb"
    effect_file = workspace / "tool-effect.log"
    workspace.mkdir()
    session_root.mkdir()
    data_dir.mkdir()

    with _held_bash_server(effect_file) as (
        base_url,
        requests,
        second_request_seen,
        release_final,
    ):
        runtime = _runtime(
            checkout=checkout,
            base_url=base_url,
            session_root=session_root,
            workspace=workspace,
        )
        project = _project("real-effect-crash")
        bench = WTBTestBench.create_with_runtime(
            runtime,
            data_dir=str(data_dir),
            workflow=DeepSeekHarnessWorkflow(),
        )
        bench.attach_project(project)
        result: dict[str, Any] = {}

        def run() -> None:
            try:
                result["execution"] = bench.run(
                    project=project.name,
                    initial_state={"prompt": "write effect then wait"},
                )
            except BaseException as error:  # captured for the calling thread
                result["error"] = error

        worker = threading.Thread(target=run)
        worker.start()
        try:
            assert second_request_seen.wait(timeout=30)
            assert effect_file.read_text(encoding="utf-8").splitlines() == [
                "wtb-effect"
            ]
            harness = runtime._harness
            assert harness is not None
            process = harness.client._proc
            assert process is not None
            process.kill()
            process.wait(timeout=10)
            release_final.set()
            worker.join(timeout=30)
            assert not worker.is_alive()
            assert "error" not in result
            unknown = result["execution"]
            execution_id = unknown.id
            assert unknown.status is ExecutionStatus.RUNNING
            assert unknown.metadata["recovery_required"] is True
            assert unknown.metadata["recovery_reason"] == "runtime_outcome_unknown"
        finally:
            release_final.set()
            worker.join(timeout=5)
            bench.close()

        restarted = WTBTestBench.create_with_runtime(
            _runtime(
                checkout=checkout,
                base_url=base_url,
                session_root=session_root,
                workspace=workspace,
            ),
            data_dir=str(data_dir),
            workflow=DeepSeekHarnessWorkflow(),
        )
        restarted.attach_project(project)
        try:
            with pytest.raises(
                RuntimeOutcomeUnknownError,
                match="interrupted|non-terminal",
            ):
                restarted.reconcile_runtime_outcome(execution_id)
            still_unknown = restarted.get_execution(execution_id)
            assert still_unknown.status is ExecutionStatus.RUNNING
            assert still_unknown.metadata["recovery_required"] is True
        finally:
            restarted.close()

    assert len(requests) == 2
    assert effect_file.read_text(encoding="utf-8").splitlines() == ["wtb-effect"]


def test_real_same_path_old_store_restore_never_replays_bash_effect(
    tmp_path: Path,
) -> None:
    checkout = _dsh_checkout()
    workspace = tmp_path / "workspace"
    session_root = tmp_path / "dsh-jsonl"
    pre_admission_backup = tmp_path / "pre-admission-backup"
    data_dir = tmp_path / "wtb"
    effect_file = workspace / "tool-effect.log"
    workspace.mkdir()
    session_root.mkdir()
    data_dir.mkdir()
    db_url = f"sqlite:///{(data_dir / 'wtb.db').as_posix()}"

    class BackupFenceThenFailTerminalUoW(SQLAlchemyUnitOfWork):
        def __init__(self) -> None:
            super().__init__(db_url)
            self.commit_calls = 0

        def commit(self) -> None:
            self.commit_calls += 1
            if self.commit_calls == 3:
                self.rollback()
                raise RuntimeError("terminal WTB commit lost before old restore")
            super().commit()
            if self.commit_calls == 2:
                shutil.copytree(session_root, pre_admission_backup)

    with _bash_server(effect_file) as (base_url, requests):
        project = _project("real-same-path-restore")
        first = WTBTestBench.create_with_runtime(
            _runtime(
                checkout=checkout,
                base_url=base_url,
                session_root=session_root,
                workspace=workspace,
            ),
            data_dir=str(data_dir),
            workflow=DeepSeekHarnessWorkflow(),
            operation_uow_factory=BackupFenceThenFailTerminalUoW,
        )
        first.attach_project(project)
        try:
            with pytest.raises(
                RuntimeError,
                match="terminal WTB commit lost before old restore",
            ) as caught:
                first.run(
                    project=project.name,
                    initial_state={"prompt": "one effect before rollback restore"},
                )
            execution_id = caught.value.wtb_execution_id
            assert len(requests) == 2
            assert effect_file.read_text(encoding="utf-8").splitlines() == [
                "wtb-effect"
            ]
        finally:
            first.close()

        identity_name = ".persistence-instance.json"
        backup_identity = (pre_admission_backup / identity_name).read_bytes()
        shutil.rmtree(session_root)
        shutil.copytree(pre_admission_backup, session_root)
        assert (session_root / identity_name).read_bytes() == backup_identity

        restarted = WTBTestBench.create_with_runtime(
            _runtime(
                checkout=checkout,
                base_url=base_url,
                session_root=session_root,
                workspace=workspace,
            ),
            data_dir=str(data_dir),
            workflow=DeepSeekHarnessWorkflow(),
        )
        restarted.attach_project(project)
        try:
            with pytest.raises(RuntimeOutcomeUnknownError, match="not found"):
                restarted.reconcile_runtime_outcome(execution_id)
            unresolved = restarted.get_execution(execution_id)
            assert unresolved.status is ExecutionStatus.RUNNING
            assert unresolved.metadata["recovery_required"] is True
            assert len(requests) == 2
            assert effect_file.read_text(encoding="utf-8").splitlines() == [
                "wtb-effect"
            ]
        finally:
            restarted.close()


def test_real_same_path_queued_backup_never_wakes_or_replays_bash(
    tmp_path: Path,
) -> None:
    checkout = _dsh_checkout()
    workspace = tmp_path / "workspace"
    session_root = tmp_path / "dsh-jsonl"
    queued_backup = tmp_path / "queued-backup"
    data_dir = tmp_path / "wtb"
    effect_file = workspace / "tool-effect.log"
    workspace.mkdir()
    session_root.mkdir()
    data_dir.mkdir()
    db_url = f"sqlite:///{(data_dir / 'wtb.db').as_posix()}"

    class FailTerminalCommitUoW(SQLAlchemyUnitOfWork):
        def __init__(self) -> None:
            super().__init__(db_url)
            self.commit_calls = 0

        def commit(self) -> None:
            self.commit_calls += 1
            if self.commit_calls == 3:
                self.rollback()
                raise RuntimeError("terminal WTB commit lost after queued backup")
            super().commit()

    with _held_tool_call_server(effect_file) as (
        base_url,
        requests,
        release_tool_call,
    ):
        runtime = _runtime(
            checkout=checkout,
            base_url=base_url,
            session_root=session_root,
            workspace=workspace,
        )
        original_invoke = runtime.invoke
        queued_receipts: list[Any] = []

        def admit_copy_then_invoke(input_state, **kwargs):
            harness = runtime._harness
            assert harness is not None
            binding = kwargs["binding"]
            admitted = harness.client.start_attempt(
                binding.exact_session_id,
                binding.attempt_id,
                binding.input_sha256,
                [{"type": "text", "text": input_state["prompt"]}],
            )
            queued_receipts.append(admitted.attempt)
            assert admitted.attempt.status == "queued"
            shutil.copytree(session_root, queued_backup)
            release_tool_call.set()
            return original_invoke(input_state, **kwargs)

        runtime.invoke = admit_copy_then_invoke
        project = _project("real-queued-restore")
        first = WTBTestBench.create_with_runtime(
            runtime,
            data_dir=str(data_dir),
            workflow=DeepSeekHarnessWorkflow(),
            operation_uow_factory=FailTerminalCommitUoW,
        )
        first.attach_project(project)
        try:
            with pytest.raises(
                RuntimeError,
                match="terminal WTB commit lost after queued backup",
            ) as caught:
                first.run(
                    project=project.name,
                    initial_state={"prompt": "admit once, execute bash once"},
                )
            execution_id = caught.value.wtb_execution_id
            assert len(queued_receipts) == 1
            assert len(requests) == 2
            assert effect_file.read_text(encoding="utf-8").splitlines() == [
                "wtb-effect"
            ]
        finally:
            release_tool_call.set()
            first.close()

        identity_name = ".persistence-instance.json"
        queued_identity = (queued_backup / identity_name).read_bytes()
        shutil.rmtree(session_root)
        shutil.copytree(queued_backup, session_root)
        assert (session_root / identity_name).read_bytes() == queued_identity

        restarted_runtime = _runtime(
            checkout=checkout,
            base_url=base_url,
            session_root=session_root,
            workspace=workspace,
        )
        restarted = WTBTestBench.create_with_runtime(
            restarted_runtime,
            data_dir=str(data_dir),
            workflow=DeepSeekHarnessWorkflow(),
        )
        restarted.attach_project(project)
        try:
            with pytest.raises(
                RuntimeOutcomeUnknownError,
                match="process generation",
            ):
                restarted.stop(execution_id)
            cold_unresolved = restarted.get_execution(execution_id)
            assert cold_unresolved.status is ExecutionStatus.RUNNING
            assert cold_unresolved.metadata["recovery_required"] is True
            assert restarted_runtime._harness is not None
            assert restarted_runtime._harness.client._proc is None
            assert len(requests) == 2
            assert effect_file.read_text(encoding="utf-8").splitlines() == [
                "wtb-effect"
            ]
            assert not any(
                event.event_type is OutboxEventType.EXECUTION_STOPPED
                for event in restarted.list_outbox_events(
                    execution_id=execution_id,
                )
            )

            with pytest.raises(
                RuntimeOutcomeUnknownError,
                match="interrupted|non-terminal|not found",
            ):
                restarted.reconcile_runtime_outcome(execution_id)
            assert len(requests) == 2
            assert effect_file.read_text(encoding="utf-8").splitlines() == [
                "wtb-effect"
            ]
            unresolved = restarted.get_execution(execution_id)
            assert unresolved.status is ExecutionStatus.RUNNING
            assert unresolved.metadata["recovery_required"] is True
        finally:
            restarted.close()


def test_real_run_stop_concurrency_has_one_cancel_terminal_outbox(
    tmp_path: Path,
) -> None:
    checkout = _dsh_checkout()
    workspace = tmp_path / "workspace"
    session_root = tmp_path / "dsh-jsonl"
    data_dir = tmp_path / "wtb"
    workspace.mkdir()
    session_root.mkdir()
    data_dir.mkdir()

    with _blocking_completion_server() as (base_url, requests, request_seen):
        runtime = _runtime(
            checkout=checkout,
            base_url=base_url,
            session_root=session_root,
            workspace=workspace,
        )
        project = _project("real-run-stop")
        bench = WTBTestBench.create_with_runtime(
            runtime,
            data_dir=str(data_dir),
            workflow=DeepSeekHarnessWorkflow(),
        )
        bench.attach_project(project)
        observed: dict[str, Any] = {}

        def run() -> None:
            try:
                observed["run"] = bench.run(
                    project=project.name,
                    initial_state={"prompt": "block until cancelled"},
                )
            except BaseException as error:
                observed["error"] = error

        run_worker = threading.Thread(target=run)
        run_worker.start()
        try:
            assert request_seen.wait(timeout=30)
            deadline = time.monotonic() + 10
            execution_id = None
            while execution_id is None and time.monotonic() < deadline:
                created = [
                    event
                    for event in bench.list_outbox_events(limit=100)
                    if event.event_type is OutboxEventType.EXECUTION_CREATED
                ]
                if created:
                    execution_id = created[0].aggregate_id
                else:
                    time.sleep(0.01)
            assert execution_id is not None

            stopped = bench.stop(execution_id)
            _BlockingCompletionHandler.release.set()
            run_worker.join(timeout=30)
            assert not run_worker.is_alive()
            assert "error" not in observed
            assert stopped.status is ExecutionStatus.CANCELLED
            assert observed["run"].status is ExecutionStatus.CANCELLED
            events = bench.list_outbox_events(execution_id=execution_id)
            assert sum(
                event.event_type is OutboxEventType.EXECUTION_STOPPED
                for event in events
            ) == 1
            assert not any(
                event.event_type is OutboxEventType.EXECUTION_COMPLETED
                for event in events
            )
            assert len(requests) == 1
        finally:
            _BlockingCompletionHandler.release.set()
            run_worker.join(timeout=5)
            bench.close()


def test_real_run_reconcile_stop_concurrency_has_one_cancel_terminal_outbox(
    tmp_path: Path,
) -> None:
    """Exercise three operation-scoped UoWs against one real DSH attempt."""
    checkout = _dsh_checkout()
    workspace = tmp_path / "workspace"
    session_root = tmp_path / "dsh-jsonl"
    data_dir = tmp_path / "wtb"
    workspace.mkdir()
    session_root.mkdir()
    data_dir.mkdir()

    with _blocking_completion_server() as (base_url, requests, request_seen):
        runtime = _runtime(
            checkout=checkout,
            base_url=base_url,
            session_root=session_root,
            workspace=workspace,
        )
        project = _project("real-run-reconcile-stop")
        bench = WTBTestBench.create_with_runtime(
            runtime,
            data_dir=str(data_dir),
            workflow=DeepSeekHarnessWorkflow(),
        )
        bench.attach_project(project)
        observed: dict[str, Any] = {}

        def run() -> None:
            try:
                observed["run"] = bench.run(
                    project=project.name,
                    initial_state={"prompt": "race reconcile with cancellation"},
                )
            except BaseException as error:
                observed["run_error"] = error

        run_worker = threading.Thread(target=run)
        run_worker.start()
        try:
            assert request_seen.wait(timeout=30)
            deadline = time.monotonic() + 10
            execution_id = None
            while execution_id is None and time.monotonic() < deadline:
                created = [
                    event
                    for event in bench.list_outbox_events(limit=100)
                    if event.event_type is OutboxEventType.EXECUTION_CREATED
                ]
                if created:
                    execution_id = created[0].aggregate_id
                else:
                    time.sleep(0.01)
            assert execution_id is not None

            race = threading.Barrier(3)

            def reconcile() -> None:
                race.wait(timeout=5)
                try:
                    observed["reconcile"] = bench.reconcile_runtime_outcome(
                        execution_id
                    )
                except BaseException as error:
                    observed["reconcile_error"] = error

            def stop() -> None:
                race.wait(timeout=5)
                try:
                    observed["stop"] = bench.stop(execution_id)
                except BaseException as error:
                    observed["stop_error"] = error

            reconcile_worker = threading.Thread(target=reconcile)
            stop_worker = threading.Thread(target=stop)
            reconcile_worker.start()
            stop_worker.start()
            race.wait(timeout=5)
            reconcile_worker.join(timeout=30)
            stop_worker.join(timeout=30)
            _BlockingCompletionHandler.release.set()
            run_worker.join(timeout=30)

            assert not reconcile_worker.is_alive()
            assert not stop_worker.is_alive()
            assert not run_worker.is_alive()
            assert "run_error" not in observed
            assert "stop_error" not in observed
            assert observed["run"].status is ExecutionStatus.CANCELLED
            assert observed["stop"].status is ExecutionStatus.CANCELLED
            if "reconcile_error" in observed:
                assert isinstance(
                    observed["reconcile_error"],
                    RuntimeOutcomeUnknownError,
                )
            else:
                assert observed["reconcile"].status is ExecutionStatus.CANCELLED

            persisted = bench.get_execution(execution_id)
            assert persisted.status is ExecutionStatus.CANCELLED
            events = bench.list_outbox_events(execution_id=execution_id)
            terminal_types = {
                OutboxEventType.EXECUTION_COMPLETED,
                OutboxEventType.EXECUTION_FAILED,
                OutboxEventType.EXECUTION_STOPPED,
            }
            assert sum(event.event_type in terminal_types for event in events) == 1
            assert sum(
                event.event_type is OutboxEventType.EXECUTION_STOPPED
                for event in events
            ) == 1
            assert sum(
                event.event_type is OutboxEventType.EXECUTION_RECONCILED
                for event in events
            ) <= 1
            assert len(requests) == 1
        finally:
            _BlockingCompletionHandler.release.set()
            run_worker.join(timeout=5)
            bench.close()


def test_real_run_reconcile_close_is_bounded_after_one_tool_side_effect(
    tmp_path: Path,
) -> None:
    """Close interrupts real stdio work without sharing a SQLAlchemy Session."""
    checkout = _dsh_checkout()
    workspace = tmp_path / "workspace"
    session_root = tmp_path / "dsh-jsonl"
    data_dir = tmp_path / "wtb"
    effect_file = tmp_path / "tool-effects.log"
    workspace.mkdir()
    session_root.mkdir()
    data_dir.mkdir()

    with _held_bash_server(effect_file) as (
        base_url,
        requests,
        second_request_seen,
        release_final,
    ):
        runtime = _runtime(
            checkout=checkout,
            base_url=base_url,
            session_root=session_root,
            workspace=workspace,
        )
        project = _project("real-run-reconcile-close")
        bench = WTBTestBench.create_with_runtime(
            runtime,
            data_dir=str(data_dir),
            workflow=DeepSeekHarnessWorkflow(),
        )
        bench.attach_project(project)
        observed: dict[str, Any] = {}

        def run() -> None:
            try:
                observed["run"] = bench.run(
                    project=project.name,
                    initial_state={"prompt": "execute bash exactly once then wait"},
                )
            except BaseException as error:
                observed["run_error"] = error

        run_worker = threading.Thread(target=run)
        run_worker.start()
        try:
            assert second_request_seen.wait(timeout=30)
            assert effect_file.read_text(encoding="utf-8").splitlines() == [
                "wtb-effect"
            ]

            deadline = time.monotonic() + 10
            execution_id = None
            while execution_id is None and time.monotonic() < deadline:
                created = [
                    event
                    for event in bench.list_outbox_events(limit=100)
                    if event.event_type is OutboxEventType.EXECUTION_CREATED
                ]
                if created:
                    execution_id = created[0].aggregate_id
                else:
                    time.sleep(0.01)
            assert execution_id is not None

            race = threading.Barrier(3)

            def reconcile() -> None:
                race.wait(timeout=5)
                try:
                    observed["reconcile"] = bench.reconcile_runtime_outcome(
                        execution_id
                    )
                except BaseException as error:
                    observed["reconcile_error"] = error

            def close() -> None:
                race.wait(timeout=5)
                started_at = time.monotonic()
                try:
                    bench.close()
                except BaseException as error:
                    observed["close_error"] = error
                finally:
                    observed["close_elapsed"] = time.monotonic() - started_at

            reconcile_worker = threading.Thread(target=reconcile)
            close_worker = threading.Thread(target=close)
            reconcile_worker.start()
            close_worker.start()
            race.wait(timeout=5)
            close_worker.join(timeout=15)
            reconcile_worker.join(timeout=15)
            run_worker.join(timeout=15)

            assert not close_worker.is_alive(), "close exceeded its lifecycle bound"
            assert not reconcile_worker.is_alive()
            assert not run_worker.is_alive()
            assert observed["close_elapsed"] < 10
            assert "close_error" not in observed
            assert "run_error" not in observed
            assert observed["run"].status is ExecutionStatus.RUNNING
            assert observed["run"].metadata["recovery_required"] is True
            if "reconcile_error" in observed:
                error = observed["reconcile_error"]
                assert isinstance(
                    error,
                    (RuntimeError, RuntimeOutcomeUnknownError),
                )
                message = str(error).lower()
                assert isinstance(error, RuntimeOutcomeUnknownError) or any(
                    expected in message
                    for expected in (
                        "wtbtestbench is closing",
                        "wtbtestbench is closed",
                        "deepseek harness runtime is closed",
                        "deepseek harness durable attempt reconciliation failed",
                    )
                )
            else:
                assert observed["reconcile"].status in {
                    ExecutionStatus.RUNNING,
                    ExecutionStatus.CANCELLED,
                }

            assert len(requests) == 2
            assert effect_file.read_text(encoding="utf-8").splitlines() == [
                "wtb-effect"
            ]

            restarted = WTBTestBench.create_with_runtime(
                _runtime(
                    checkout=checkout,
                    base_url=base_url,
                    session_root=session_root,
                    workspace=workspace,
                ),
                data_dir=str(data_dir),
                workflow=DeepSeekHarnessWorkflow(),
            )
            restarted.attach_project(project)
            try:
                persisted = restarted.get_execution(execution_id)
                assert persisted.status is ExecutionStatus.RUNNING
                assert persisted.metadata["recovery_required"] is True
                events = restarted.list_outbox_events(execution_id=execution_id)
                terminal_types = {
                    OutboxEventType.EXECUTION_COMPLETED,
                    OutboxEventType.EXECUTION_FAILED,
                    OutboxEventType.EXECUTION_STOPPED,
                }
                assert not any(
                    event.event_type in terminal_types for event in events
                )
                assert sum(
                    event.event_type
                    is OutboxEventType.EXECUTION_RECOVERY_REQUIRED
                    for event in events
                ) == 1
                assert sum(
                    event.event_type is OutboxEventType.EXECUTION_CREATED
                    for event in events
                ) == 1
                assert sum(
                    event.event_type
                    is OutboxEventType.EXECUTION_DISPATCH_PREPARED
                    for event in events
                ) == 1
                assert sum(
                    event.event_type is OutboxEventType.EXECUTION_STARTED
                    for event in events
                ) == 1
            finally:
                restarted.close()
        finally:
            release_final.set()
            run_worker.join(timeout=5)
            if not getattr(bench, "_closed", False):
                bench.close()
