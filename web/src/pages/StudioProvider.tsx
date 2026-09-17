import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react';
import { configureStudioCatalog } from './StudioDemoData';
import { emptyState, hydrateState, selectedRun, studioReducer, type DemoAction, type DemoState } from './StudioState';

type Catalog = { variants: { id: string; name: string; description: string; workflowVariant: string | null; nodes: { id: string; implementation: string }[]; edges: { source: string; target: string; conditional: boolean }[] }[]; capabilities: Record<string, boolean>; unavailable: Record<string, string> };
type Execution = { id: string; variantId: string; status: string; createdAt: string; nodeRuns?: { nodeId: string; status: string; step: number; error?: string }[]; parentId?: string; fromCheckpointId?: string; runtime?: { host: string; pid: number; workspace: string; output: string; interpreter: string; pythonVersion: string; environment: string } };
type Checkpoint = { checkpointId: string; step: number; nextNodes: string[]; fileCommitId: string; createdAt: string };
async function api<T>(path: string, body?: unknown): Promise<T> { const response = await fetch('/api/v1' + path, { method: body === undefined ? 'GET' : 'POST', headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) }); if (!response.ok) { const e = await response.json().catch(() => ({})); throw new Error(e.detail || `HTTP ${response.status}`); } return response.json() as Promise<T>; }

interface StudioContextValue { state: DemoState; dispatch: (action: DemoAction) => void; auto: boolean; setAuto: (value: boolean) => void; drawer: boolean; setDrawer: (value: boolean) => void; modal: 'guide' | 'fork' | 'rollback' | null; setModal: (value: 'guide' | 'fork' | 'rollback' | null) => void; notify: (message: string) => void; message: string; saved: boolean; capabilities: Record<string, boolean>; unavailable: Record<string, string>; ready: boolean }
const StudioContext = createContext<StudioContextValue | null>(null);

export function StudioProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState(emptyState), [drawer, setDrawer] = useState(false), [modal, setModal] = useState<StudioContextValue['modal']>(null), [message, setMessage] = useState(''), [capabilities, setCapabilities] = useState<Record<string, boolean>>({}), [unavailable, setUnavailable] = useState<Record<string, string>>({}), [ready, setReady] = useState(false);
  const refresh = useCallback(async () => {
    try {
      const catalog = await api<Catalog>('/catalog'); configureStudioCatalog(catalog.variants); setCapabilities(catalog.capabilities); setUnavailable(catalog.unavailable);
      const page = await api<{ items: Execution[] }>('/executions?limit=100');
      const details = await Promise.all(page.items.map(async item => api<Execution>(`/executions/${item.id}`).catch(() => item)));
      const checkpointPairs = await Promise.all(details.map(async item => [item.id, (await api<{ items: Checkpoint[] }>(`/executions/${item.id}/checkpoints?limit=200`)).items] as const));
      const checkpointMap = Object.fromEntries(checkpointPairs);
      setState(previous => hydrateState(previous, details as Parameters<typeof hydrateState>[1], checkpointMap)); setReady(true);
    } catch (error) { setMessage(`无法连接本机 WTB：${String(error)}`); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => { let alive = true; const timer = window.setInterval(() => void refresh(), 2000); let socket: WebSocket | undefined; try { socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`); socket.onmessage = () => alive && void refresh(); } catch { /* polling remains the authoritative fallback */ } return () => { alive = false; clearInterval(timer); socket?.close(); }; }, [refresh]);
  useEffect(() => { document.body.dataset.theme = 'cyber'; document.body.dataset.view = state.view; return () => { delete document.body.dataset.theme; delete document.body.dataset.view; }; }, [state.view]);
  useEffect(() => { if (!message) return; const timer = window.setTimeout(() => setMessage(''), 3400); return () => clearTimeout(timer); }, [message]);
  const submit = async (id: string, action: string, payload: object = {}) => { const result = await api<{ operationId: string; executionId: string }>(`/executions/${id}/${action}`, payload); for (let attempt = 0; attempt < 100; attempt++) { const op = await api<{ status: string; error?: string; resultExecutionId?: string }>(`/operations/${result.operationId}`); if (!['queued', 'running'].includes(op.status)) { if (op.status !== 'completed') throw new Error(op.error || '操作失败'); if (op.resultExecutionId) setState(previous => studioReducer(previous, { type: 'run', id: op.resultExecutionId! })); return; } await new Promise(resolve => setTimeout(resolve, 100)); } throw new Error('操作等待超时'); };
  const dispatch = (action: DemoAction) => {
    const run = selectedRun(state);
    if (action.type === 'reset') { void refresh(); return; }
    if (action.type === 'pause' || action.type === 'step') { if (!run) return; const command = run.status === 'running' ? 'pause' : 'resume'; void submit(run.id, command).then(refresh).then(() => setMessage(command === 'pause' ? '暂停请求已提交；将在安全边界确认。' : '继续请求已完成。')).catch(error => setMessage(String(error))); return; }
    if (action.type === 'restore') { void submit(action.runId, action.kind === 'fork' ? 'branches' : 'rollback', { checkpointId: action.cpId }).then(refresh).then(() => { setModal(null); setMessage(action.kind === 'fork' ? '已从真实检查点创建分支。' : '已回退至真实检查点。'); }).catch(error => setMessage(String(error))); return; }
    setState(previous => studioReducer(previous, action));
  };
  return <StudioContext.Provider value={{ state, dispatch, auto: false, setAuto: () => setMessage('自动推进不适用于真实执行；请使用继续操作。'), drawer, setDrawer, modal, setModal, notify: setMessage, message, saved: true, capabilities, unavailable, ready }}>{children}</StudioContext.Provider>;
}
export function useStudio() { const value = useContext(StudioContext); if (!value) throw new Error('StudioProvider is required'); return value; }
