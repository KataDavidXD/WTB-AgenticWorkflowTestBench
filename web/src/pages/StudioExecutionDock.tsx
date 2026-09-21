import { STATUS } from "./StudioDemoData";
import { useStudio } from "./StudioProvider";
import {
  selectedCheckpoint,
  selectedRun,
  selectedVariant,
} from "./StudioState";
export function StudioExecutionDock() {
  const { state, dispatch, setModal, busy, connection, executions } =
      useStudio(),
    r = selectedRun(state),
    v = selectedVariant(state),
    cp = selectedCheckpoint(state),
    raw = executions.find((e) => e.id === r.id);
  const blocked = busy || connection !== "connected" || !!raw?.pendingOperation,
    settled =
      !!r.id &&
      !["queued", "running", "pausing", "stopping", "idle"].includes(r.status);
  const restore =
      settled &&
      cp.restorable &&
      !blocked &&
      (raw?.capabilities?.rollback ?? true),
    fork =
      settled && cp.restorable && !blocked && (raw?.capabilities?.fork ?? true);
  return (
    <section
      className="execution-dock"
      id="execution-dock"
      aria-label="运行与检查点控制"
    >
      <div className="selected-run">
        <strong>{v.name}</strong>
        <select
          className="run-select"
          id="run-select"
          aria-label="选择此变体的运行实例"
          value={r.id}
          onChange={(e) => dispatch({ type: "run", id: e.target.value })}
        >
          {!r.id && <option value="">尚无执行</option>}
          {state.runs
            .filter((e) => e.variantId === v.id)
            .map((e) => (
              <option key={e.id} value={e.id}>
                {e.id}
              </option>
            ))}
        </select>
        <div className="sub">
          <span className={`status-text ${r.status}`}>{STATUS[r.status]}</span>
          <span>当前尝试 · {r.step} 次已完成节点记录</span>
        </div>
      </div>
      <div>
        <label htmlFor="checkpoint-select">
          {state.selectedCp ? "预览历史检查点" : "当前执行检查点"}
        </label>
        <select
          id="checkpoint-select"
          className="checkpoint-list"
          aria-label="选择检查点"
          value={state.selectedCp}
          onChange={(e) => dispatch({ type: "checkpoint", id: e.target.value })}
        >
          <option value="">
            实时 · {r.checkpointId?.slice(0, 8) || "尚无检查点"}
          </option>
          {r.path
            .map((id) => state.checkpoints[id])
            .map((c) => (
              <option key={c.id} value={c.id}>
                {c.clock + 1} · {c.node === "∅" ? "初始状态" : c.node + " 后"} ·{" "}
                {c.id.slice(0, 8)} · {c.tracked} 文件
              </option>
            ))}
        </select>
      </div>
      <div className="dock-actions">
        <button
          className="primary-btn"
          disabled={blocked || !["running", "paused"].includes(r.status)}
          onClick={() => dispatch({ type: "pause" })}
        >
          {r.status === "running"
            ? "暂停"
            : r.status === "paused"
              ? "继续"
              : r.status === "failed"
                ? "请先回退"
                : r.status === "completed"
                  ? "运行完成"
                  : "等待执行"}
        </button>
        <button
          className="outline-btn"
          disabled={blocked || r.status !== "paused"}
          onClick={() => dispatch({ type: "step" })}
        >
          继续执行
        </button>
        <button
          className="outline-btn"
          disabled={!restore}
          onClick={() => setModal("rollback")}
        >
          回退
        </button>
        <button
          className="outline-btn"
          disabled={!fork}
          onClick={() => setModal("fork")}
        >
          Fork
        </button>
      </div>
    </section>
  );
}
