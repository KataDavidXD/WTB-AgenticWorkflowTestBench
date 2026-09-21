"""Real local API profile. Run: python -m wtb.api.console --data-dir data/console."""

import argparse
import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from wtb.application.services.console_service import Conflict, ConsoleService


class StartRequest(BaseModel):
    variantId: str
    state: dict = Field(default_factory=dict)
    breakpoints: list[str] = Field(default_factory=list)
    nodeVariants: dict[str, str] = Field(default_factory=dict)


class BatchRequest(BaseModel):
    variants: list[str]
    inputs: list[dict]


def create_app(data_dir=None, config_path=None):
    clients: set[asyncio.Queue] = set()

    @asynccontextmanager
    async def lifespan(app):
        service = ConsoleService(
            data_dir or os.getenv("WTB_CONSOLE_DATA", "data/console"),
            config_path or os.getenv("WTB_CONSOLE_CONFIG"),
        )
        app.state.console = service
        loop = asyncio.get_running_loop()

        def broadcast(event):
            for queue in list(clients):
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(event)

        service.notify = lambda event: loop.call_soon_threadsafe(broadcast, event)
        try:
            yield
        finally:
            await asyncio.to_thread(service.close)
            service.notify = lambda event: None

    app = FastAPI(title="WTB Local Console", lifespan=lifespan)

    @app.exception_handler(KeyError)
    async def missing(_, error):
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(ValueError)
    async def invalid(_, error):
        return JSONResponse(
            status_code=409 if isinstance(error, Conflict) else 400,
            content={"detail": str(error)},
        )

    def service():
        return app.state.console

    @app.get("/api/v1/catalog")
    def catalog():
        return service().catalog()

    @app.get("/api/v1/health")
    def health():
        return {"status": "ready", "mode": "local", "mock": False}

    @app.get("/api/v1/console-state")
    def console_state():
        # One projection replaces O(runs) requests per node event.
        with service().lock:
            executions = [
                {k: v for k, v in e.items() if k not in {"storeDir", "graphPath"}}
                for e in service().store.list("execution")
            ]
            checkpoints = [
                {k: v for k, v in c.items() if k not in {"state", "nodeRuns"}}
                for c in service().store.list("checkpoint")
            ]
            return {
                "catalog": service().catalog(),
                "executions": executions,
                "checkpoints": checkpoints,
                "events": service().store.events(limit=200)["items"],
            }

    @app.get("/api/v1/executions")
    def executions(
        projectId: str | None = None,
        variantId: str | None = None,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        items = service().store.list("execution")
        if projectId:
            items = [item for item in items if item.get("projectId") == projectId]
        if variantId:
            items = [item for item in items if item.get("variantId") == variantId]
        return {
            "items": [
                {
                    k: v
                    for k, v in e.items()
                    if k not in {"nodeRuns", "state", "storeDir", "graphPath"}
                }
                for e in items[offset : offset + limit]
            ],
            "total": len(items),
        }

    @app.post("/api/v1/workflows/{project}/execute", status_code=202)
    def start(project: str, body: StartRequest):
        if not body.variantId.startswith(project + "/"):
            raise ValueError("项目与变体不匹配")
        return service().start(
            body.variantId, body.state, body.breakpoints, body.nodeVariants
        )

    @app.get("/api/v1/executions/{id}")
    def execution(id: str):
        e = service().store.get("execution", id)
        return {k: v for k, v in e.items() if k not in {"storeDir", "graphPath"}}

    @app.get("/api/v1/operations/{id}")
    def operation(id: str):
        return service().store.get("operation", id)

    @app.get("/api/v1/executions/{id}/checkpoints")
    def checkpoints(
        id: str, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)
    ):
        return service().checkpoints(id, limit, offset)

    @app.get("/api/v1/executions/{id}/checkpoints/{cp}")
    def checkpoint(id: str, cp: str):
        return service().store.get("checkpoint", id + ":" + cp)

    @app.get("/api/v1/executions/{id}/checkpoints/{cp}/files")
    def files(id: str, cp: str):
        return service().files(id, cp)

    @app.get("/api/v1/executions/{id}/checkpoints/{cp}/file")
    def file(id: str, cp: str, path: str):
        return service().files(id, cp, path)

    @app.get("/api/v1/executions/{id}/branches")
    def branches(id: str):
        service().store.get("execution", id)
        return [e for e in service().store.list("execution") if e.get("parentId") == id]

    @app.post("/api/v1/executions/{id}/{action}", status_code=202)
    def command(id: str, action: str, body: dict):
        aliases = {"branches": "fork", "checkpoints": "checkpoint", "state": "edit"}
        return service().command(id, aliases.get(action, action), body)

    @app.post("/api/v1/batch-tests", status_code=202)
    def batch(body: BatchRequest):
        return service().batch(body.variants, body.inputs)

    @app.get("/api/v1/batch-tests")
    def batches():
        return service().store.list("batch")

    @app.get("/api/v1/batch-tests/{id}/results")
    def batch_results(id: str):
        batch = service().store.get("batch", id)
        items = [service().store.get("execution", eid) for eid in batch["executionIds"]]
        return {
            "items": [
                {
                    k: v
                    for k, v in e.items()
                    if k not in {"storeDir", "graphPath", "state", "nodeRuns"}
                }
                for e in items
            ],
            "total": len(items),
        }

    @app.get("/api/v1/audit/events")
    def events(
        executionId: str | None = None,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        return service().store.events(executionId, limit, offset)

    @app.get("/api/v1/system")
    def system():
        return service().system()

    @app.post("/api/v1/system/integrity")
    def integrity():
        return service().check_integrity()

    @app.websocket("/ws")
    async def socket(ws: WebSocket):
        origin = ws.headers.get("origin", "")
        if origin and not any(
            origin.startswith(prefix)
            for prefix in ("http://127.0.0.1:", "http://localhost:")
        ):
            await ws.close(code=1008)
            return
        await ws.accept()
        queue = asyncio.Queue(maxsize=100)
        clients.add(queue)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    event = {"kind": "heartbeat"}
                await ws.send_json(event)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            clients.discard(queue)

    return app


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data/console")
    parser.add_argument("--config")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(
        create_app(args.data_dir, args.config), host="127.0.0.1", port=args.port
    )
