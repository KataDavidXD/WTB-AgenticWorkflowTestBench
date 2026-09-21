import { beforeEach, describe, expect, it } from "vitest";
import type { Checkpoint, Execution, Variant } from "../domain";
import { configureStudioCatalog, variants } from "./StudioDemoData";
import {
  emptyState,
  hydrateState,
  latestRun,
  nodeStatus,
  selectedCheckpoint,
  selectedRun,
  selectedVariant,
  studioReducer,
} from "./StudioState";

const catalog = ["default", "other"].map((name) => ({
  id: `project/${name}`,
  name,
  description: "",
  workflowVariant: null,
  nodes: ["prepare", "transform", "finish"].map((id) => ({
    id,
    implementation: id,
  })),
  edges: [],
}));
function execution(id = "run", changes: Partial<Execution> = {}): Execution {
  return {
    id,
    variantId: "project/default",
    projectId: "project",
    graph: {} as Variant,
    status: "queued",
    createdAt: "2026-01-01T00:00:00Z",
    state: {},
    nodeRuns: [],
    activeNodes: [],
    nextNodes: [],
    breakpoints: [],
    elapsedMs: 0,
    runtime: {
      mode: "local",
      host: "localhost",
      pid: 1,
      workspace: "/tmp/run",
      output: "/tmp/run/output",
      interpreter: "python",
      pythonVersion: "3.12",
      environment: "native",
      pathLocation: "local",
    },
    ...changes,
  };
}
function checkpoint(id: string, changes: Partial<Checkpoint> = {}): Checkpoint {
  return {
    id: `run:${id}`,
    checkpointId: id,
    executionId: "run",
    state: {},
    nextNodes: [],
    nodeRuns: [],
    fileCommitId: `cas-${id}`,
    createdAt: "2026-01-01T00:00:01Z",
    step: 0,
    ...changes,
  };
}
beforeEach(() => configureStudioCatalog(catalog));
describe("real console projection", () => {
  it("keeps an empty or queued execution renderable before the first checkpoint", () => {
    expect(selectedCheckpoint(hydrateState(undefined, [], {})).restorable).toBe(
      false,
    );
    const state = hydrateState(undefined, [execution()], {});
    expect(selectedRun(state).id).toBe("run");
    expect(selectedCheckpoint(state).id).toBe("");
  });
  it("renders an empty project configuration safely", () => {
    configureStudioCatalog([]);
    expect(selectedVariant(emptyState()).cfg).toEqual([]);
  });
  it("does not borrow an execution from another variant", () => {
    const state = studioReducer(hydrateState(undefined, [execution()], {}), {
      type: "variant",
      id: "project/other",
    });
    expect(selectedRun(state).status).toBe("idle");
    expect(selectedRun(hydrateState(state, [execution()], {})).id).toBe("");
    expect(latestRun(state, variants[1]).id).toBe("");
  });
  it("selects the latest execution independently of server list order", () => {
    const state = hydrateState(
      undefined,
      [
        execution("old"),
        execution("new", { createdAt: "2026-01-02T00:00:00Z" }),
      ],
      {},
    );
    expect(latestRun(state, variants[0]).id).toBe("new");
  });
  it("does not mark an unexecuted finish node complete in a loop", () => {
    const e = execution("run", {
      status: "paused",
      nextNodes: ["transform"],
      nodeRuns: Array.from({ length: 4 }, (_, i) => ({
        id: `n${i}`,
        taskId: `n${i}`,
        nodeId: i ? "transform" : "prepare",
        status: "completed",
        startedAt: "",
        elapsedMs: 0,
        step: i,
      })),
    });
    const state = hydrateState(undefined, [e], {});
    expect(nodeStatus(state, "prepare")).toBe("done");
    expect(nodeStatus(state, "transform")).toBe("paused");
    expect(nodeStatus(state, "finish")).toBe("queued");
  });
  it("shows simultaneous active nodes using runtime IDs", () => {
    const state = hydrateState(
      undefined,
      [
        execution("run", {
          status: "running",
          activeNodes: ["prepare", "transform"],
        }),
      ],
      {},
    );
    expect(nodeStatus(state, "prepare")).toBe("running");
    expect(nodeStatus(state, "transform")).toBe("running");
    expect(nodeStatus(state, "finish")).toBe("queued");
  });
  it("excludes abandoned node records after rollback", () => {
    const state = hydrateState(
      undefined,
      [
        execution("run", {
          status: "paused",
          nextNodes: ["transform"],
          activeRunIds: [],
          nodeRuns: [
            {
              id: "old",
              taskId: "old",
              nodeId: "finish",
              status: "completed",
              startedAt: "",
              elapsedMs: 0,
              step: 9,
            },
          ],
        }),
      ],
      {},
    );
    expect(nodeStatus(state, "finish")).toBe("queued");
    expect(selectedRun(state).step).toBe(0);
  });
  it("uses persisted checkpoint identity, order and file counts", () => {
    const cps = [
      checkpoint("latest", {
        createdAt: "2026-01-01T00:00:03Z",
        lastNode: "finish",
        tracked: 2,
      }),
      checkpoint("first", { lastNode: "prepare", tracked: 1 }),
    ];
    const state = hydrateState(
      undefined,
      [execution("run", { checkpointId: "latest" })],
      { run: cps },
    );
    expect(selectedCheckpoint(state)).toMatchObject({
      id: "latest",
      node: "finish",
      tracked: 2,
      clock: 1,
    });
    expect(selectedRun(state).path).toEqual(["first", "latest"]);
  });
  it("resolves fork origins even when child is returned before parent", () => {
    const child = execution("child", {
      parentId: "run",
      fromCheckpointId: "origin",
      checkpointId: "child-cp",
      attempts: [{ id: "fork", kind: "fork", checkpointId: "origin" }],
    });
    const state = hydrateState(undefined, [child, execution()], {
      run: [checkpoint("origin")],
      child: [
        checkpoint("child-cp", {
          executionId: "child",
          attemptId: "fork",
          completedNodes: ["prepare"],
        }),
      ],
    });
    expect(state.tracks.find((t) => t.id === "fork")?.origin?.runId).toBe(
      "run",
    );
    expect(state.runs.find((r) => r.id === "child")?.inheritedNodes).toEqual([
      "prepare",
    ]);
  });
  it("preserves rollback attempt history and clamps stale axes", () => {
    const state = hydrateState(
      { ...emptyState(), xDim: 50, yDim: 80 },
      [
        execution("run", {
          attempts: [
            { id: "initial", kind: "initial" },
            { id: "replay", kind: "rollback", checkpointId: "origin" },
          ],
        }),
      ],
      { run: [checkpoint("origin", { attemptId: "initial" })] },
    );
    expect(state.tracks).toHaveLength(2);
    expect(state.tracks[1].origin?.type).toBe("rollback");
    expect(state.xDim).toBe(2);
    expect(state.yDim).toBe(2);
  });
});
