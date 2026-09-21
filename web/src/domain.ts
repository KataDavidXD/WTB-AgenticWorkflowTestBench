export type Status =
  | "queued"
  | "pending"
  | "running"
  | "pausing"
  | "stopping"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled";
export type Variables = Record<string, unknown>;
export interface ComponentDef {
  id: string;
  name: string;
  description: string;
  version: string | null;
  tags: string[];
}
export interface GraphNode {
  id: string;
  componentId: string;
  implementation: string;
  x: number;
  y: number;
}
export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  label?: string;
  conditional: boolean;
}
export interface Variant {
  runtimeBackend?: string;
  capabilities?: Record<string, boolean>;
  id: string;
  projectId: string;
  name: string;
  description: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  workflowVariant: string | null;
  nodeVariants: Record<string, string>;
}
export interface Project {
  id: string;
  name: string;
  description: string;
  initialState: Variables;
  nodeVariants: Record<string, string[]>;
}
export interface Catalog {
  projects: Project[];
  variants: Variant[];
  components: ComponentDef[];
  capabilities: Record<string, boolean>;
  unavailable: Record<string, string>;
}
export interface NodeRun {
  id: string;
  taskId: string;
  nodeId: string;
  status: "running" | "completed" | "failed";
  startedAt: string;
  elapsedMs: number;
  step: number;
  error?: string;
}
export interface Checkpoint {
  tracked?: number;
  lastNode?: string;
  completedNodes?: string[];
  attemptId?: string;
  id: string;
  checkpointId: string;
  executionId: string;
  state: Variables;
  nextNodes: string[];
  nodeRuns: NodeRun[];
  fileCommitId: string;
  createdAt: string;
  step: number;
  parentCheckpointId?: string;
}
export interface RuntimeBinding {
  mode: "local" | "ray";
  host: string;
  pid: number;
  workspace: string;
  output: string;
  interpreter: string;
  pythonVersion: string;
  environment: string;
  pathLocation: string;
}
export interface Execution {
  capabilities?: Record<string, boolean>;
  attempts?: {
    id: string;
    kind: "initial" | "fork" | "rollback";
    checkpointId?: string;
  }[];
  id: string;
  variantId: string;
  projectId: string;
  graph: Variant;
  status: Status;
  createdAt: string;
  state: Variables;
  nodeRuns: NodeRun[];
  activeRunIds?: string[];
  activeNodes: string[];
  nextNodes: string[];
  checkpointId?: string;
  breakpoints: string[];
  elapsedMs: number;
  runtime: RuntimeBinding;
  pendingOperation?: string;
  error?: string;
  operationError?: string;
  parentId?: string;
  fromCheckpointId?: string;
  batchId?: string;
}
export interface FileVersion {
  path: string;
  hash: string;
  size: number;
}
export interface AuditEvent {
  id: string;
  seq: number;
  executionId?: string;
  kind: string;
  message: string;
  time: string;
}
export interface Batch {
  id: string;
  createdAt: string;
  executionIds: string[];
}
export interface Page<T> {
  items: T[];
  total: number;
}
export interface OperationResult {
  executionId: string;
  operationId: string;
}
export interface Operation {
  id: string;
  status: string;
  error?: string;
  resultExecutionId?: string;
}
export interface Snapshot {
  catalog?: Catalog;
  executions: Execution[];
  total: number;
  offset: number;
  connection: "connecting" | "live" | "polling" | "offline";
  loading: boolean;
  error?: string;
  revision: number;
}
export interface ConsoleService {
  getSnapshot: () => Snapshot;
  subscribe: (listener: () => void) => () => void;
  connect(): () => void;
  refresh(): Promise<void>;
  start(
    project: string,
    variantId: string,
    state: Variables,
    breakpoints: string[],
    nodeVariants: Record<string, string>,
  ): Promise<OperationResult>;
  command(
    id: string,
    action: string,
    payload?: Variables,
  ): Promise<OperationResult>;
}
export const statusNames: Record<Status, string> = {
  queued: "排队中",
  pending: "待执行",
  running: "运行中",
  pausing: "暂停请求中",
  stopping: "停止请求中",
  paused: "已暂停",
  completed: "已完成",
  failed: "失败",
  cancelled: "已停止",
};
