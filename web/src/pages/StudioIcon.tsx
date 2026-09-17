import type { ReactNode } from 'react';
const paths: Record<string, ReactNode> = {
  pause: <path d="M8 5v14M16 5v14" />, play: <path d="m8 5 11 7-11 7Z" />,
  fork: <><path d="M6 3v13a4 4 0 0 0 4 4h8M6 11h6a5 5 0 0 0 5-5V3" /><circle cx="6" cy="3" r="2" /><circle cx="17" cy="3" r="2" /><circle cx="18" cy="20" r="2" /></>,
  back: <path d="m8 4-5 5 5 5M3 9h11a7 7 0 0 1 0 14" />, copy: <><rect x="8" y="8" width="12" height="12" rx="2" /><path d="M16 8V4H4v12h4" /></>,
  arrow: <path d="M4 12h16m-6-6 6 6-6 6" />, step: <><path d="m5 5 10 7-10 7Z" /><path d="M19 5v14" /></>, close: <path d="m6 6 12 12M18 6 6 18" />,
  box: <><path d="m12 3 9 5v9l-9 5-9-5V8Z" /><path d="m3 8 9 5 9-5M12 13v9" /></>, terminal: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="m7 9 3 3-3 3m6 0h4" /></>, check: <path d="m5 12 4 4L19 6" />, info: <><circle cx="12" cy="12" r="9" /><path d="M12 11v6m0-11v2" /></>,
};
export function StudioIcon({ name, size = 14 }: { name: string; size?: number }) { return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] || paths.info}</svg>; }
