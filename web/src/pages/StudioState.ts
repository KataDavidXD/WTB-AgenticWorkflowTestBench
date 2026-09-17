import { byVariant, COMPONENTS, nodes, SHORT, variants, type NodeId, type RunStatus, type StudioVariant } from './StudioDemoData';
import type { Projection } from './StudioTypes';

export interface Checkpoint { id: string; runId: string; trackId: string; step: number; node: NodeId | '∅'; cas: string; tracked: number; restorable: boolean; clock: number }
export interface Attempt { id: string; runId: string; baseStep: number; origin: { runId: string; trackId: string; cpId: string; type: 'fork' | 'rollback' } | null; cps: string[]; active: boolean; kind: 'initial' | 'fork' | 'rollback'; label: string }
export interface DemoRun { id: string; variantId: string; mode: 'ray' | 'local'; status: RunStatus; step: number; trackId: string; path: string[]; events: { kind: string; text: string; at: string }[]; created: number; failedNode: NodeId | null }
export interface DemoState { schema: number; view: Projection; selectedRun: string; selectedNode: NodeId; selectedCp: string; focus: boolean; onlyFamily: boolean; xDim: number; yDim: number; baseline: string; runCounter: number; cpCounter: number; trackCounter: number; clock: number; runs: DemoRun[]; tracks: Attempt[]; checkpoints: Record<string, Checkpoint> }

export function initialState(): DemoState {
  const s: DemoState = { schema: 3, view: 'loom', selectedRun: 'run-102', selectedNode: 'C', selectedCp: 'cp-102-02', focus: false, onlyFamily: true, xDim: 2, yDim: 4, baseline: 'w01', runCounter: 120, cpCounter: 500, trackCounter: 20, clock: 0, runs: [], tracks: [], checkpoints: {} };
  const statuses: RunStatus[] = ['completed', 'running', 'failed', 'idle', 'completed', 'paused', 'running', 'idle', 'completed', 'completed'];
  const steps = [5, 2, 2, 0, 4, 2, 3, 0, 6, 5];
  variants.forEach((v, i) => {
    const rid = `run-${101 + i}`, tid = `track-${i + 1}`, step = Math.min(nodes(v).length, steps[i]);
    const r: DemoRun = { id: rid, variantId: v.id, mode: [1, 2, 5, 6, 8].includes(i) ? 'ray' : 'local', status: statuses[i], step, trackId: tid, path: [], events: [], created: i, failedNode: statuses[i] === 'failed' ? nodes(v)[step] : null };
    const t: Attempt = { id: tid, runId: rid, baseStep: 0, origin: null, cps: [], active: true, kind: 'initial', label: '初次执行' };
    for (let j = 0; j <= step; j++) {
      const id = `cp-${101 + i}-${String(j).padStart(2, '0')}`;
      s.checkpoints[id] = { id, runId: rid, trackId: tid, step: j, node: j ? nodes(v)[j - 1] : '∅', cas: `sha256:${(7843 + i * 817 + j * 153).toString(16)}b2e`, tracked: j * 3, restorable: true, clock: j };
      r.path.push(id); t.cps.push(id);
    }
    r.events.push({ kind: 'created', text: '创建运行', at: '10:21:00' });
    if (step) r.events.push({ kind: 'checkpoint', text: `checkpoint / ${nodes(v)[step - 1]} 后`, at: `10:21:${String(2 + step * 2).padStart(2, '0')}` });
    s.runs.push(r); s.tracks.push(t);
  });
  const parent = s.runs[1];
  const child: DemoRun = { id: 'run-111', variantId: 'w02', mode: 'local', status: 'paused', step: 2, trackId: 'track-11', path: parent.path.slice(0, 2), events: [{ kind: 'fork', text: '从 run-102 / A 后 Fork', at: '10:21:09' }], created: 11, failedNode: null };
  s.checkpoints['cp-111-02'] = { id: 'cp-111-02', runId: child.id, trackId: child.trackId, step: 2, node: 'B', cas: 'sha256:fa217cb', tracked: 6, restorable: true, clock: 8 };
  child.path.push('cp-111-02'); s.runs.push(child);
  s.tracks.push({ id: child.trackId, runId: child.id, baseStep: 1, origin: { runId: parent.id, trackId: parent.trackId, cpId: 'cp-102-01', type: 'fork' }, cps: ['cp-111-02'], active: true, kind: 'fork', label: 'Fork · 独立工作区' });
  const oldTrack = s.tracks[1];
  for (let j = 3; j <= 4; j++) {
    const id = `cp-102-${String(j).padStart(2, '0')}`;
    s.checkpoints[id] = { id, runId: parent.id, trackId: oldTrack.id, step: j, node: nodes(variants[1])[j - 1], cas: `sha256:older${j}c0`, tracked: j * 3, restorable: true, clock: j }; oldTrack.cps.push(id);
  }
  oldTrack.active = false; parent.trackId = 'track-12';
  s.tracks.push({ id: parent.trackId, runId: parent.id, baseStep: 2, origin: { runId: parent.id, trackId: oldTrack.id, cpId: 'cp-102-02', type: 'rollback' }, cps: [], active: true, kind: 'rollback', label: 'Rollback · 新尝试' });
  parent.events.push({ kind: 'rollback', text: '回退到 B 后；第一次尝试保留', at: '10:21:15' }, { kind: 'resume', text: '新尝试开始执行 C', at: '10:21:17' });
  const second: DemoRun = { id: 'run-112', variantId: 'w02', mode: 'ray', status: 'failed', step: 4, trackId: 'track-13', path: [...parent.path], events: [{ kind: 'fork', text: '从 run-102 / B 后 Fork', at: '10:21:19' }, { kind: 'error', text: 'E 执行失败 / 演示 timeout', at: '10:21:23' }], created: 12, failedNode: 'E' };
  const secondTrack: Attempt = { id: second.trackId, runId: second.id, baseStep: 2, origin: { runId: parent.id, trackId: oldTrack.id, cpId: 'cp-102-02', type: 'fork' }, cps: [], active: true, kind: 'fork', label: 'Fork · 独立工作区' };
  for (let j = 3; j <= 4; j++) {
    const id = `cp-112-${String(j).padStart(2, '0')}`;
    s.checkpoints[id] = { id, runId: second.id, trackId: second.trackId, step: j, node: nodes(variants[1])[j - 1], cas: `sha256:fork${j}d0`, tracked: j * 3, restorable: true, clock: j + 8 }; second.path.push(id); secondTrack.cps.push(id);
  }
  s.runs.push(second); s.tracks.push(secondTrack); return s;
}

export const selectedRun = (s: DemoState) => s.runs.find(r => r.id === s.selectedRun)!;
export const selectedVariant = (s: DemoState) => byVariant(selectedRun(s).variantId);
export const selectedAttempt = (s: DemoState, r = selectedRun(s)) => s.tracks.find(t => t.id === r.trackId)!;
export const selectedCheckpoint = (s: DemoState) => s.checkpoints[s.selectedCp] || s.checkpoints[selectedRun(s).path.at(-1)!];
export const latestRun = (s: DemoState, v: StudioVariant) => v.id === selectedRun(s).variantId ? selectedRun(s) : s.runs.filter(r => r.variantId === v.id).sort((a, b) => b.created - a.created)[0];
export const currentNode = (r: DemoRun) => nodes(byVariant(r.variantId))[r.step] || nodes(byVariant(r.variantId)).at(-1)!;
export function nodeStatus(s: DemoState, c: NodeId, r = selectedRun(s)): 'skipped' | 'restored' | 'done' | 'failed' | 'running' | 'paused' | 'queued' {
  const i = nodes(byVariant(r.variantId)).indexOf(c);
  if (i < 0) return 'skipped';
  if (i < r.step) return i < selectedAttempt(s, r).baseStep ? 'restored' : 'done';
  if (i === r.step) { if (r.status === 'failed') return 'failed'; if (['running', 'pausing'].includes(r.status)) return 'running'; if (r.status === 'paused') return 'paused'; }
  return 'queued';
}
export function nodeContext(s: DemoState, c = s.selectedNode, r = selectedRun(s)) {
  const ns = nodeStatus(s, c, r), i = COMPONENTS.findIndex(x => x.id === c), num = r.id.split('-')[1], attempt = s.tracks.filter(t => t.runId === r.id).indexOf(selectedAttempt(s, r)) + 1;
  const cfg = byVariant(r.variantId).cfg[i], host = r.mode === 'ray' ? `ray-w${c === 'E' ? '03' : '02'}` : 'local-mbp', allocated = ['done', 'running', 'failed'].includes(ns);
  const actor = r.mode === 'ray' ? (allocated ? `${c.toLowerCase()}-${SHORT[String(cfg)]}-${num}-t${attempt}` : '未分配 Actor') : (allocated ? `pid ${4100 + Number(num) * 2 + i * 31 + attempt}` : '未启动进程');
  const workspace = `/Users/builder/wtb/workspaces/${r.id}/`, workdir = r.mode === 'ray' ? `/srv/wtb/workspaces/${r.id}/` : workspace;
  const envBase = r.mode === 'ray' ? '/opt/wtb/envs/' : '/Users/builder/wtb/.venvs/', envId = `${c.toLowerCase()}-${cfg || 'none'}-py311`;
  return { c, cfg, ns, host, actor, allocated, workspace, workdir, envId, envPath: allocated ? `${envBase}${envId}/bin/python` : null, attempt, mode: r.mode, envSpec: cfg ? `${c.toLowerCase()}-${cfg}.lock` : '—', inherited: ns === 'restored' };
}

export type DemoAction = { type: 'view'; view: Projection } | { type: 'run'; id: string } | { type: 'variant'; id: string } | { type: 'node'; id: NodeId } | { type: 'checkpoint'; id: string; runId?: string } | { type: 'focus'; value: boolean } | { type: 'axis'; axis: 'xDim' | 'yDim'; value: number } | { type: 'baseline'; id: string } | { type: 'step' | 'pause'; runId?: string } | { type: 'restore'; kind: 'fork' | 'rollback'; cpId: string; runId: string } | { type: 'reset' };
function log(s: DemoState, r: DemoRun, kind: string, text: string) { const sec = ++s.clock; r.events.push({ kind, text, at: `10:${String(22 + Math.floor(sec / 60)).padStart(2, '0')}:${String(sec % 60).padStart(2, '0')}` }); }
function checkpointPath(s: DemoState, id: string, seen = new Set<string>()): string[] { if (seen.has(id)) return []; seen.add(id); const cp = s.checkpoints[id], t = s.tracks.find(x => x.id === cp.trackId); if (!t) return [id]; return [...(t.origin ? checkpointPath(s, t.origin.cpId, seen) : []), ...t.cps.slice(0, t.cps.indexOf(id) + 1)]; }
export function studioReducer(previous: DemoState, action: DemoAction): DemoState {
  if (action.type === 'reset') return initialState();
  const s = structuredClone(previous);
  const chooseRun = (id: string) => { const r = s.runs.find(x => x.id === id); if (r) { s.selectedRun = id; s.selectedCp = r.path.at(-1)!; s.selectedNode = currentNode(r); } };
  switch (action.type) {
    case 'view': s.view = action.view; break;
    case 'run': chooseRun(action.id); break;
    case 'variant': chooseRun(latestRun(s, byVariant(action.id)).id); break;
    case 'node': s.selectedNode = action.id; break;
    case 'checkpoint': { const cp = s.checkpoints[action.id]; if (cp) { if (action.runId) s.selectedRun = action.runId; s.selectedCp = cp.id; s.selectedNode = cp.node === '∅' ? nodes(selectedVariant(s))[0] : cp.node; } break; }
    case 'focus': s.focus = action.value; break;
    case 'baseline': s.baseline = action.id; break;
    case 'axis': { const other = action.axis === 'xDim' ? 'yDim' : 'xDim', old = s[action.axis]; s[action.axis] = action.value; if (s[other] === action.value) s[other] = old; break; }
    case 'pause': { const r = action.runId ? s.runs.find(x => x.id === action.runId)! : selectedRun(s); if (r.status === 'running') { r.status = 'pausing'; log(s, r, 'pause_requested', '请求在下一个节点边界暂停'); } else if (['paused', 'idle', 'failed'].includes(r.status)) { const old = r.status; r.status = 'running'; r.failedNode = null; log(s, r, old === 'idle' ? 'start' : 'resume', old === 'failed' ? '重试当前节点' : '开始 / 继续执行'); } break; }
    case 'step': {
      const r = action.runId ? s.runs.find(x => x.id === action.runId)! : selectedRun(s), seq = nodes(byVariant(r.variantId));
      if (!['running', 'paused', 'pausing'].includes(r.status) || r.step >= seq.length) break;
      const pausing = r.status === 'pausing', paused = r.status === 'paused', c = seq[r.step++], id = `cp-${r.id.split('-')[1]}-${++s.cpCounter}`, t = selectedAttempt(s, r);
      s.checkpoints[id] = { id, runId: r.id, trackId: t.id, step: r.step, node: c, cas: `sha256:${(s.cpCounter * 983).toString(16)}ab`, tracked: r.step * 3, restorable: true, clock: s.clock }; t.cps.push(id); r.path.push(id); r.failedNode = null;
      log(s, r, 'checkpoint', `${c} 完成 / ${id}`); r.status = r.step === seq.length ? 'completed' : pausing || paused ? 'paused' : 'running';
      if (pausing) log(s, r, 'pause', '已在节点边界暂停'); if (r.status === 'completed') log(s, r, 'complete', '运行完成'); if (r.id === s.selectedRun) { s.selectedCp = id; s.selectedNode = currentNode(r); } break;
    }
    case 'restore': {
      const source = s.runs.find(r => r.id === action.runId), cp = s.checkpoints[action.cpId];
      if (!source || !cp?.restorable || (action.kind === 'rollback' && ['running', 'pausing', 'idle'].includes(source.status))) break;
      const tid = `track-${++s.trackCounter}`, path = source.path.includes(cp.id) ? source.path.slice(0, source.path.indexOf(cp.id) + 1) : checkpointPath(s, cp.id);
      let target: DemoRun;
      if (action.kind === 'fork') { target = { id: `run-${++s.runCounter}`, variantId: source.variantId, mode: source.mode, status: cp.step === nodes(byVariant(source.variantId)).length ? 'completed' : 'paused', step: cp.step, trackId: tid, path: [...path], events: [], created: ++s.clock, failedNode: null }; s.runs.push(target); log(s, target, 'fork', `从 ${source.id} / ${cp.id} Fork`); }
      else { target = source; selectedAttempt(s, target).active = false; target.trackId = tid; target.step = cp.step; target.status = cp.step === nodes(byVariant(target.variantId)).length ? 'completed' : 'paused'; target.path = [...path]; target.failedNode = null; log(s, target, 'rollback', `回退至 ${cp.id}，旧尝试保留`); }
      s.tracks.push({ id: tid, runId: target.id, baseStep: cp.step, origin: { runId: cp.runId, trackId: cp.trackId, cpId: cp.id, type: action.kind }, cps: [], active: true, kind: action.kind, label: action.kind === 'fork' ? 'Fork · 独立工作区' : 'Rollback · 新尝试' });
      s.selectedRun = target.id; s.selectedCp = cp.id; s.selectedNode = currentNode(target); s.view = 'lineage'; break;
    }
  }
  return s;
}
