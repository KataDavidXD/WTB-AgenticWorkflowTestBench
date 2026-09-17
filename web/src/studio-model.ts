export type StudioNode = { id: string; implementation: string };
export type StudioVariant = { id: string; nodes: StudioNode[]; edges: unknown[] };
export type StudioRun = { nodeId: string; status: string };

export function unionNodeIds(variants: StudioVariant[]) { return [...new Set(variants.flatMap(variant => variant.nodes.map(node => node.id)))]; }
export function displayNodeStatus(nodeId: string, nodes: StudioNode[], runs: StudioRun[]) {
  if (!nodes.some(node => node.id === nodeId)) return 'missing';
  return runs.filter(run => run.nodeId === nodeId).at(-1)?.status || 'pending';
}
export function canControl(status: string, action: 'pause' | 'resume' | 'stop' | 'checkpoint' | 'rollback' | 'fork') {
  if (action === 'pause') return status === 'running';
  if (action === 'resume') return status === 'paused';
  if (action === 'stop') return status === 'running' || status === 'paused';
  return status === 'paused';
}
