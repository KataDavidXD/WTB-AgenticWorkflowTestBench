import { useEffect, useRef } from 'react';
import type { ReactNode } from 'react';
import { statusNames } from '../domain';
import type { Status } from '../domain';

export function StatusBadge({ status }: { status: Status }) {
  const icons: Record<Status, string> = { running: '▶', pausing: '◷', paused: 'Ⅱ', completed: '✓', failed: '!', cancelled: '■', queued: '⋯' };
  return <span className={`status ${status}`}><span aria-hidden="true">{icons[status]}</span> {statusNames[status]}</span>;
}
export const time = (value: string) => new Date(value).toLocaleTimeString('zh-CN', { hour12: false });
export const duration = (ms: number) => `${(ms / 1000).toFixed(0)}s`;

export function Empty({ children }: { children: ReactNode }) { return <div className="empty">{children}</div>; }
export function JsonView({ value }: { value: unknown }) { return <pre className="json">{JSON.stringify(value, null, 2)}</pre>; }
export function Tags({ items }: { items: string[] }) { return <span className="tags">{items.map(t => <span key={t} className="tag">{t}</span>)}</span>; }
export function Field({ label, children }: { label: string; children: ReactNode }) { return <label className="field"><span>{label}</span>{children}</label>; }
export function PathField({ label, path, onCopy }: { label: string; path: string; onCopy: (text: string) => void }) {
  return <div className="path-field"><div>{label}<button className="link" onClick={() => onCopy(path)} aria-label={`复制${label}`}>复制</button></div><code>{path}</code></div>;
}

export function Modal({ title, children, onClose }: { title: string; children: ReactNode; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose); closeRef.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement;
    const root = ref.current!;
    const focusables = () => [...root.querySelectorAll<HTMLElement>('button:not(:disabled),input,select,textarea,[tabindex="0"]')];
    focusables()[0]?.focus();
    const handler = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeRef.current();
      if (event.key === 'Tab') {
        const nodes = focusables(); const first = nodes[0]; const last = nodes.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    };
    root.addEventListener('keydown', handler);
    return () => { root.removeEventListener('keydown', handler); previous?.focus(); };
  }, []);
  return <div className="modal-backdrop"><div className="modal" role="dialog" aria-modal="true" aria-label={title} ref={ref}>
    <div className="panel-heading"><h2>{title}</h2><button aria-label="关闭弹窗" onClick={onClose}>×</button></div>
    {children}
  </div></div>;
}
