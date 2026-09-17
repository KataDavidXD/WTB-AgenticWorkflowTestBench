import { byVariant, COMPONENTS, nodes, type NodeId, type RunStatus, type StudioVariant } from './StudioDemoData';
import type { Projection } from './StudioTypes';

export interface Checkpoint { id: string; runId: string; trackId: string; step: number; node: NodeId | '∅'; cas: string; tracked: number; restorable: boolean; clock: number }
export interface Attempt { id: string; runId: string; baseStep: number; origin: { runId: string; trackId: string; cpId: string; type: 'fork' | 'rollback' } | null; cps: string[]; active: boolean; kind: 'initial' | 'fork' | 'rollback'; label: string }
export interface DemoRun { id: string; variantId: string; mode: 'ray' | 'local'; status: RunStatus; step: number; trackId: string; path: string[]; events: { kind: string; text: string; at: string }[]; created: number; failedNode: NodeId | null; runtime?: { host: string; pid: number; workspace: string; output: string; interpreter: string; pythonVersion: string; environment: string } }
export interface DemoState { schema: number; view: Projection; selectedRun: string; selectedNode: NodeId; selectedCp: string; focus: boolean; onlyFamily: boolean; xDim: number; yDim: number; baseline: string; runCounter: number; cpCounter: number; trackCounter: number; clock: number; runs: DemoRun[]; tracks: Attempt[]; checkpoints: Record<string, Checkpoint> }
type ApiExecution = { id: string; variantId: string; status: RunStatus; createdAt: string; nodeRuns?: { nodeId: string; status: string; step: number; error?: string }[]; nextNodes?: string[]; checkpointId?: string; parentId?: string; fromCheckpointId?: string; runtime?: DemoRun['runtime'] };
type ApiCheckpoint = { checkpointId: string; step: number; nextNodes: string[]; fileCommitId: string; createdAt: string };

export function emptyState(): DemoState { const first = COMPONENTS[0]?.id || ''; return { schema: 4, view: 'loom', selectedRun: '', selectedNode: first, selectedCp: '', focus: false, onlyFamily: true, xDim: Math.min(1, Math.max(0, COMPONENTS.length - 1)), yDim: Math.min(2, Math.max(0, COMPONENTS.length - 1)), baseline: '', runCounter: 0, cpCounter: 0, trackCounter: 0, clock: 0, runs: [], tracks: [], checkpoints: {} }; }
export function hydrateState(previous: DemoState | undefined, executions: ApiExecution[], checkpointsByRun: Record<string, ApiCheckpoint[]>) {
  const first = COMPONENTS[0]?.id || '';
  const checkpoints: Record<string, Checkpoint> = {};
  const tracks: Attempt[] = [];
  const runs = executions.map((execution, index): DemoRun => {
    const cps = (checkpointsByRun[execution.id] || []).map((checkpoint, cpIndex) => {
      const node = checkpoint.step ? (byVariant(execution.variantId) ? nodes(byVariant(execution.variantId))[checkpoint.step - 1] || '∅' : '∅') : '∅';
      checkpoints[checkpoint.checkpointId] = { id: checkpoint.checkpointId, runId: execution.id, trackId: `track-${execution.id}`, step: checkpoint.step, node, cas: checkpoint.fileCommitId, tracked: 0, restorable: true, clock: cpIndex };
      return checkpoint.checkpointId;
    });
    const completed = execution.nodeRuns?.filter(run => run.status === 'completed').length || 0;
    const failed = execution.nodeRuns?.find(run => run.status === 'failed')?.nodeId || null;
    const origin = execution.parentId && execution.fromCheckpointId ? { runId: execution.parentId, trackId: `track-${execution.parentId}`, cpId: execution.fromCheckpointId, type: 'fork' as const } : null;
    tracks.push({ id: `track-${execution.id}`, runId: execution.id, baseStep: origin ? (checkpoints[origin.cpId]?.step || 0) : 0, origin, cps, active: true, kind: origin ? 'fork' : 'initial', label: origin ? 'Fork · 独立工作区' : '初次执行' });
    return { id: execution.id, variantId: execution.variantId, mode: 'local', status: execution.status, step: completed, trackId: `track-${execution.id}`, path: cps, events: [{ kind: 'execution', text: `${execution.status} · ${execution.createdAt}`, at: execution.createdAt.slice(11, 19) }], created: index, failedNode: failed, runtime: execution.runtime };
  });
  const selectedRun = runs.some(run => run.id === previous?.selectedRun) ? previous!.selectedRun : runs[0]?.id || '';
  const current = runs.find(run => run.id === selectedRun);
  const selectedCp = current?.path.includes(previous?.selectedCp || '') ? previous!.selectedCp : current?.path.at(-1) || '';
  const selectedNode = COMPONENTS.some(component => component.id === previous?.selectedNode) ? previous!.selectedNode : first;
  return { ...(previous || emptyState()), selectedRun, selectedCp, selectedNode, baseline: previous?.baseline && byVariant(previous.baseline) ? previous.baseline : runs[0]?.variantId || '', runs, tracks, checkpoints, runCounter: runs.length, cpCounter: Object.keys(checkpoints).length };
}
export const selectedRun = (state: DemoState) => state.runs.find(run => run.id === state.selectedRun) || state.runs[0]!;
export const selectedVariant = (state: DemoState) => byVariant(selectedRun(state).variantId);
export const selectedAttempt = (state: DemoState, run = selectedRun(state)) => state.tracks.find(track => track.id === run.trackId) || state.tracks[0]!;
export const selectedCheckpoint = (state: DemoState) => state.checkpoints[state.selectedCp] || state.checkpoints[selectedRun(state)?.path.at(-1) || ''];
export const latestRun = (state: DemoState, variant: StudioVariant) => state.runs.filter(run => run.variantId === variant.id).sort((a, b) => b.created - a.created)[0] || state.runs[0]!;
export const currentNode = (run: DemoRun) => nodes(byVariant(run.variantId))[run.step] || nodes(byVariant(run.variantId)).at(-1) || COMPONENTS[0]?.id || '';
export function nodeStatus(state: DemoState, node: NodeId, run = selectedRun(state)): 'skipped' | 'restored' | 'done' | 'failed' | 'running' | 'paused' | 'queued' { const index = nodes(byVariant(run.variantId)).indexOf(node); if (index < 0) return 'skipped'; if (run.failedNode === node) return 'failed'; if (index < run.step) return index < selectedAttempt(state, run).baseStep ? 'restored' : 'done'; if (run.runtime && ['running', 'pausing'].includes(run.status) && index === run.step) return 'running'; if (run.status === 'paused' && index === run.step) return 'paused'; return 'queued'; }
export function nodeContext(state: DemoState, node = state.selectedNode, run = selectedRun(state)) { const status = nodeStatus(state, node, run), runtime = run.runtime; return { c: node, cfg: byVariant(run.variantId).cfg[COMPONENTS.findIndex(component => component.id === node)], ns: status, host: runtime?.host || '未分配', actor: runtime ? `pid ${runtime.pid}` : '未启动进程', allocated: !!runtime && !['queued', 'skipped'].includes(status), workspace: runtime?.workspace || '未提供', workdir: runtime?.output || '未提供', envId: runtime?.environment || '当前不可用', envPath: runtime?.interpreter || null, attempt: 1, mode: run.mode, envSpec: runtime?.pythonVersion || '未提供', inherited: status === 'restored' }; }
export type DemoAction = { type: 'view'; view: Projection } | { type: 'run'; id: string } | { type: 'variant'; id: string } | { type: 'node'; id: NodeId } | { type: 'checkpoint'; id: string; runId?: string } | { type: 'focus'; value: boolean } | { type: 'axis'; axis: 'xDim' | 'yDim'; value: number } | { type: 'baseline'; id: string } | { type: 'pause' | 'step'; runId?: string } | { type: 'restore'; kind: 'fork' | 'rollback'; cpId: string; runId: string } | { type: 'reset' };
export function studioReducer(previous: DemoState, action: DemoAction): DemoState { const state = structuredClone(previous); if (action.type === 'view') state.view = action.view; if (action.type === 'run') { state.selectedRun = action.id; state.selectedCp = selectedRun(state).path.at(-1) || ''; } if (action.type === 'variant') { const run = latestRun(state, byVariant(action.id)); if (run) { state.selectedRun = run.id; state.selectedCp = run.path.at(-1) || ''; } } if (action.type === 'node') state.selectedNode = action.id; if (action.type === 'checkpoint') { const cp = state.checkpoints[action.id]; if (cp) { state.selectedCp = cp.id; state.selectedRun = action.runId || cp.runId; state.selectedNode = cp.node === '∅' ? COMPONENTS[0]?.id || '' : cp.node; } } if (action.type === 'focus') state.focus = action.value; if (action.type === 'baseline') state.baseline = action.id; if (action.type === 'axis') state[action.axis] = action.value; return state; }
