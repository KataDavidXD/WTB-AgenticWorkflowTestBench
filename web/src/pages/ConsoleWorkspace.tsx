import { useState, type ReactNode } from "react";
import type { Checkpoint, Variables } from "../domain";
import { History, EventTable } from "../components/History";
import {
  BatchPage,
  ComponentLibrary,
  ComparePage,
  SystemPage,
} from "../components/Pages";
import { Modal } from "../components/common";
import { useRemote } from "../useRemote";
import { useStudio } from "./StudioProvider";
import { StudioMasthead } from "./StudioMasthead";
import { StudioOverlays } from "./StudioOverlays";
import "./console-workspace.css";

function object(text: string): Variables {
  const value = JSON.parse(text);
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("输入必须是 JSON 对象");
  return value;
}
export function ConsoleWorkspace({ children }: { children: ReactNode }) {
  const {
    catalog,
    executions,
    state,
    dispatch,
    ready,
    connection,
    busy,
    revision,
    start,
    operate,
    notify,
    setModal,
  } = useStudio();
  const [tab, setTab] = useState("studio"),
    [dialog, setDialog] = useState<"start" | "edit" | "breakpoints" | null>(
      null,
    ),
    [variant, setVariant] = useState(""),
    [text, setText] = useState("{}"),
    [points, setPoints] = useState<string[]>([]),
    [overrides, setOverrides] = useState<Record<string, string>>({}),
    [error, setError] = useState("");
  const execution = executions.find(
    (e) =>
      e.id === state.selectedRun && e.variantId === state.selectedVariantId,
  );
  const cp = useRemote<Checkpoint>(
    execution && state.selectedCp
      ? `/executions/${execution.id}/checkpoints/${state.selectedCp}`
      : undefined,
    revision,
  );
  const chosen =
      catalog?.variants.find((v) => v.id === variant) || catalog?.variants[0],
    project = catalog?.projects.find((p) => p.id === chosen?.projectId);
  const disabled = busy || connection !== "connected";
  const choose = (id: string) => {
    setVariant(id);
    const v = catalog?.variants.find((v) => v.id === id),
      p = catalog?.projects.find((p) => p.id === v?.projectId);
    setText(JSON.stringify(p?.initialState || {}, null, 2));
    setPoints([]);
    setOverrides({});
  };
  const open = (kind: "start" | "edit" | "breakpoints") => {
    setError("");
    if (kind === "start")
      choose(state.selectedVariantId || catalog?.variants[0]?.id || "");
    else {
      setText(JSON.stringify(execution?.state || {}, null, 2));
      setPoints(execution?.breakpoints || []);
    }
    setDialog(kind);
  };
  const submit = async () => {
    try {
      setError("");
      if (dialog === "start")
        await start(
          chosen!.id,
          object(text),
          points,
          Object.fromEntries(
            Object.entries(overrides).filter(([, value]) => value),
          ),
        );
      else if (execution)
        await operate(
          execution.id,
          dialog === "edit" ? "state" : "breakpoints",
          dialog === "edit" ? { state: object(text) } : { nodes: points },
        );
      setDialog(null);
      if (dialog === "start") setTab("execution");
    } catch (e) {
      setError(String(e));
    }
  };
  const command = (action: string) => {
    if (!execution) return;
    if (action === "edit") open("edit");
    else
      void operate(
        execution.id,
        action === "checkpoint" ? "checkpoints" : action,
      ).catch(() => {});
  };
  return (
    <>
      <div className="console-navigation">
        <nav aria-label="控制台功能">
          {[
            ["studio", "结构视图"],
            ["execution", "执行与文件"],
            ["batch", "批量实验"],
            ["components", "组件库"],
            ["compare", "变体对照"],
            ["audit", "审计"],
            ["system", "系统"],
          ].map(([id, label]) => (
            <button
              key={id}
              className={tab === id ? "active" : ""}
              onClick={() => setTab(id)}
            >
              {label}
            </button>
          ))}
        </nav>
        <span role="status">
          {connection === "connected"
            ? "已连接"
            : connection === "offline"
              ? "连接中断"
              : "正在连接"}
        </span>
        <button
          className="accent-btn"
          disabled={!ready || !catalog?.variants.length || disabled}
          onClick={() => open("start")}
        >
          新建执行
        </button>
      </div>
      {tab === "studio" ? (
        children
      ) : (
        <div className="shell console-workspace">
          <StudioMasthead />
          {tab === "execution" && (
            <>
              <h1>执行与文件</h1>
              <label>
                执行实例{" "}
                <select
                  aria-label="所有执行实例"
                  value={execution?.id || ""}
                  onChange={(e) =>
                    dispatch({ type: "run", id: e.target.value })
                  }
                >
                  <option value="" disabled>
                    请选择执行
                  </option>
                  {executions.map((e) => (
                    <option key={e.id} value={e.id}>
                      {e.variantId} · {e.id} · {e.status}
                    </option>
                  ))}
                </select>
              </label>
              {!execution ? (
                <p>当前变体尚无执行。点击“新建执行”开始。</p>
              ) : (
                <>
                  <p>
                    状态：
                    <strong data-testid="execution-status">
                      {execution.status}
                    </strong>{" "}
                    · {execution.id} · {execution.runtime.environment}
                  </p>
                  {execution.error && <p role="alert">{execution.error}</p>}
                  <div className="toolbar">
                    <button
                      disabled={
                        disabled ||
                        !!execution.pendingOperation ||
                        execution.status !== "paused"
                      }
                      onClick={() => command("resume")}
                    >
                      继续运行
                    </button>
                    <button
                      disabled={
                        disabled ||
                        !["running", "queued"].includes(execution.status)
                      }
                      onClick={() => command("pause")}
                    >
                      暂停运行
                    </button>
                    <button
                      disabled={
                        disabled ||
                        !["running", "queued", "paused"].includes(
                          execution.status,
                        )
                      }
                      onClick={() => command("stop")}
                    >
                      停止执行
                    </button>
                    <button
                      disabled={
                        disabled ||
                        execution.status !== "paused" ||
                        !!execution.pendingOperation
                      }
                      onClick={() => open("breakpoints")}
                    >
                      设置断点
                    </button>
                    <button
                      disabled={
                        disabled ||
                        execution.status !== "paused" ||
                        !!execution.pendingOperation
                      }
                      onClick={() => open("edit")}
                    >
                      编辑状态
                    </button>
                    <button
                      disabled={
                        disabled ||
                        execution.status !== "paused" ||
                        !!execution.pendingOperation
                      }
                      onClick={() => command("checkpoint")}
                    >
                      创建检查点
                    </button>
                    <button
                      disabled={
                        disabled ||
                        !execution.checkpointId ||
                        ["running", "queued", "pausing", "stopping"].includes(
                          execution.status,
                        )
                      }
                      onClick={() => setModal("rollback")}
                    >
                      回退到所选检查点
                    </button>
                    <button
                      disabled={
                        disabled ||
                        !execution.checkpointId ||
                        ["running", "queued", "pausing", "stopping"].includes(
                          execution.status,
                        )
                      }
                      onClick={() => setModal("fork")}
                    >
                      从所选检查点 Fork
                    </button>
                  </div>
                  {execution.status === "failed" && (
                    <p>选择有效检查点后回退，可修改状态并继续运行。</p>
                  )}
                  {cp.error && <p role="alert">{cp.error}</p>}
                  <History
                    key={execution.id}
                    execution={execution}
                    checkpoint={cp.data}
                    revision={revision}
                    onCheckpoint={(id) =>
                      dispatch({ type: "checkpoint", id: id || "" })
                    }
                    onExecution={(id) => dispatch({ type: "run", id })}
                    command={command}
                  />
                </>
              )}
            </>
          )}
          {catalog && tab === "batch" && (
            <BatchPage
              catalog={catalog}
              revision={revision}
              onExecution={(id) => {
                dispatch({ type: "run", id });
                setTab("execution");
              }}
            />
          )}
          {catalog && tab === "components" && (
            <ComponentLibrary catalog={catalog} />
          )}
          {catalog && tab === "compare" && (
            <ComparePage variants={catalog.variants} />
          )}
          {tab === "audit" && (
            <section className="panel panel-body">
              <h1>审计事件</h1>
              <EventTable revision={revision} />
            </section>
          )}
          {tab === "system" && <SystemPage revision={revision} />}
          <StudioOverlays />
        </div>
      )}
      {dialog && (
        <Modal
          title={
            dialog === "start"
              ? "启动工作流"
              : dialog === "edit"
                ? "编辑执行状态"
                : "设置执行断点"
          }
          onClose={() => setDialog(null)}
        >
          {dialog === "start" && (
            <>
              <label>
                项目与变体
                <select
                  aria-label="启动变体"
                  value={chosen?.id || ""}
                  onChange={(e) => choose(e.target.value)}
                >
                  {catalog?.variants.map((v) => (
                    <option key={v.id} value={v.id}>
                      {v.projectId} / {v.name} · {v.runtimeBackend}
                    </option>
                  ))}
                </select>
              </label>
              {Object.entries(project?.nodeVariants || {}).map(
                ([node, choices]) => (
                  <label key={node}>
                    {node} 实现
                    <select
                      aria-label={node + " 实现"}
                      value={overrides[node] || ""}
                      onChange={(e) =>
                        setOverrides({ ...overrides, [node]: e.target.value })
                      }
                    >
                      <option value="">使用变体默认实现</option>
                      {choices.map((choice) => (
                        <option key={choice} value={choice}>
                          {choice}
                        </option>
                      ))}
                    </select>
                  </label>
                ),
              )}
            </>
          )}
          {dialog !== "breakpoints" && (
            <label>
              {dialog === "start" ? "初始状态" : "状态 JSON"}
              <textarea
                aria-label="状态 JSON"
                rows={8}
                value={text}
                onChange={(e) => setText(e.target.value)}
              />
            </label>
          )}
          {dialog !== "edit" && (
            <fieldset>
              <legend>节点前断点</legend>
              {(dialog === "start" ? chosen?.nodes : execution?.graph.nodes)
                ?.filter((n) => !n.id.startsWith("__"))
                .map((n) => (
                  <label key={n.id}>
                    <input
                      type="checkbox"
                      aria-label={"断点 " + n.id}
                      checked={points.includes(n.id)}
                      onChange={(e) =>
                        setPoints(
                          e.target.checked
                            ? [...points, n.id]
                            : points.filter((p) => p !== n.id),
                        )
                      }
                    />
                    {n.id}
                  </label>
                ))}
            </fieldset>
          )}
          {error && <p role="alert">{error}</p>}
          <div className="toolbar">
            <button
              disabled={busy}
              onClick={() => {
                setDialog(null);
                notify("操作已取消");
              }}
            >
              取消
            </button>
            <button
              className="accent-btn"
              disabled={disabled}
              onClick={() => void submit()}
            >
              {dialog === "start" ? "启动执行" : "保存"}
            </button>
          </div>
        </Modal>
      )}
    </>
  );
}
