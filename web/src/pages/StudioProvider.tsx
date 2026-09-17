import { createContext, useContext, useEffect, useReducer, useRef, useState, type Dispatch, type ReactNode } from 'react';
import { initialState, selectedRun, studioReducer, type DemoAction, type DemoState } from './StudioState';
const STORAGE_KEY = 'wtb-react-structural-studio-v1';
interface StudioContextValue { state: DemoState; dispatch: Dispatch<DemoAction>; auto: boolean; setAuto: (value: boolean) => void; drawer: boolean; setDrawer: (value: boolean) => void; modal: 'guide' | 'fork' | 'rollback' | null; setModal: (value: 'guide' | 'fork' | 'rollback' | null) => void; notify: (message: string) => void; message: string; saved: boolean }
const StudioContext = createContext<StudioContextValue | null>(null);
function load(): DemoState {
  try { const raw = localStorage.getItem(STORAGE_KEY); if (raw) { const s: DemoState = JSON.parse(raw); if (s.schema === 3 && s.runs?.length && s.tracks?.length && s.checkpoints && s.runs.some(r => r.id === s.selectedRun) && ['loom', 'lineage', 'section', 'atlas'].includes(s.view)) { s.runs.forEach(r => { if (r.status === 'pausing') r.status = 'paused'; }); return s; } } } catch { /* Storage may be unavailable. */ }
  return initialState();
}
export function StudioProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(studioReducer, undefined, load), [auto, setAuto] = useState(false), [drawer, setDrawer] = useState(false), [modal, setModal] = useState<StudioContextValue['modal']>(null), [message, setMessage] = useState(''), [saved, setSaved] = useState(false);
  const current = useRef(state); current.current = state;
  useEffect(() => { try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); setSaved(true); } catch { setSaved(false); } }, [state]);
  useEffect(() => { document.body.dataset.theme = 'cyber'; document.body.dataset.view = state.view; return () => { delete document.body.dataset.theme; delete document.body.dataset.view; }; }, [state.view]);
  useEffect(() => { if (!message) return; const timer = window.setTimeout(() => setMessage(''), 3400); return () => clearTimeout(timer); }, [message]);
  const pausingIds = state.runs.filter(r => r.status === 'pausing').map(r => r.id).join(',');
  useEffect(() => { const timers = pausingIds ? pausingIds.split(',').map(runId => window.setTimeout(() => dispatch({ type: 'step', runId }), 750)) : []; return () => timers.forEach(clearTimeout); }, [pausingIds]);
  useEffect(() => { if (!auto) return; const timer = window.setInterval(() => { const r = selectedRun(current.current); if (r.status === 'running') dispatch({ type: 'step', runId: r.id }); }, 1900); return () => clearInterval(timer); }, [auto]);
  return <StudioContext.Provider value={{ state, dispatch, auto, setAuto, drawer, setDrawer, modal, setModal, notify: setMessage, message, saved }}>{children}</StudioContext.Provider>;
}
export function useStudio() { const value = useContext(StudioContext); if (!value) throw new Error('StudioProvider is required'); return value; }
