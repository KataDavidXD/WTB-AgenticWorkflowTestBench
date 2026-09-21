import { useState } from "react";
import type { Checkpoint, Execution, Variant } from "../domain";
import { JsonView, PathField } from "./common";

export function Inspector({
  execution,
  graph,
  nodeId,
  checkpoint,
  breakpoint,
}: {
  execution?: Execution;
  graph: Variant;
  nodeId?: string;
  checkpoint?: Checkpoint;
  breakpoint: (node: string) => void;
}) {
  const [tab, setTab] = useState("node");
  const [copyStatus, setCopyStatus] = useState("");
  const node = graph.nodes.find((n) => n.id === nodeId);
  const copy = (path: string) => {
    void navigator.clipboard
      .writeText(path)
      .then(() => setCopyStatus("已复制"))
      .catch(() => setCopyStatus("复制失败，请手动复制"));
  };
  return (
    <aside className="panel inspector">
      <div className="tabs">
        {[
          ["node", "节点"],
          ["runtime", "运行资源"],
          ["env", "环境"],
        ].map(([id, label]) => (
          <button
            key={id}
            className={tab === id ? "active" : ""}
            onClick={() => setTab(id)}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="panel-body">
        {tab === "node" && (
          <>
            <h3>{node?.id ?? "点击节点查看详情"}</h3>
            {node && (
              <>
                <p>实现：{node.implementation}</p>
                <p>版本与标签：未提供</p>
                <button
                  disabled={
                    !execution ||
                    execution.status !== "paused" ||
                    !!execution.pendingOperation ||
                    !!checkpoint ||
                    node.id.startsWith("__")
                  }
                  title="暂停后可修改下一次继续执行的断点"
                  onClick={() => breakpoint(node.id)}
                >
                  {execution?.breakpoints.includes(node.id)
                    ? "移除断点"
                    : "设置断点"}
                </button>
                <JsonView
                  value={(
                    checkpoint?.nodeRuns ??
                    execution?.nodeRuns ??
                    []
                  ).filter((r) => r.nodeId === node.id)}
                />
              </>
            )}
          </>
        )}
        {tab === "runtime" &&
          (execution ? (
            <>
              <p>本地 · {execution.runtime.host}</p>
              <p>PID：{execution.runtime.pid}</p>
              <p>路径位于：{execution.runtime.pathLocation}</p>
              <PathField
                label="Workspace"
                path={execution.runtime.workspace}
                onCopy={copy}
              />
              <PathField
                label="输出目录"
                path={execution.runtime.output}
                onCopy={copy}
              />
              <p>{copyStatus}</p>
              <p className="muted">
                Ray Actor：当前未接入。CPU/GPU 用量：未采集。
              </p>
            </>
          ) : (
            <p>尚未选择执行</p>
          ))}
        {tab === "env" &&
          (execution ? (
            <>
              <p>{execution.runtime.environment}</p>
              <PathField
                label="实际解释器"
                path={execution.runtime.interpreter}
                onCopy={copy}
              />
              <p>Python {execution.runtime.pythonVersion}</p>
              <p>独立 venv / 远程环境：当前不可用</p>
              <p>没有指定目标解释器，匹配检查不适用。</p>
            </>
          ) : (
            <p>尚未选择执行</p>
          ))}
      </div>
    </aside>
  );
}
