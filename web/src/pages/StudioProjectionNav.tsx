import { useStudio } from "./StudioProvider";
import { projections } from "./StudioTypes";
export function StudioProjectionNav() {
  const { state, dispatch } = useStudio();
  return (
    <nav className="study-tabs" aria-label="四种可视化视角">
      {projections.map((p) => (
        <button
          key={p.id}
          className={`study-tab ${p.id === state.view ? "active" : ""}`}
          data-view={p.id}
          aria-current={p.id === state.view ? "page" : false}
          onClick={() => dispatch({ type: "view", view: p.id })}
        >
          <span className="n">{p.number}</span>
          <span>
            <strong>{p.label}</strong>
            <small>{p.english}</small>
          </span>
        </button>
      ))}
      <span className="tab-note">ONE MODEL / FOUR PROJECTIONS</span>
    </nav>
  );
}
