import type { Catalog, ConsoleService, Execution, OperationResult, Page, Snapshot, Variables } from './domain';

export async function api<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch('/api/v1' + path, { method: body === undefined ? 'GET' : 'POST', headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!response.ok) { const error = await response.json().catch(() => ({})); throw new Error(typeof error.detail === 'string' ? error.detail : `HTTP ${response.status}`); }
  return response.json() as Promise<T>;
}
class HttpConsoleService implements ConsoleService {
  private state: Snapshot = { executions: [], total: 0, offset: 0, connection: 'connecting', loading: true, revision: 0 };
  private listeners = new Set<() => void>();
  private inFlight?: Promise<void>;
  getSnapshot = () => this.state;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  private publish(patch: Partial<Snapshot>) { this.state = { ...this.state, ...patch, revision: this.state.revision + 1 }; this.listeners.forEach(fn => fn()); }
  refresh = async () => {
    if (this.inFlight) return this.inFlight;
    this.inFlight = (async () => {
      try {
        const [catalog, page] = await Promise.all([api<Catalog>('/catalog'), api<Page<Execution>>(`/executions?limit=50&offset=${this.state.offset}`)]);
        this.publish({ catalog, executions: page.items, total: page.total, loading: false, error: undefined, connection: this.state.connection === 'live' ? 'live' : 'polling' });
      } catch (error) { this.publish({ loading: false, error: String(error), connection: 'offline' }); }
    })().finally(() => { this.inFlight = undefined; });
    return this.inFlight;
  };
  page = async (offset: number) => { this.publish({ offset }); await this.refresh(); };
  connect() {
    let active = true; let socket: WebSocket; let retry: ReturnType<typeof setTimeout>; let debounce: ReturnType<typeof setTimeout>;
    const open = () => {
      if (!active) return;
      socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws`);
      socket.onopen = () => { this.publish({ connection: 'live' }); void this.refresh(); };
      socket.onmessage = () => { clearTimeout(debounce); debounce = setTimeout(() => void this.refresh(), 100); };
      socket.onclose = () => { if (active) { this.publish({ connection: 'polling' }); retry = setTimeout(open, 2000); } };
      socket.onerror = () => socket.close();
    };
    void this.refresh(); open();
    const timer = setInterval(() => void this.refresh(), 2000);
    return () => { active = false; clearInterval(timer); clearTimeout(retry); clearTimeout(debounce); socket?.close(); };
  }
  start = (project: string, variantId: string, state: Variables, breakpoints: string[], nodeVariants: Record<string, string>) => api<OperationResult>(`/workflows/${encodeURIComponent(project)}/execute`, { variantId, state, breakpoints, nodeVariants });
  command = (id: string, action: string, payload: Variables = {}) => api<OperationResult>(`/executions/${id}/${action}`, payload);
}
export const service = new HttpConsoleService();
