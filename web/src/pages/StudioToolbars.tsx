import { COMPONENTS, variants } from "./StudioDemoData";
import { useStudio } from "./StudioProvider";
import { selectedRun, selectedVariant } from "./StudioState";
export function PathLoomToolbar() {
  const { state, dispatch } = useStudio();
  return (
    <>
      <div className="left">
        <span className="upper">VARIANT WEAVE</span>
        <div className="segmented">
          <button
            className={`mini-tab ${!state.focus ? "active" : ""}`}
            onClick={() => dispatch({ type: "focus", value: false })}
          >
            全部变体
          </button>
          <button
            className={`mini-tab ${state.focus ? "active" : ""}`}
            onClick={() => dispatch({ type: "focus", value: true })}
          >
            聚焦当前
          </button>
        </div>
      </div>
      <div className="right">
        {[
          ["ok", "完成"],
          ["accent", "运行 / 暂停"],
          ["bad", "失败"],
          ["idle", "未运行"],
        ].map(([color, text]) => (
          <span className="legend" key={color}>
            <i className="dot" style={{ background: `var(--${color})` }} />
            {text}
          </span>
        ))}
      </div>
    </>
  );
}
export function BranchAtlasToolbar() {
  const { state } = useStudio();
  return (
    <>
      <div className="left">
        <span className="upper">
          {selectedVariant(state).name} / EXECUTION LINEAGE
        </span>
        <span className="small muted">横轴：逻辑节点边界 · 不是物理时间</span>
      </div>
      <div className="right">
        <span className="legend">
          <i className="dot" style={{ background: "var(--ink)" }} />
          已有检查点
        </span>
        <span>○ 尚未完成</span>
        <span className="muted">实时状态由 WTB 推送</span>
      </div>
    </>
  );
}
export function ExecutionSectionToolbar() {
  const { state, setDrawer } = useStudio(),
    r = selectedRun(state);
  return (
    <>
      <div className="left">
        <span className="upper">
          {r.id} / {r.mode === "ray" ? "RAY DISTRIBUTED" : "LOCAL PROCESS"}
        </span>
        <span className="small muted">点击任意节点，沿映射下钻</span>
      </div>
      <div className="right">
        <span>
          <i className="legend-line" />
          实际分配
        </span>
        <span>┄ 待调度 / 状态继承</span>
        <button className="text-btn" onClick={() => setDrawer(true)}>
          检查完整路径 ↗
        </button>
      </div>
    </>
  );
}
function DimensionSelect({ axis }: { axis: "xDim" | "yDim" }) {
  const { state, dispatch } = useStudio();
  return (
    <select
      id={axis === "xDim" ? "x-dim" : "y-dim"}
      className="mini-select"
      value={state[axis]}
      onChange={(e) =>
        dispatch({ type: "axis", axis, value: Number(e.target.value) })
      }
    >
      {COMPONENTS.map((component, i) => (
        <option key={component.id} value={i}>
          {component.id} {component.name}
        </option>
      ))}
    </select>
  );
}
export function VariantAtlasToolbar() {
  const { state, dispatch } = useStudio();
  return (
    <>
      <div className="left">
        <label className="axis-label">
          横轴 <DimensionSelect axis="xDim" />
        </label>
        <label className="axis-label">
          纵轴 <DimensionSelect axis="yDim" />
        </label>
        <label className="axis-label">
          比较基线{" "}
          <select
            id="baseline-select"
            className="mini-select"
            value={state.baseline}
            onChange={(e) => dispatch({ type: "baseline", id: e.target.value })}
          >
            {variants.map((v) => (
              <option key={v.id} value={v.id}>
                {v.name}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="right">
        <span>环段 = 已注册组件组成</span>
        <span>距离无数值含义</span>
      </div>
    </>
  );
}
