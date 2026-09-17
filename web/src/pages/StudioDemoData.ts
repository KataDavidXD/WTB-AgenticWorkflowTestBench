export type NodeId = 'A' | 'B' | 'C' | 'D' | 'E' | 'F';
export type Configuration = string | null;
export type RunStatus = 'idle' | 'running' | 'pausing' | 'paused' | 'completed' | 'failed';
export interface StudioVariant {
  id: string;
  name: string;
  label: string;
  cfg: Configuration[];
  extra: [NodeId, NodeId][];
  tags: string[];
}
export const COMPONENTS: { id: NodeId; name: string; en: string }[] = [
  { id: 'A', name: '输入', en: 'INPUT' }, { id: 'B', name: '切块', en: 'CHUNK' },
  { id: 'C', name: '检索', en: 'RETRIEVE' }, { id: 'D', name: '重排', en: 'RERANK' },
  { id: 'E', name: '生成', en: 'GENERATE' }, { id: 'F', name: '输出', en: 'OUTPUT' },
];
export const CONFIGS: Record<NodeId, Configuration[]> = {
  A: ['default'], B: ['c200', 'c500', 'c1000', null], C: ['dense', 'hybrid', 'bm25', null],
  D: ['cross', null], E: ['small', 'large', null], F: ['json', 'csv'],
};
export const LABELS: Record<string, string> = { default: 'default', c200: 'chunk · 200', c500: 'chunk · 500', c1000: 'chunk · 1k', dense: 'dense', hybrid: 'hybrid', bm25: 'BM25', cross: 'cross-encoder', small: 'small', large: 'large', json: 'JSON', csv: 'CSV', null: '— 未使用' };
export const SHORT: Record<string, string> = { default: 'default', c200: '200', c500: '500', c1000: '1k', dense: 'dense', hybrid: 'hybrid', bm25: 'BM25', cross: 'cross-enc.', small: 'small', large: 'large', json: 'JSON', csv: 'CSV', null: '∅' };
export const STATUS: Record<RunStatus, string> = { idle: '未运行', running: '运行中', pausing: '暂停请求中', paused: '已暂停', completed: '已完成', failed: '失败' };
export const NODESTATUS = { done: '已执行', running: '运行中', paused: '边界暂停', queued: '待调度', failed: '失败', restored: '检查点继承', skipped: '未使用' };
const names = ['标准 RAG', '混合检索 + 重排', '稀疏检索', '直接生成', '索引导出', '长上下文', '质量优先', '轻量配置', '多路召回', '上下文旁路'];
const vectors: Configuration[][] = [
  ['default', 'c500', 'dense', null, 'small', 'json'],
  ['default', 'c500', 'hybrid', 'cross', 'small', 'json'],
  ['default', 'c500', 'bm25', null, 'small', 'json'],
  ['default', null, null, null, 'small', 'json'],
  ['default', 'c200', 'dense', null, null, 'csv'],
  ['default', 'c1000', 'dense', null, 'large', 'json'],
  ['default', 'c500', 'hybrid', 'cross', 'large', 'json'],
  ['default', 'c200', 'bm25', null, 'small', 'json'],
  ['default', 'c200', 'hybrid', 'cross', 'large', 'json'],
  ['default', 'c500', 'dense', null, 'small', 'json'],
];
export const variants: StudioVariant[] = vectors.map((cfg, i) => ({ id: `w${String(i + 1).padStart(2, '0')}`, name: `workflow${String(i + 1).padStart(2, '0')}`, label: names[i], cfg, extra: i === 9 ? [['B', 'E']] : [], tags: i === 9 ? ['旁路', 'RAG'] : i === 3 ? ['直出'] : ['RAG'] }));
export const byVariant = (id: string) => variants.find(v => v.id === id)!;
export const nodes = (v: StudioVariant) => COMPONENTS.filter((_, i) => v.cfg[i] !== null).map(c => c.id);
export function edges(v: StudioVariant): [NodeId, NodeId][] {
  const sequence = nodes(v);
  return sequence.slice(1).map((node, i): [NodeId, NodeId] => [sequence[i], node]).concat(v.extra);
}
export const configurationDifference = (a: StudioVariant, b: StudioVariant) => a.cfg.reduce<number>((count, cfg, i) => count + Number(cfg !== b.cfg[i]), 0);
export function edgeDifference(a: StudioVariant, b: StudioVariant) {
  const x = new Set(edges(a).map(e => e.join('>'))), y = new Set(edges(b).map(e => e.join('>')));
  return [...x].filter(e => !y.has(e)).length + [...y].filter(e => !x.has(e)).length;
}
export const statusColor = (status: RunStatus) => status === 'failed' ? 'var(--bad)' : ['running', 'paused', 'pausing'].includes(status) ? 'var(--accent)' : status === 'completed' ? 'var(--ok)' : 'var(--idle)';
