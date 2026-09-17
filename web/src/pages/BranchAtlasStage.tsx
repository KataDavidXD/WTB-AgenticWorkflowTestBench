import { Fragment } from 'react';
import { nodes, STATUS, statusColor } from './StudioDemoData';
import { useStudio } from './StudioProvider';
import { selectedRun, selectedVariant, type Attempt, type Checkpoint, type DemoState } from './StudioState';
import { Hit, Mono, Pill, StudioSvg, Text } from './StudioSvg';

export function lineageGeometry(state: DemoState) {
  const ns = nodes(selectedVariant(state)), familyRuns = state.runs.filter(r => r.variantId === selectedVariant(state).id), ids = new Set(familyRuns.map(r => r.id)), tracks = state.tracks.filter(t => ids.has(t.runId));
  return { ns, familyRuns, tracks, height: Math.max(453, 75 + tracks.length * 91), X: (step: number) => 250 + step * ((1255 - 250) / (ns.length || 1)), Y: (t: Attempt) => 117 + tracks.findIndex(q => q.id === t.id) * 91 };
}
type Geometry = ReturnType<typeof lineageGeometry>;
export function BranchAtlasParentConnector({ t, geometry }: { t: Attempt; geometry: Geometry }) {
  const { state } = useStudio(); if (!t.origin) return null;
  const src = state.tracks.find(q => q.id === t.origin!.trackId), cp = state.checkpoints[t.origin.cpId]; if (!src || !geometry.tracks.some(q => q.id === src.id)) return null;
  const x = geometry.X(cp.step), sy = geometry.Y(src), dy = geometry.Y(t), col = t.kind === 'rollback' ? 'var(--accent)' : 'var(--ink)';
  return <><path d={`M${x},${sy} C${x + 24},${sy} ${x + 24},${dy} ${x},${dy}`} fill="none" stroke={col} strokeWidth={1.5} strokeDasharray={t.kind === 'rollback' ? '4 4' : undefined} /><Mono x={x + 32} y={(sy + dy) / 2 + 3} size={8} color={col}>{t.kind === 'rollback' ? 'REWIND' : 'FORK'}</Mono></>;
}
export function BranchAtlasCheckpoint({ cp, runId, x, y, color }: { cp: Checkpoint; runId: string; x: number; y: number; color: string }) {
  const { state, dispatch } = useStudio(), chosen = cp.id === state.selectedCp && runId === state.selectedRun;
  return <Hit label={`${cp.id} · ${cp.node} 后 · ${cp.tracked} 个跟踪文件`} onClick={() => dispatch({ type: 'checkpoint', id: cp.id, runId })}><rect x={x - 28} y={y - 19} width={56} height={53} fill="transparent" />{chosen && <circle cx={x} cy={y} r={16} fill="var(--accent-soft)" stroke="var(--accent)" strokeWidth={1} />}<circle className="hover-stroke" cx={x} cy={y} r={chosen ? 8 : 6} fill={chosen ? 'var(--accent)' : color} stroke="var(--bg)" strokeWidth={2} /><Mono x={x} y={y + 27} size={8} color={chosen ? 'var(--accent)' : 'var(--muted)'} textAnchor="middle">{cp.id.replace('cp-', '')}</Mono></Hit>;
}
export function BranchAtlasAttemptTrack({ t, geometry: { X, Y, ns } }: { t: Attempt; geometry: Geometry }) {
  const { state, dispatch } = useStudio(), r = selectedRun(state), rr = state.runs.find(q => q.id === t.runId)!, active = t.id === rr.trackId, sel = t.id === r.trackId, y = Y(t), doneCps = t.cps.map(id => state.checkpoints[id]), lastStep = doneCps.length ? doneCps.at(-1)!.step : t.baseStep, color = sel ? 'var(--accent)' : active ? 'var(--ink)' : 'var(--muted)', xp = X(Math.min(ns.length, lastStep + .46));
  return <><Hit label={`${rr.id} · ${t.label}`} onClick={() => dispatch({ type: 'run', id: rr.id })}>{sel && <rect x={-2} y={y - 31} width={198} height={67} rx={4} fill="var(--surface)" />}<Mono x={10} y={y - 9} size={12} color={sel ? 'var(--accent)' : 'var(--ink)'}>{rr.id}</Mono><Text x={10} y={y + 10} size={10} color="var(--muted)">{t.label}{active ? '' : ' · 历史保留'}</Text><Mono x={10} y={y + 28} size={8}>{`${rr.mode === 'ray' ? 'RAY' : 'LOCAL'} / attempt ${state.tracks.filter(a => a.runId === rr.id).indexOf(t) + 1}`}</Mono></Hit>
    <line x1={X(t.baseStep)} y1={y} x2={X(ns.length)} y2={y} stroke="var(--line)" strokeDasharray="4 6" />
    {lastStep > t.baseStep && <path d={`M${X(t.baseStep)},${y} L${X(lastStep)},${y}`} stroke={color} strokeWidth={sel ? 2.7 : 1.7} fill="none" />}
    {t.origin && <Hit label={`${rr.id} 起点 · ${t.origin.cpId}`} onClick={() => dispatch({ type: 'checkpoint', id: t.origin!.cpId, runId: rr.id })}><circle cx={X(t.baseStep)} cy={y} r={6} fill="var(--bg)" stroke={color} strokeWidth={2} /></Hit>}
    {doneCps.map(cp => <BranchAtlasCheckpoint key={cp.id} cp={cp} runId={rr.id} x={X(cp.step)} y={y} color={color} />)}
    {active && <>{Array.from({ length: ns.length - lastStep }, (_, i) => lastStep + 1 + i).map(j => <circle key={j} cx={X(j)} cy={y} r={4} fill="var(--bg)" stroke="var(--line)" />)}{['running', 'pausing'].includes(rr.status) ? <><path className="flowing" d={`M${X(lastStep) + 9},${y} L${xp},${y}`} fill="none" stroke={color} strokeWidth={2.5} /><circle cx={xp} cy={y} r={5} fill={color} /><circle className="pulse" cx={xp} cy={y} r={10} fill="none" stroke={color} /><Text x={xp} y={y - 18} size={10} color={color} textAnchor="middle">{ns[rr.step] || ''} 执行中</Text></> : <Pill x={Math.min(1272, X(lastStep) + 19)} y={y - 21} text={STATUS[rr.status]} color={statusColor(rr.status)} />}</>}
  </>;
}
export function BranchAtlasStage() {
  const { state } = useStudio(), geometry = lineageGeometry(state), { height: H, X, ns, tracks } = geometry;
  return <StudioSvg height={H} label="检查点与执行分支谱系"><rect x={215} y={48} width={1083} height={H - 91} fill="url(#dots)" /><Mono x={0} y={24} size={9}>RUN / ATTEMPT</Mono><Mono x={216} y={24} size={9}>LOGICAL PROGRESS →</Mono>
    {Array.from({ length: ns.length + 1 }, (_, i) => <Fragment key={i}><line x1={X(i)} y1={60} x2={X(i)} y2={H - 35} stroke="var(--line)" strokeDasharray="2 6" /><Mono x={X(i)} y={49} size={9} textAnchor="middle">{i === 0 ? '∅ 初始' : `${ns[i - 1]} 后`}</Mono></Fragment>)}
    {tracks.map(t => <BranchAtlasParentConnector key={t.id} t={t} geometry={geometry} />)}{tracks.map(t => <BranchAtlasAttemptTrack key={t.id} t={t} geometry={geometry} />)}
    <Mono x={0} y={H - 4} size={8}>PERSISTED HISTORY</Mono><Text x={215} y={H - 4} size={10} color="var(--muted)">实心点是可选择的持久化检查点；新分支必须从这些点出发。</Text>
  </StudioSvg>;
}
