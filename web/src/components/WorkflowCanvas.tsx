import { useMemo } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  Handle,
  Position,
} from "@xyflow/react";
import type { Node, NodeProps } from "@xyflow/react";
import type { Checkpoint, Execution, Variant } from "../domain";

type Card = Node<{
  label: string;
  implementation: string;
  status: string;
  visits: number;
  elapsed: number;
  breakpoint: boolean;
}>;
function CardNode({ data }: NodeProps<Card>) {
  return (
    <div className={`graph-card ${data.status}`}>
      <Handle type="target" position={Position.Left} />
      <strong>{data.label}</strong>
      <div className="graph-implementation">{data.implementation}</div>
      <div>
        {data.breakpoint ? "● 断点 · " : ""}
        {
          (
            {
              running: "▶ 运行中",
              completed: "✓ 已完成",
              failed: "! 失败",
              pending: "○ 未执行",
              paused: "Ⅱ 下一步",
            } as Record<string, string>
          )[data.status]
        }
      </div>
      <small>
        {data.visits} 次记录 · {data.elapsed} ms
      </small>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}
const types = { card: CardNode };
export function WorkflowCanvas({
  model,
  execution,
  checkpoint,
  onSelect,
  compact = false,
}: {
  model: Variant;
  execution?: Execution;
  checkpoint?: Checkpoint;
  onSelect?: (id: string) => void;
  compact?: boolean;
}) {
  const nodes = useMemo(
    () =>
      model.nodes.map((n) => {
        const runs = (checkpoint?.nodeRuns ?? execution?.nodeRuns ?? []).filter(
          (r) =>
            r.nodeId === n.id &&
            (checkpoint ||
              !execution?.activeRunIds ||
              execution.activeRunIds.includes(r.id)),
        );
        const current = runs.at(-1);
        const next = checkpoint?.nextNodes ?? execution?.nextNodes ?? [];
        let status: string = current?.status ?? "pending";
        if (
          next.includes(n.id) &&
          (checkpoint || execution?.status === "paused")
        )
          status = "paused";
        return {
          id: n.id,
          type: "card",
          position: { x: n.x, y: n.y },
          data: {
            label: n.id,
            implementation: n.implementation,
            status,
            visits: runs.length,
            elapsed: runs.reduce((s, r) => s + r.elapsedMs, 0),
            breakpoint: execution?.breakpoints.includes(n.id) ?? false,
          },
        };
      }),
    [model, execution, checkpoint],
  );
  return (
    <div className={`graph-canvas ${compact ? "compact" : ""}`}>
      <ReactFlow
        key={model.id}
        nodes={nodes}
        edges={model.edges.map((e) => ({
          ...e,
          style: { strokeDasharray: e.conditional ? "5 4" : undefined },
        }))}
        nodeTypes={types}
        fitView
        nodesDraggable={false}
        nodesConnectable={false}
        deleteKeyCode={null}
        onNodeClick={(_, node) => onSelect?.(node.id)}
      >
        <Background />
        <Controls showInteractive={false} />
        <MiniMap />
      </ReactFlow>
    </div>
  );
}
