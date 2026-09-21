import { COMPONENTS, variants } from "./StudioDemoData";
import { useStudio } from "./StudioProvider";
export function StudioHero() {
  const { state } = useStudio();
  const data = {
    loom: [
      "Workflow,",
      "woven.",
      "把已注册变体放到同一张图里。看共同路径，也看每一次偏离。",
      "01 / CONFIGURATION × PATH",
    ],
    lineage: [
      "History,",
      "unfolded.",
      "每一次尝试都有来处。回退留下轨迹，Fork 长出新分支。",
      "02 / CHECKPOINT × LINEAGE",
    ],
    section: [
      "Execution,",
      "exposed.",
      "从逻辑节点，一直剖开到 Actor、主机、工作目录与 Python 环境。",
      "03 / LOGIC × RUNTIME × ISOLATION",
    ],
    atlas: [
      "Possibility,",
      "mapped.",
      "不是一列变体名字，而是一张可探索的配置空间。",
      "04 / CONFIGURATION × COVERAGE",
    ],
  }[state.view];
  return (
    <section className="hero" id="hero">
      <div>
        <div className="eyebrow">
          REGISTERED WORKFLOW &nbsp; / &nbsp; {data[3]}
        </div>
        <h1>
          {data[0]} <em>{data[1]}</em>
        </h1>
        <p>{data[2]}</p>
      </div>
      <div className="hero-stats">
        {[
          [COMPONENTS.length, "组件类型"],
          [variants.length, "定义变体"],
          [state.runs.length, "运行实例"],
          [
            state.runs.filter((r) => ["running", "pausing"].includes(r.status))
              .length,
            "正在执行",
          ],
        ].map(([count, label]) => (
          <div key={label}>
            <b>{String(count).padStart(2, "0")}</b>
            <small>{label}</small>
          </div>
        ))}
      </div>
    </section>
  );
}
