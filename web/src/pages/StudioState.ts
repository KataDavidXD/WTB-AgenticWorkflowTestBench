import type {
  AuditEvent,
  Execution,
  Checkpoint as ApiCheckpoint,
} from "../domain";
import {
  byVariant,
  COMPONENTS,
  nodes,
  variants,
  type NodeId,
  type RunStatus,
  type StudioVariant,
} from "./StudioDemoData";
import type { Projection } from "./StudioTypes";
export interface Checkpoint {
  id: string;
  runId: string;
  trackId: string;
  step: number;
  node: NodeId | "∅";
  cas: string;
  tracked: number;
  restorable: boolean;
  clock: number;
  completedNodes: string[];
}
export interface Attempt {
  id: string;
  runId: string;
  baseStep: number;
  origin: {
    runId: string;
    trackId: string;
    cpId: string;
    type: "fork" | "rollback";
  } | null;
  cps: string[];
  active: boolean;
  kind: "initial" | "fork" | "rollback";
  label: string;
}
export interface DemoRun {
  id: string;
  variantId: string;
  mode: "ray" | "local";
  status: RunStatus;
  step: number;
  trackId: string;
  path: string[];
  events: { kind: string; text: string; at: string }[];
  created: number;
  failedNode: NodeId | null;
  runtime?: Execution["runtime"];
  checkpointId?: string;
  activeNodes: string[];
  nextNodes: string[];
  completedNodes: string[];
  inheritedNodes: string[];
}
export interface DemoState {
  schema: number;
  view: Projection;
  selectedRun: string;
  selectedVariantId: string;
  selectedNode: NodeId;
  selectedCp: string;
  focus: boolean;
  onlyFamily: boolean;
  xDim: number;
  yDim: number;
  baseline: string;
  runCounter: number;
  cpCounter: number;
  trackCounter: number;
  clock: number;
  runs: DemoRun[];
  tracks: Attempt[];
  checkpoints: Record<string, Checkpoint>;
}
export function emptyState(): DemoState {
  return {
    schema: 5,
    view: "loom",
    selectedRun: "",
    selectedVariantId: "",
    selectedNode: "",
    selectedCp: "",
    focus: false,
    onlyFamily: true,
    xDim: 0,
    yDim: 1,
    baseline: "",
    runCounter: 0,
    cpCounter: 0,
    trackCounter: 0,
    clock: 0,
    runs: [],
    tracks: [],
    checkpoints: {},
  };
}
const idleRun = (variantId: string): DemoRun => ({
  id: "",
  variantId,
  mode: "local",
  status: "idle",
  step: 0,
  trackId: "",
  path: [],
  events: [],
  created: 0,
  failedNode: null,
  activeNodes: [],
  nextNodes: [],
  completedNodes: [],
  inheritedNodes: [],
});
const emptyCheckpoint: Checkpoint = {
  id: "",
  runId: "",
  trackId: "",
  step: 0,
  node: "∅",
  cas: "尚无检查点",
  tracked: 0,
  restorable: false,
  clock: 0,
  completedNodes: [],
};
export function hydrateState(
  previous: DemoState | undefined,
  executions: Execution[],
  checkpointsByRun: Record<string, ApiCheckpoint[]>,
  events: AuditEvent[] = [],
): DemoState {
  const checkpoints: Record<string, Checkpoint> = {},
    tracks: Attempt[] = [];
  for (const e of executions) {
    const cps = [...(checkpointsByRun[e.id] || [])].sort(
      (a, b) =>
        a.createdAt.localeCompare(b.createdAt) ||
        a.checkpointId.localeCompare(b.checkpointId),
    );
    cps.forEach((cp, index) => {
      checkpoints[cp.checkpointId] = {
        id: cp.checkpointId,
        runId: e.id,
        trackId: cp.attemptId || e.attempts?.[0]?.id || `track-${e.id}`,
        step: cp.step,
        node: cp.lastNode || "∅",
        cas: cp.fileCommitId,
        tracked: cp.tracked ?? 0,
        restorable: !!cp.fileCommitId,
        clock: index,
        completedNodes: cp.completedNodes || [],
      };
    });
  }
  const runs = executions
    .map((e): DemoRun => {
      const cps = Object.values(checkpoints)
        .filter((c) => c.runId === e.id)
        .sort((a, b) => a.clock - b.clock);
      const attempts = e.attempts?.length
        ? e.attempts
        : [
            {
              id: `track-${e.id}`,
              kind: e.parentId ? ("fork" as const) : ("initial" as const),
              checkpointId: e.fromCheckpointId,
            },
          ];
      attempts.forEach((a, index) => {
        const source = checkpoints[a.checkpointId || ""];
        const origin = source
          ? {
              runId: source.runId,
              trackId: source.trackId,
              cpId: source.id,
              type:
                a.kind === "fork" ? ("fork" as const) : ("rollback" as const),
            }
          : null;
        tracks.push({
          id: a.id,
          runId: e.id,
          baseStep: source?.clock || 0,
          origin,
          cps: cps.filter((c) => c.trackId === a.id).map((c) => c.id),
          active: index === attempts.length - 1,
          kind: a.kind,
          label:
            a.kind === "fork"
              ? "Fork · 独立工作区"
              : a.kind === "rollback"
                ? "回退后继续"
                : "初次执行",
        });
      });
      const active = (e.nodeRuns || []).filter(
          (r) => !e.activeRunIds || e.activeRunIds.includes(r.id),
        ),
        completed = active.filter((r) => r.status === "completed");
      const inherited = e.parentId
        ? checkpoints[e.checkpointId || ""]?.completedNodes.filter(
            (n) => !completed.some((r) => r.nodeId === n),
          ) || []
        : [];
      return {
        id: e.id,
        variantId: e.variantId,
        mode: e.runtime?.mode === "ray" ? "ray" : "local",
        status: e.status === "pending" ? "queued" : e.status,
        step: completed.length,
        trackId: attempts.at(-1)!.id,
        path: cps.map((c) => c.id),
        events: events
          .filter((event) => event.executionId === e.id)
          .sort((a, b) => a.seq - b.seq)
          .map((event) => ({
            kind: event.kind,
            text: event.message,
            at: event.time.slice(11, 19),
          })),
        created: Date.parse(e.createdAt),
        failedNode:
          [...active].reverse().find((r) => r.status === "failed")?.nodeId ||
          null,
        runtime: e.runtime,
        checkpointId: e.checkpointId,
        activeNodes: e.activeNodes || [],
        nextNodes: e.nextNodes || [],
        completedNodes: [...new Set(completed.map((r) => r.nodeId))],
        inheritedNodes: inherited,
      };
    })
    .sort((a, b) => b.created - a.created || a.id.localeCompare(b.id));
  const selectedVariantId = byVariant(previous?.selectedVariantId || "")
    ? previous!.selectedVariantId
    : runs[0]?.variantId || variants[0]?.id || "";
  const current =
      runs.find(
        (r) =>
          r.id === previous?.selectedRun && r.variantId === selectedVariantId,
      ) || runs.find((r) => r.variantId === selectedVariantId),
    selectedCp = current?.path.includes(previous?.selectedCp || "")
      ? previous!.selectedCp
      : "";
  const maxDim = Math.max(0, COMPONENTS.length - 1);
  return {
    ...(previous || emptyState()),
    selectedVariantId,
    selectedRun: current?.id || "",
    selectedCp,
    selectedNode: COMPONENTS.some((c) => c.id === previous?.selectedNode)
      ? previous!.selectedNode
      : COMPONENTS[0]?.id || "",
    xDim: Math.min(previous?.xDim || 0, maxDim),
    yDim: Math.min(previous?.yDim ?? 1, maxDim),
    baseline: byVariant(previous?.baseline || "")
      ? previous!.baseline
      : selectedVariantId,
    runs,
    tracks,
    checkpoints,
    runCounter: runs.length,
    cpCounter: Object.keys(checkpoints).length,
  };
}
export const selectedRun = (s: DemoState) =>
  s.runs.find(
    (r) => r.id === s.selectedRun && r.variantId === s.selectedVariantId,
  ) || idleRun(s.selectedVariantId);
export const selectedVariant = (s: DemoState): StudioVariant =>
  byVariant(s.selectedVariantId) ||
  variants[0] || {
    id: "",
    name: "无已注册变体",
    label: "",
    cfg: [],
    links: [],
    extra: [],
    tags: [],
  };
export const selectedAttempt = (s: DemoState, r = selectedRun(s)) =>
  s.tracks.find((t) => t.id === r.trackId);
export const selectedCheckpoint = (s: DemoState) =>
  s.checkpoints[s.selectedCp || selectedRun(s).checkpointId || ""] ||
  emptyCheckpoint;
export const latestRun = (s: DemoState, v: StudioVariant) =>
  s.runs
    .filter((r) => r.variantId === v.id)
    .sort((a, b) => b.created - a.created)[0] || idleRun(v.id);
export const currentNode = (r: DemoRun) =>
  r.activeNodes[0] || r.nextNodes[0] || "";
export function nodeStatus(
  s: DemoState,
  node: NodeId,
  r = selectedRun(s),
):
  | "skipped"
  | "restored"
  | "done"
  | "failed"
  | "running"
  | "paused"
  | "queued" {
  if (!byVariant(r.variantId) || !nodes(byVariant(r.variantId)).includes(node))
    return "skipped";
  if (s.selectedCp && s.selectedRun === r.id && s.selectedCp !== r.checkpointId)
    return selectedCheckpoint(s).completedNodes.includes(node)
      ? "restored"
      : "queued";
  if (r.activeNodes.includes(node)) return "running";
  if (r.status === "failed" && r.failedNode === node) return "failed";
  if (r.status === "paused" && r.nextNodes.includes(node)) return "paused";
  if (r.completedNodes.includes(node)) return "done";
  if (r.inheritedNodes.includes(node)) return "restored";
  return "queued";
}
export function nodeContext(
  s: DemoState,
  node = s.selectedNode,
  r = selectedRun(s),
) {
  const v = selectedVariant(s),
    status = nodeStatus(s, node, r),
    runtime = r.runtime,
    allocated =
      !!runtime && !["queued", "skipped", "restored"].includes(status);
  return {
    c: node,
    cfg: v?.cfg[COMPONENTS.findIndex((c) => c.id === node)],
    ns: status,
    host: allocated ? runtime!.host : "未分配",
    actor: allocated ? `pid ${runtime!.pid}` : "未分配",
    allocated,
    workspace: runtime?.workspace || "未启动执行",
    workdir: runtime?.output || "未分配",
    envId: runtime?.environment || "未分配",
    envPath: allocated ? runtime!.interpreter : null,
    attempt: s.tracks.filter((t) => t.runId === r.id).length,
    mode: r.mode,
    envSpec: runtime?.pythonVersion || "未提供",
    inherited: status === "restored",
  };
}
export type DemoAction =
  | { type: "view"; view: Projection }
  | { type: "run"; id: string }
  | { type: "variant"; id: string }
  | { type: "node"; id: NodeId }
  | { type: "checkpoint"; id: string; runId?: string }
  | { type: "focus"; value: boolean }
  | { type: "axis"; axis: "xDim" | "yDim"; value: number }
  | { type: "baseline"; id: string }
  | { type: "pause" | "step"; runId?: string }
  | { type: "restore"; kind: "fork" | "rollback"; cpId: string; runId: string }
  | { type: "reset" };
export function studioReducer(
  previous: DemoState,
  action: DemoAction,
): DemoState {
  const s = structuredClone(previous);
  if (action.type === "view") s.view = action.view;
  if (action.type === "run") {
    const r = s.runs.find((r) => r.id === action.id);
    if (r) {
      s.selectedRun = r.id;
      s.selectedVariantId = r.variantId;
      s.selectedCp = "";
    }
  }
  if (action.type === "variant") {
    s.selectedVariantId = action.id;
    s.selectedRun =
      s.runs
        .filter((r) => r.variantId === action.id)
        .sort((a, b) => b.created - a.created)[0]?.id || "";
    s.selectedCp = "";
  }
  if (action.type === "node") s.selectedNode = action.id;
  if (action.type === "checkpoint") {
    const cp = s.checkpoints[action.id];
    if (!action.id) s.selectedCp = "";
    else if (cp) {
      s.selectedCp = cp.id;
      s.selectedRun = cp.runId;
      s.selectedVariantId = s.runs.find((r) => r.id === cp.runId)!.variantId;
      s.selectedNode = cp.node === "∅" ? COMPONENTS[0]?.id || "" : cp.node;
    }
  }
  if (action.type === "focus") s.focus = action.value;
  if (action.type === "baseline") s.baseline = action.id;
  if (action.type === "axis") s[action.axis] = action.value;
  return s;
}
