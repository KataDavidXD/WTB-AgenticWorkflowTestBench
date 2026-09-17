import { Fragment } from 'react';
import { COMPONENTS, CONFIGS, LABELS, NODESTATUS, STATUS, edges, nodes, statusColor, variants, type StudioVariant } from './StudioDemoData';
import { useStudio } from './StudioProvider';
import { latestRun, nodeContext, selectedVariant } from './StudioState';
import { Hit, Mono, StudioSvg, Text } from './StudioSvg';

export function loomGeometry() {
  const xs = COMPONENTS.map((_, i) => 217 + i * 176), seats: Record<string, number> = {};
  COMPONENTS.forEach(c => CONFIGS[c.id].forEach((o, j, opts) => { seats[`${c.id}:${o}`] = opts.length === 1 ? 202 : 105 + j * (267 / (opts.length - 1)); }));
  const pos = (v: StudioVariant, i: number) => ({ x: xs[i], y: seats[`${COMPONENTS[i].id}:${v.cfg[i]}`] + (Number(v.id.slice(1)) - 5.5) * 3.05 });
  const path = (v: StudioVariant) => { const y = 78 + (Number(v.id.slice(1)) - 1) * 36, points = [{ x: 138, y }, ...COMPONENTS.map((_, i) => pos(v, i)), { x: 1206, y }]; return points.reduce((s, b, i) => { if (!i) return `M ${b.x} ${b.y}`; const a = points[i - 1], m = (a.x + b.x) / 2; return s + ` C ${m} ${a.y}, ${m} ${b.y}, ${b.x} ${b.y}`; }, ''); };
  return { xs, seats, pos, path };
}
type Geometry = ReturnType<typeof loomGeometry>;
export function PathLoomConfigurationSlots({ geometry }: { geometry: Geometry }) {
  const { state, dispatch } = useStudio();
  return <>{COMPONENTS.map((c, i) => { const x = geometry.xs[i], selected = state.selectedNode === c.id; return <Fragment key={c.id}>
    <line x1={x} y1={61} x2={x} y2={392} stroke="var(--line)" />
    <Hit label={`${c.id} ${c.name}；查看当前变体的节点上下文`} onClick={() => dispatch({ type: 'node', id: c.id })}><circle className="hover-stroke" cx={x} cy={18} r={13} fill={selected ? 'var(--ink)' : 'var(--surface)'} stroke="var(--line)" /><Mono x={x} y={22} size={11} color={selected ? 'var(--bg)' : 'var(--ink)'} textAnchor="middle">{c.id}</Mono><Text x={x + 21} y={22}>{c.name}</Text><Mono x={x - 13} y={44} size={8}>{c.en}</Mono></Hit>
    {CONFIGS[c.id].map(o => { const y = geometry.seats[`${c.id}:${o}`]; return <Fragment key={String(o)}><rect x={x - 18} y={y - 22} width={36} height={44} rx={17} fill="var(--bg)" stroke="var(--line)" strokeDasharray={o === null ? '3 3' : '0'} /><Mono x={x + 24} y={y - 20} size={9}>{LABELS[String(o)]}</Mono></Fragment>; })}
  </Fragment>; })}</>;
}
export function PathLoomVariantTrack({ v, geometry }: { v: StudioVariant; geometry: Geometry }) {
  const { state, dispatch } = useStudio(), sel = v.id === selectedVariant(state).id, path = geometry.path(v);
  return <><path d={path} fill="none" stroke={sel ? 'var(--accent)' : 'var(--muted)'} strokeWidth={sel ? 3 : 1.15} opacity={sel ? 1 : state.focus ? .065 : .25} className="track-path" />
    {(!state.focus || sel) && <path d={path} fill="none" stroke="transparent" strokeWidth={8} className="hit" tabIndex={0} role="button" aria-label={`选择 ${v.name}`} onClick={() => dispatch({ type: 'variant', id: v.id })} onKeyDown={e => { if (['Enter', ' '].includes(e.key)) { e.preventDefault(); e.stopPropagation(); dispatch({ type: 'variant', id: v.id }); } }}><title>{v.name} · {v.label}</title></path>}
    {sel && COMPONENTS.map((c, i) => { const p = geometry.pos(v, i), ctx = nodeContext(state, c.id); return <Fragment key={c.id}><Hit label={`${c.id} ${c.name} · ${LABELS[String(v.cfg[i])]} · ${NODESTATUS[ctx.ns]}`} onClick={() => dispatch({ type: 'node', id: c.id })}><circle cx={p.x} cy={p.y} r={c.id === state.selectedNode ? 7 : 4.5} fill={v.cfg[i] === null || ['queued', 'paused'].includes(ctx.ns) ? 'var(--bg)' : 'var(--accent)'} stroke={v.cfg[i] !== null && ['queued', 'paused'].includes(ctx.ns) ? 'var(--accent)' : 'var(--bg)'} strokeWidth={2} /><circle cx={p.x} cy={p.y} r={14} fill="transparent" /></Hit>{ctx.ns === 'running' && <circle className="pulse" cx={p.x} cy={p.y} r={11} fill="none" stroke="var(--accent)" strokeWidth={2} />}</Fragment>; })}
    {sel && v.extra.map(([a, b]) => { const p = geometry.pos(v, COMPONENTS.findIndex(c => c.id === a)), q = geometry.pos(v, COMPONENTS.findIndex(c => c.id === b)); return <path key={a + b} d={`M ${p.x} ${p.y} C ${p.x + 60} ${p.y - 80}, ${q.x - 60} ${q.y - 80}, ${q.x} ${q.y}`} fill="none" stroke="var(--accent)" strokeWidth={1.6} strokeDasharray="4 4" />; })}
  </>;
}
export function PathLoomSelectedTopology() {
  const { state, dispatch } = useStudio(), v = selectedVariant(state), ns = nodes(v), x = 178, y = 438, gap = 90;
  return <><line x1={0} y1={418} x2={1360} y2={418} stroke="var(--line)" /><Mono x={0} y={443} size={8}>SELECTED TOPOLOGY</Mono>
    {edges(v).map(([a, b]) => { const ia = ns.indexOf(a), ib = ns.indexOf(b), xa = x + ia * gap, xb = x + ib * gap, extra = ib - ia > 1; return <path key={a + b} d={extra ? `M${xa},${y} Q${(xa + xb) / 2},${y - 35} ${xb},${y}` : `M${xa + 11},${y} L${xb - 12},${y}`} fill="none" stroke={extra ? 'var(--accent)' : 'var(--line)'} strokeWidth={1.2} markerEnd="url(#arrowhead)" />; })}
    {ns.map((c, i) => <Hit key={c} label={`${c} 节点`} onClick={() => dispatch({ type: 'node', id: c })}><circle className="hover-stroke" cx={x + i * gap} cy={y} r={10} fill={c === state.selectedNode ? 'var(--ink)' : 'var(--bg)'} stroke="var(--line)" /><Mono x={x + i * gap} y={y + 3} size={9} color={c === state.selectedNode ? 'var(--bg)' : 'var(--ink)'} textAnchor="middle">{c}</Mono></Hit>)}
    <Text x={878} y={442} size={10} color="var(--muted)">{`${v.label}  /  ${ns.length} 节点 · ${edges(v).length} 条边`}</Text>
  </>;
}
export function PathLoomStage() {
  const { state, dispatch } = useStudio(), v = selectedVariant(state), geometry = loomGeometry();
  return <StudioSvg label="组件配置路径织谱"><rect x={166} y={50} width={1004} height={353} fill="url(#dots)" /><Mono x={0} y={28} size={9}>VARIANTS / 10</Mono><Mono x={1220} y={28} size={9}>LATEST RUN</Mono><PathLoomConfigurationSlots geometry={geometry} />
    {[...variants.filter(item => item.id !== v.id), v].map(item => <PathLoomVariantTrack key={item.id} v={item} geometry={geometry} />)}
    {variants.map((item, i) => { const y = 78 + i * 36, r = latestRun(state, item), sel = item.id === v.id, color = statusColor(r.status); return <Hit key={item.id} label={`${item.name} · ${item.label} · ${STATUS[r.status]}`} onClick={() => dispatch({ type: 'variant', id: item.id })}>{sel && <rect x={-2} y={y - 18} width={131} height={31} rx={4} fill="var(--accent-soft)" />}<Mono x={7} y={y + 3} size={9} color={sel ? 'var(--accent)' : 'var(--muted)'}>{String(i + 1).padStart(2, '0')}</Mono><Text x={33} y={y + 3} color={sel ? 'var(--accent)' : 'var(--ink)'} fontWeight={sel ? 600 : undefined}>{item.name}</Text><circle cx={1206} cy={y} r={sel ? 5.5 : 3.5} fill={color} /><Text x={1221} y={y + 3} size={10} color={color}>{STATUS[r.status]}</Text><Mono x={1298} y={y + 3} size={9}>{r.mode === 'ray' ? 'Ray' : 'Local'}</Mono></Hit>; })}<PathLoomSelectedTopology />
  </StudioSvg>;
}
