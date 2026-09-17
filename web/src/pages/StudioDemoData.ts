/**
 * The SVG stages retain their original visual grammar.  This module is now a
 * mutable projection of the real console catalog, not sample workflow data.
 */
export type NodeId = string;
export type Configuration = string | null;
export type RunStatus = 'idle' | 'queued' | 'running' | 'pausing' | 'paused' | 'stopping' | 'completed' | 'failed' | 'cancelled';
export interface StudioVariant { id: string; name: string; label: string; cfg: Configuration[]; extra: [NodeId, NodeId][]; tags: string[]; }
type ApiVariant = { id: string; name: string; description: string; workflowVariant: string | null; nodes: { id: string; implementation: string }[]; edges: { source: string; target: string; conditional: boolean }[] };

export let COMPONENTS: { id: NodeId; name: string; en: string }[] = [];
export let CONFIGS: Record<NodeId, Configuration[]> = {};
export let LABELS: Record<string, string> = { null: '— 未使用' };
export let SHORT: Record<string, string> = { null: '∅' };
export let variants: StudioVariant[] = [];
export const STATUS: Record<RunStatus, string> = { idle: '未运行', queued: '排队中', running: '运行中', pausing: '暂停请求中', paused: '已暂停', stopping: '停止请求中', completed: '已完成', failed: '失败', cancelled: '已停止' };
export const NODESTATUS = { done: '已执行', running: '运行中', paused: '边界暂停', queued: '待调度', failed: '失败', restored: '检查点继承', skipped: '未使用' };

export function configureStudioCatalog(source: ApiVariant[]) {
  const nodeIds = [...new Set(source.flatMap(variant => variant.nodes.map(node => node.id).filter(id => !id.startsWith('__'))))];
  COMPONENTS = nodeIds.map(id => ({ id, name: id, en: id.toUpperCase().slice(0, 12) }));
  CONFIGS = Object.fromEntries(COMPONENTS.map(component => [component.id, [...new Set(source.flatMap(variant => variant.nodes.filter(node => node.id === component.id).map(node => node.implementation))), null]]));
  LABELS = { null: '— 未使用' }; SHORT = { null: '∅' };
  Object.values(CONFIGS).flat().filter((value): value is string => value !== null).forEach(value => { LABELS[value] = value; SHORT[value] = value.length > 11 ? value.slice(0, 10) + '…' : value; });
  variants = source.map(variant => {
    const present = new Set(variant.nodes.map(node => node.id));
    const sequential = COMPONENTS.filter(component => present.has(component.id)).slice(1).map((component, i) => [COMPONENTS.filter(c => present.has(c.id))[i].id, component.id].join('>'));
    return { id: variant.id, name: variant.name, label: variant.description || variant.workflowVariant || '默认工作流', cfg: COMPONENTS.map(component => variant.nodes.find(node => node.id === component.id)?.implementation || null), extra: variant.edges.filter(edge => !edge.source.startsWith('__') && !edge.target.startsWith('__') && !sequential.includes(edge.source + '>' + edge.target)).map(edge => [edge.source, edge.target]), tags: variant.workflowVariant ? [variant.workflowVariant] : ['default'] };
  });
}
export const byVariant = (id: string) => variants.find(variant => variant.id === id)!;
export const nodes = (variant: StudioVariant) => COMPONENTS.filter((_, index) => variant.cfg[index] !== null).map(component => component.id);
export function edges(variant: StudioVariant): [NodeId, NodeId][] { const sequence = nodes(variant); return sequence.slice(1).map((node, index): [NodeId, NodeId] => [sequence[index], node]).concat(variant.extra); }
export const configurationDifference = (a: StudioVariant, b: StudioVariant) => a.cfg.reduce((count, cfg, i) => count + Number(cfg !== b.cfg[i]), 0);
export function edgeDifference(a: StudioVariant, b: StudioVariant) { const left = new Set(edges(a).map(edge => edge.join('>'))), right = new Set(edges(b).map(edge => edge.join('>'))); return [...left].filter(edge => !right.has(edge)).length + [...right].filter(edge => !left.has(edge)).length; }
export const statusColor = (status: RunStatus) => status === 'failed' ? 'var(--bad)' : ['running', 'paused', 'pausing', 'stopping'].includes(status) ? 'var(--accent)' : status === 'completed' ? 'var(--ok)' : 'var(--idle)';
