export type Status = 'queued' | 'running' | 'pausing' | 'paused' | 'completed' | 'failed' | 'cancelled';
export type Mode = 'local' | 'ray';
export type Scenario = 'normal' | 'node_failure' | 'restore_failure' | 'environment_mismatch' | 'actor_reassigned';
export type EnvironmentKind = 'current' | 'venv' | 'reused';
export type Variables = Record<string, unknown>;

export interface ComponentDef {
  id: string;
  name: string;
  description: string;
  version: string;
  tags: string[];
}
export interface GraphNode { id: string; componentId: string; implementation: string; x: number; y: number }
export interface GraphEdge { id: string; source: string; target: string; label?: string; conditional?: boolean }
export interface Variant {
  id: string;
  projectId: string;
  name: string;
  description: string;
  tags: string[];
  nodes: GraphNode[];
  edges: GraphEdge[];
  route: string[];
  config: { model: string; temperature: number; retrieval: string };
}
export interface FileVersion { path: string; content: string }
export interface Checkpoint {
  id: string;
  label: string;
  cursor: number;
  state: Variables;
  files: FileVersion[];
  fileCommitId: string;
  createdAt: string;
  epoch: number;
}
export interface NodeRun {
  id: string;
  nodeId: string;
  visit: number;
  status: 'running' | 'completed' | 'failed';
  elapsedMs: number;
  epoch: number;
}
export interface RuntimeBinding {
  mode: Mode;
  host: string;
  pid: number;
  actorId?: string;
  rayActorId?: string;
  assignmentHistory: { actorId: string; rayActorId: string; reason: string }[];
  cpus: number;
  gpus: number;
  workspace: string;
  output: string;
  checkpoints: string;
  cas: string;
  environment: {
    kind: EnvironmentKind;
    provider: string;
    requestedPath: string;
    actualPython: string;
    version: string;
    match: boolean;
    dependencies: string[];
  };
}
export interface Execution {
  id: string;
  variantId: string;
  graph: Variant;
  status: Status;
  cursor: number;
  progressTicks: number;
  nodeTicks: number;
  elapsedMs: number;
  createdAt: string;
  scenario: Scenario;
  scenarioApplied: boolean;
  state: Variables;
  files: FileVersion[];
  checkpoints: Checkpoint[];
  nodeRuns: NodeRun[];
  runtime: RuntimeBinding;
  breakpoints: string[];
  skipBreakpoint?: string;
  error?: string;
  operationError?: string;
  parentId?: string;
  fromCheckpointId?: string;
  recoveryCheckpointId?: string;
  batchId?: string;
  score?: number;
  epoch: number;
}
export interface Batch {
  id: string;
  name: string;
  executionIds: string[];
  variantIds: string[];
  inputs: string[];
  createdAt: string;
}
export interface AuditEvent {
  id: string;
  executionId?: string;
  kind: 'execution' | 'node' | 'checkpoint' | 'control' | 'runtime' | 'system';
  message: string;
  time: string;
  level: 'info' | 'error';
}
export interface DemoSnapshot {
  revision: number;
  projects: { id: string; name: string; description: string }[];
  components: ComponentDef[];
  variants: Variant[];
  executions: Execution[];
  batches: Batch[];
  events: AuditEvent[];
  system: {
    cacheHits: number;
    cacheMisses: number;
    outboxPending: number;
    outboxProcessed: number;
    integrity: 'unchecked' | 'healthy' | 'issues';
    checkedAt?: string;
    findings: string[];
  };
}
export interface StartOptions {
  variantId: string;
  mode: Mode;
  scenario: Scenario;
  environment: EnvironmentKind;
  input: string;
  model: string;
  temperature: number;
  nodeImplementation: string;
  nodeTicks: number;
}
export type Command =
  | { type: 'pause' | 'resume' | 'stop' | 'checkpoint' }
  | { type: 'rollback'; checkpointId: string }
  | { type: 'fork'; checkpointId: string; state?: Variables }
  | { type: 'edit'; state: Variables }
  | { type: 'breakpoint'; nodeId: string };
export interface OperationResult { ok: boolean; message: string; executionId?: string }

/** A future HTTP adapter can maintain this same subscribed snapshot and command API. */
export interface ConsoleService {
  getSnapshot: () => DemoSnapshot;
  subscribe: (listener: () => void) => () => void;
  start(options: StartOptions): OperationResult;
  startBatch(variantIds: string[], inputs: string[], options: StartOptions): OperationResult;
  command(executionId: string, command: Command): OperationResult;
  reset(): void;
  tick(): void;
  startClock(): () => void;
  systemAction(action: 'check' | 'outbox' | 'cache'): void;
}

export const statusNames: Record<Status, string> = {
  queued: '排队中', running: '运行中', pausing: '暂停请求中', paused: '已暂停',
  completed: '已完成', failed: '失败', cancelled: '已停止',
};
export const scenarioNames: Record<Scenario, string> = {
  normal: '正常执行', node_failure: '节点失败', restore_failure: '文件恢复失败',
  environment_mismatch: '环境不匹配', actor_reassigned: 'Actor 重新分配',
};
export const busy = (e: Execution) => e.status === 'running' || e.status === 'pausing';
export const terminal = (e: Execution) => ['completed', 'failed', 'cancelled'].includes(e.status);

export function commandReason(e: Execution, type: Command['type']): string | undefined {
  if (type === 'pause' && e.status !== 'running') return '仅运行中的执行可请求暂停';
  if (type === 'resume' && e.status !== 'paused') return '请先暂停或回退到检查点';
  if (type === 'stop' && terminal(e)) return '执行已结束';
  if (['rollback', 'fork'].includes(type) && (busy(e) || e.status === 'queued')) return '请先暂停执行，再选择历史检查点';
  if (['edit', 'checkpoint'].includes(type) && e.status !== 'paused') return '请先暂停，在安全边界操作';
  return undefined;
}
