import { useMemo } from 'react';
import { Background, Controls, Handle, MarkerType, MiniMap, Position, ReactFlow } from '@xyflow/react';
import type { Node, NodeProps } from '@xyflow/react';
import type { Checkpoint, ComponentDef, Execution, Variant } from '../domain';
import { busy } from '../domain';

type CardData = { label: string; componentId: string; implementation: string; status: string; breakpoint: boolean; elapsed: string };
type CardNode = Node<CardData>;
function ComponentNode({ data, selected }: NodeProps<CardNode>) {
  return <div className={`graph-card ${data.status} ${selected ? 'selected' : ''}`}>
    <Handle type="target" position={Position.Left} />
    <div className="graph-card-top"><span className="component-letter">{data.componentId.toUpperCase()}</span><strong>{data.label}</strong>{data.breakpoint && <span className="breakpoint-dot" title="节点前断点">●</span>}</div>
    <div className="graph-implementation">{data.implementation}</div>
    <div className="graph-card-bottom"><span>{({ running: '▶ 运行中', completed: '✓ 已完成', failed: '! 失败', pending: '○ 待执行', paused: 'Ⅱ 等待继续', skipped: '↪ 未走此分支' } as Record<string, string>)[data.status]}</span><span>{data.elapsed}</span></div>
    <Handle type="source" position={Position.Right} />
  </div>;
}
const nodeTypes = { component: ComponentNode };

export function WorkflowCanvas({ variant, execution, checkpoint, components, selectedNode, onSelect, compact = false }: {
  variant: Variant; execution?: Execution; checkpoint?: Checkpoint; components: ComponentDef[];
  selectedNode?: string; onSelect?: (id: string) => void; compact?: boolean;
}) {
  const cursor = checkpoint?.cursor ?? execution?.cursor ?? 0;
  const model = execution?.graph ?? variant;
  const { nodes, edges } = useMemo(() => {
    const complete = model.route.slice(0, cursor);
    const current = execution && !checkpoint && busy(execution) ? model.route[cursor] : undefined;
    const pathEdges = new Set(model.route.slice(1, cursor).map((n, i) => `${model.route[i]}-${n}`));
    const nodes: CardNode[] = model.nodes.map(n => {
      const def = components.find(c => c.id === n.componentId)!;
      let status = complete.includes(n.id) ? 'completed' : 'pending';
      if (current === n.id) status = 'running';
      if (!checkpoint && execution?.status === 'failed' && model.route[cursor] === n.id) status = 'failed';
      if (!checkpoint && execution?.status === 'paused' && model.route[cursor] === n.id) status = 'paused';
      if (execution?.status === 'completed' && !complete.includes(n.id)) status = 'skipped';
      const elapsed = execution?.nodeRuns.filter(r => r.nodeId === n.id && r.epoch === (checkpoint?.epoch ?? execution.epoch)).reduce((sum, r) => sum + r.elapsedMs, 0) ?? 0;
      return { id: n.id, type: 'component', position: { x: n.x, y: n.y }, selected: n.id === selectedNode,
        data: { label: def.name, componentId: n.componentId, implementation: n.implementation, status, breakpoint: execution?.breakpoints.includes(n.id) ?? false, elapsed: checkpoint ? '快照' : elapsed ? `${elapsed / 1000}s` : '—' } };
    });
    const edges = model.edges.map(e => ({ ...e, type: 'smoothstep', animated: !compact && busyOrFalse(execution) && !checkpoint && e.target === current,
      markerEnd: { type: MarkerType.ArrowClosed, color: pathEdges.has(`${e.source}-${e.target}`) ? '#15805c' : '#8794a8' },
      style: { stroke: pathEdges.has(`${e.source}-${e.target}`) ? '#15805c' : '#a5afbd', strokeWidth: 1.7, strokeDasharray: e.conditional ? '5 4' : undefined },
      labelStyle: { fontSize: 10, fill: '#566377' }, labelBgStyle: { fill: '#f8fafc' },
    }));
    return { nodes, edges };
  }, [model, cursor, execution, checkpoint, components, selectedNode, compact]);
  return <div className={`graph-canvas ${compact ? 'compact' : ''}`} data-testid="workflow-canvas">
    <ReactFlow key={`${model.id}-${compact}`} nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView fitViewOptions={{ padding: 0.18 }}
      minZoom={0.25} maxZoom={1.5} nodesDraggable={false} nodesConnectable={false} edgesReconnectable={false}
      deleteKeyCode={null} onNodeClick={(_, node) => onSelect?.(node.id)} proOptions={{ hideAttribution: false }}>
      <Background color="#dbe1eb" gap={20} size={1} />
      {!compact && <><Controls showInteractive={false} /><MiniMap pannable zoomable nodeColor="#cbd8ef" /></>}
    </ReactFlow>
  </div>;
}
const busyOrFalse = (e?: Execution) => e ? busy(e) : false;
