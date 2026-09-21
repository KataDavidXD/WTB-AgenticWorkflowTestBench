import time
from pathlib import Path

from fastapi.testclient import TestClient
from wtb.api.console import create_app
import pytest


def wait(client, result):
    for _ in range(200):
        op = client.get("/api/v1/operations/" + result["operationId"]).json()
        if op["status"] not in ("queued", "running"):
            assert op["status"] == "completed", op.get("error", op)
            return client.get(
                "/api/v1/executions/"
                + op.get("resultExecutionId", result["executionId"])
            ).json()
        time.sleep(0.05)
    raise AssertionError("operation timed out")


@pytest.mark.parametrize("factory", ["create_project", "create_langgraph_project"])
def test_node_failure_is_terminal_and_can_recover(tmp_path, factory):
    if factory == "create_langgraph_project":
        pytest.importorskip("langgraph")
    config = tmp_path / "projects.json"
    import json

    config.write_text(
        json.dumps(
            {"projects": [{"factory": "wtb.testing.console_project:" + factory}]}
        )
    )
    with TestClient(create_app(tmp_path / "console", config)) as c:
        v = c.get("/api/v1/catalog").json()["variants"][0]
        response = c.post(
            f"/api/v1/workflows/{v['projectId']}/execute",
            json={
                "variantId": v["id"],
                "state": {"text": "recover", "fail": True, "delay": 0},
            },
        )
        job = response.json()
        for _ in range(200):
            op = c.get("/api/v1/operations/" + job["operationId"]).json()
            if op["status"] not in {"queued", "running"}:
                break
            time.sleep(0.03)
        assert op["status"] == "failed", op
        e = c.get("/api/v1/executions/" + job["executionId"]).json()
        assert e["status"] == "failed", e
        assert e["pendingOperation"] is None
        cp = next(
            p
            for p in c.get(f"/api/v1/executions/{e['id']}/checkpoints").json()["items"]
            if p["lastNode"] == "prepare"
        )
        e = wait(
            c,
            c.post(
                f"/api/v1/executions/{e['id']}/rollback",
                json={"checkpointId": cp["checkpointId"]},
            ).json(),
        )
        e = wait(
            c,
            c.post(
                f"/api/v1/executions/{e['id']}/state", json={"state": {"fail": False}}
            ).json(),
        )
        e = wait(c, c.post(f"/api/v1/executions/{e['id']}/resume", json={}).json())
        assert e["status"] == "completed", e


def test_real_execution_recovery(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        response = c.post(
            "/api/v1/workflows/file-workflow/execute",
            json={
                "variantId": "file-workflow/default",
                "state": {"text": "hello", "repeat": 2, "delay": 0},
            },
        )
        assert response.status_code == 202, response.text
        e = wait(c, response.json())
        assert e["status"] == "completed", e
        assert len([n for n in e["nodeRuns"] if n["nodeId"] == "transform"]) == 2
        output = Path(e["runtime"]["output"]) / "report.txt"
        assert output.read_text() == "final:pass 2:hello"
        cps = c.get(f"/api/v1/executions/{e['id']}/checkpoints").json()["items"]
        cp = next(p for p in cps if p["nextNodes"] == ["transform"])
        result = c.post(
            f"/api/v1/executions/{e['id']}/rollback",
            json={"checkpointId": cp["checkpointId"]},
        )
        rolled = wait(c, result.json())
        assert rolled["status"] == "paused"
        original = output.read_bytes()
        child = wait(
            c,
            c.post(
                f"/api/v1/executions/{e['id']}/branches",
                json={"checkpointId": cp["checkpointId"], "state": {"text": "child"}},
            ).json(),
        )
        assert child["runtime"]["workspace"] != e["runtime"]["workspace"]
        child = wait(
            c, c.post(f"/api/v1/executions/{child['id']}/resume", json={}).json()
        )
        assert child["status"] == "completed"
        assert output.read_bytes() == original
        assert "child" in (Path(child["runtime"]["output"]) / "report.txt").read_text()
    with TestClient(create_app(tmp_path)) as c:
        assert c.get("/api/v1/executions/" + e["id"]).json()["status"] == "paused"


def start(c, variant="default", state=None, breakpoints=None):
    r = c.post(
        "/api/v1/workflows/file-workflow/execute",
        json={
            "variantId": "file-workflow/" + variant,
            "state": state or {"text": "test", "delay": 0},
            "breakpoints": breakpoints or [],
        },
    )
    assert r.status_code == 202, r.text
    return r.json()


def test_breakpoints_manual_checkpoint_and_edit(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        e = wait(c, start(c, breakpoints=["transform"]))
        assert e["status"] == "paused"
        assert e["nextNodes"] == ["transform"]
        assert all(n["nodeId"] != "transform" for n in e["nodeRuns"])
        old_cp = e["checkpointId"]
        e = wait(c, c.post(f"/api/v1/executions/{e['id']}/checkpoints", json={}).json())
        assert e["checkpointId"] != old_cp
        e = wait(
            c,
            c.post(
                f"/api/v1/executions/{e['id']}/state",
                json={"state": {"text": "edited"}},
            ).json(),
        )
        e = wait(c, c.post(f"/api/v1/executions/{e['id']}/resume", json={}).json())
        assert e["status"] == "completed"
        assert "edited" in (Path(e["runtime"]["output"]) / "report.txt").read_text()


def test_optional_langgraph_parallel_reducer_and_recovery(tmp_path):
    pytest.importorskip("langgraph")
    config = tmp_path / "projects.json"
    config.write_text(
        '{"projects": [{"factory": "wtb.testing.console_project:create_langgraph_project"}]}'
    )
    with TestClient(create_app(tmp_path / "console", config)) as c:
        catalog = c.get("/api/v1/catalog").json()
        variant = next(
            v for v in catalog["variants"] if v["workflowVariant"] == "parallel"
        )
        job = c.post(
            "/api/v1/workflows/langgraph-file-workflow/execute",
            json={
                "variantId": variant["id"],
                "state": {"text": "parallel", "delay": 0},
                "breakpoints": ["left", "right"],
            },
        )
        assert job.status_code == 202, job.text
        e = wait(c, job.json())
        assert e["status"] == "paused"
        assert set(e["nextNodes"]) == {"left", "right"}
        before = e["checkpointId"]
        e = wait(c, c.post(f"/api/v1/executions/{e['id']}/resume", json={}).json())
        assert e["status"] == "completed", e
        assert sorted(e["state"]["notes"]) == ["left", "right"]
        assert {n["nodeId"] for n in e["nodeRuns"] if n["status"] == "completed"} == {
            "prepare",
            "left",
            "right",
            "finish",
        }
        e = wait(
            c,
            c.post(
                f"/api/v1/executions/{e['id']}/rollback", json={"checkpointId": before}
            ).json(),
        )
        child = wait(
            c,
            c.post(
                f"/api/v1/executions/{e['id']}/branches", json={"checkpointId": before}
            ).json(),
        )
        child = wait(
            c, c.post(f"/api/v1/executions/{child['id']}/resume", json={}).json()
        )
        assert child["status"] == "completed", child
        assert sorted(child["state"]["notes"]) == ["left", "right"]


def test_console_projection_tracks_real_checkpoints_and_replay_attempts(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        e = wait(c, start(c, state={"text": "loop", "delay": 0, "repeat": 5}))
        projection = c.get("/api/v1/console-state").json()
        assert len(projection["executions"]) == 1
        assert "storeDir" not in projection["executions"][0]
        cps = projection["checkpoints"]
        assert all("state" not in cp and "nodeRuns" not in cp for cp in cps)
        assert len(cps) > len(e["graph"]["nodes"])
        assert (
            next(cp for cp in cps if cp["checkpointId"] == e["checkpointId"])[
                "lastNode"
            ]
            == "finish"
        )
        target = next(
            cp
            for cp in cps
            if cp["nextNodes"] == ["transform"] and cp["lastNode"] == "prepare"
        )
        assert target["tracked"] == 1
        assert target["completedNodes"] == ["prepare"]
        rolled = wait(
            c,
            c.post(
                f"/api/v1/executions/{e['id']}/rollback",
                json={"checkpointId": target["checkpointId"]},
            ).json(),
        )
        assert len(rolled["attempts"]) == 2
        assert rolled["attempts"][-1]["kind"] == "rollback"
        assert rolled["attempts"][-1]["checkpointId"] == target["checkpointId"]
        assert all(
            n["nodeId"] == "prepare"
            for n in rolled["nodeRuns"]
            if n["id"] in rolled["activeRunIds"]
        )


def test_pause_stop_and_query_during_node(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        job = start(c, state={"text": "slow", "delay": 0.5, "repeat": 4})
        id = job["executionId"]
        for _ in range(100):
            e = c.get("/api/v1/executions/" + id).json()
            if e["activeNodes"]:
                break
            time.sleep(0.02)
        paused = c.post(f"/api/v1/executions/{id}/pause", json={})
        assert paused.status_code == 202
        assert c.get("/api/v1/executions/" + id).json()["status"] == "pausing"
        e = wait(c, job)
        assert e["status"] == "paused"
        assert len(e["nodeRuns"]) == 1
        e = wait(c, c.post(f"/api/v1/executions/{id}/stop", json={}).json())
        assert e["status"] == "cancelled"


def test_cas_failure_does_not_move_state_or_overwrite_output(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        e = wait(c, start(c))
        cps = c.get(f"/api/v1/executions/{e['id']}/checkpoints").json()["items"]
        cp = next(p for p in cps if p.get("tracked", 0) > 0)
        files = c.get(
            f"/api/v1/executions/{e['id']}/checkpoints/{cp['checkpointId']}/files"
        ).json()
        digest = files[0]["hash"]
        blob = (
            Path(e["runtime"]["workspace"])
            / ".filetrack"
            / "blobs"
            / digest[:2]
            / digest[2:]
        )
        blob.write_bytes(b"corrupt")
        original = (Path(e["runtime"]["output"]) / "report.txt").read_bytes()
        job = c.post(
            f"/api/v1/executions/{e['id']}/rollback",
            json={"checkpointId": cp["checkpointId"]},
        ).json()
        for _ in range(100):
            op = c.get("/api/v1/operations/" + job["operationId"]).json()
            if op["status"] == "failed":
                break
            time.sleep(0.03)
        assert op["status"] == "failed"
        actual = c.get("/api/v1/executions/" + e["id"]).json()
        assert actual["checkpointId"] == e["checkpointId"]
        assert actual["state"] == e["state"]
        assert (Path(e["runtime"]["output"]) / "report.txt").read_bytes() == original
        assert (
            c.get(
                f"/api/v1/executions/{e['id']}/checkpoints/{cp['checkpointId']}/file",
                params={"path": "../../secret"},
            ).status_code
            == 404
        )


def test_batch_limit_failure_isolation_and_variant(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        response = c.post(
            "/api/v1/batch-tests",
            json={
                "variants": ["file-workflow/transform:uppercase"],
                "inputs": [
                    {"text": "first", "delay": 0.2},
                    {"text": "bad", "fail": True, "delay": 0.2},
                    {"text": "third", "delay": 0.2},
                ],
            },
        )
        assert response.status_code == 202
        batch = response.json()
        for _ in range(200):
            items = c.get(f"/api/v1/batch-tests/{batch['id']}/results").json()["items"]
            assert len([e for e in items if e["status"] == "running"]) <= 2
            if all(e["status"] in ["failed", "completed"] for e in items):
                break
            time.sleep(0.03)
        assert [e["status"] for e in items].count("completed") == 2
        first = items[0]
        assert "FIRST" in (Path(first["runtime"]["output"]) / "report.txt").read_text()
        assert len(c.get("/api/v1/audit/events").json()["items"]) > 0
        assert c.get("/api/v1/system").json()["outbox"] is None


def test_restart_marks_orphans_failed(tmp_path):
    from wtb.application.services.console_service import ConsoleService

    service = ConsoleService(tmp_path)
    service.store.put("execution", {"id": "orphan", "status": "running"})
    service.close()
    with TestClient(create_app(tmp_path)) as c:
        assert c.get("/api/v1/executions/orphan").json()["status"] == "failed"


def test_websocket_and_catalog_empty_config(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        with c.websocket_connect("/ws") as ws:
            result = start(c)
            event = ws.receive_json()
            assert event["executionId"] == result["executionId"]
            assert event["seq"] > 0
        wait(c, result)
    config = tmp_path / "empty.json"
    config.write_text('{"projects": []}')
    with TestClient(create_app(tmp_path / "empty", config)) as c:
        assert c.get("/api/v1/catalog").json()["projects"] == []


def test_catalog_serializes_workflow_project_metadata_and_execution_filters(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        catalog = c.get("/api/v1/catalog").json()
        project = catalog["projects"][0]
        assert project["version"] == 1
        assert project["sdk"]["execution"]["batch_executor"] == "threadpool"
        assert project["sdk"]["pauseStrategy"]["mode"] == "before_node"
        assert project["nodeVariantDetails"]["transform"][0]["name"] == "uppercase"
        assert all(
            v["runtimeBackend"] in {"native", "langgraph"} for v in catalog["variants"]
        )
        execution = wait(c, start(c))
        page = c.get(
            "/api/v1/executions",
            params={"projectId": "file-workflow", "variantId": execution["variantId"]},
        ).json()
        assert page["total"] == 1
        assert page["items"][0]["id"] == execution["id"]
