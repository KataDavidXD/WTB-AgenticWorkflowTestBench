import { STATUS } from "./StudioDemoData";
import { useStudio } from "./StudioProvider";
import { selectedRun, selectedVariant, type DemoState } from "./StudioState";
import { Hit, Mono, StudioSvg, Text } from "./StudioSvg";
export function lineageGeometry(state: DemoState) {
  const familyRuns = state.runs.filter(
      (r) => r.variantId === selectedVariant(state).id,
    ),
    ids = new Set(familyRuns.map((r) => r.id)),
    tracks = state.tracks.filter((t) => ids.has(t.runId));
  return {
    familyRuns,
    tracks,
    height: Math.max(453, 100 + tracks.length * 110),
  };
}
export function BranchAtlasStage() {
  const { state, dispatch } = useStudio(),
    { tracks, height } = lineageGeometry(state),
    run = selectedRun(state),
    maximum = Math.max(
      1,
      ...Object.values(state.checkpoints).map((c) => c.clock + 1),
    );
  const X = (index: number) => 260 + ((index + 1) * 1000) / (maximum + 1),
    Y = (index: number) => 105 + index * 110;
  return (
    <StudioSvg height={height} label="检查点与执行分支谱系">
      <Mono x={0} y={25}>
        RUN / ATTEMPT
      </Mono>
      <Text x={260} y={25}>
        按持久化顺序排列 · 同一节点可多次执行 · 选择检查点查看状态
      </Text>
      {!tracks.length && (
        <Text x={260} y={120}>
          当前变体尚无执行，点击“新建执行”开始。
        </Text>
      )}
      {tracks.map((t, index) => {
        const r = state.runs.find((r) => r.id === t.runId)!,
          cps = t.cps.map((id) => state.checkpoints[id]).filter(Boolean),
          y = Y(index),
          source = t.origin && state.checkpoints[t.origin.cpId],
          parent = source
            ? tracks.findIndex((a) => a.id === source.trackId)
            : -1;
        return (
          <g key={t.id}>
            <Hit
              label={`${r.id} · ${t.label}`}
              onClick={() => dispatch({ type: "run", id: r.id })}
            >
              <Mono
                x={0}
                y={y - 12}
                color={r.id === run.id ? "var(--accent)" : "var(--ink)"}
              >
                <title>{r.id}</title>
                {r.id.slice(0, 18)}…
              </Mono>
              <Text x={0} y={y + 8}>
                {t.label}
                {t.active ? "" : " · 历史保留"}
              </Text>
              <Mono x={0} y={y + 28}>
                {t.active ? STATUS[r.status] : "历史尝试"}
              </Mono>
            </Hit>
            <line x1={250} y1={y} x2={1310} y2={y} stroke="var(--line)" />
            {source && parent >= 0 && (
              <path
                d={`M${X(source.clock)},${Y(parent)} C${X(source.clock) + 25},${Y(parent)} 250,${y} 260,${y}`}
                stroke="var(--accent)"
                fill="none"
                strokeDasharray="4 4"
              />
            )}
            {cps.map((cp) => (
              <Hit
                key={cp.id}
                label={`${cp.id} · ${cp.node} 后 · ${cp.tracked} 个跟踪文件`}
                onClick={() => dispatch({ type: "checkpoint", id: cp.id })}
              >
                <circle
                  cx={X(cp.clock)}
                  cy={y}
                  r={state.selectedCp === cp.id ? 9 : 6}
                  fill={
                    state.selectedCp === cp.id ? "var(--accent)" : "var(--ink)"
                  }
                />
                <Mono x={X(cp.clock)} y={y + 23} textAnchor="middle" size={8}>
                  {cp.node}
                </Mono>
                <Mono x={X(cp.clock)} y={y + 40} textAnchor="middle" size={7}>
                  {cp.id.slice(0, 8)}
                </Mono>
              </Hit>
            ))}
          </g>
        );
      })}
      <Mono x={0} y={height - 12}>
        PERSISTED HISTORY
      </Mono>
    </StudioSvg>
  );
}
