import { Fragment } from 'react';
import { COMPONENTS, LABELS, NODESTATUS, edges } from './StudioDemoData';
import { useStudio } from './StudioProvider';
import { nodeContext, selectedVariant } from './StudioState';
import { Hit, Mono, StudioSvg, Text } from './StudioSvg';
const Y = { definition: 86, executor: 247, isolation: 373 };
type CardProps = { x: number; ctx: ReturnType<typeof nodeContext>; sel: boolean; unused: boolean; onSelect: () => void };
export function ExecutionSectionResourceCard({ x, ctx, sel, unused, onSelect }: CardProps) {
  const y = Y.executor, col = sel ? 'var(--accent)' : 'var(--muted)';
  return <Hit label={`${ctx.c} 执行资源 · ${ctx.actor}`} onClick={onSelect}><path className="hover-stroke" d={`M${x - 67},${y - 26} h122 l14,10 v53 h-136 Z`} fill={sel ? 'var(--surface)' : 'var(--bg)'} stroke={col} strokeWidth={.9} strokeDasharray={!ctx.allocated ? '3 3' : undefined} /><Mono x={x - 54} y={y - 7} size={7} color={col}>{unused ? 'NOT IN GRAPH' : ctx.inherited ? 'SNAPSHOT STATE' : ctx.allocated ? ctx.mode === 'ray' ? 'RAY ACTOR' : 'LOCAL PROCESS' : 'NOT SCHEDULED'}</Mono><Mono x={x - 54} y={y + 11} size={8} color="var(--ink)">{unused ? '—' : ctx.inherited ? '继承 · 不占 Actor' : ctx.actor}</Mono><Mono x={x - 54} y={y + 28} size={8}>{ctx.allocated ? ctx.host : NODESTATUS[ctx.ns]}</Mono></Hit>;
}
export function ExecutionSectionEnvironmentCard({ x, ctx, sel, unused, onSelect }: CardProps) {
  const y = Y.isolation, col = sel ? 'var(--accent)' : 'var(--muted)';
  return <Hit label={`${ctx.c} Python 环境 · ${ctx.envPath || '待解析 / 未运行'}`} onClick={onSelect}><rect className="hover-stroke" x={x - 67} y={y - 30} width={136} height={53} rx={2} fill="var(--bg)" stroke={col} strokeWidth={.8} /><Mono x={x - 54} y={y - 11} size={7} color={col}>{unused ? '—' : ctx.inherited ? 'ENV NOT RESTORED' : ctx.allocated ? 'PYTHON 3.11 / uv' : 'PLANNED LOCKFILE'}</Mono><Mono x={x - 54} y={y + 7} size={8} color="var(--ink)">{unused ? 'not used' : ctx.allocated ? ctx.envId : ctx.envSpec}</Mono></Hit>;
}
export function ExecutionSectionNodeColumn({ index }: { index: number }) {
  const { state, dispatch, setDrawer } = useStudio(), c = COMPONENTS[index], x = 264 + index * 192, ctx = nodeContext(state, c.id), sel = c.id === state.selectedNode, unused = ctx.cfg === null, col = sel ? 'var(--accent)' : 'var(--muted)', y = Y.definition;
  const onSelect = () => { dispatch({ type: 'node', id: c.id }); setDrawer(true); };
  return <g opacity={unused ? .2 : 1}>
    {!unused && <path d={`M${x},${y + 25} C${x},${y + 83} ${x + 22},${Y.executor - 60} ${x + 22},${Y.executor - 27} V${Y.isolation - 31}`} fill="none" stroke={col} strokeWidth={sel ? 2 : 1} strokeDasharray={!ctx.allocated ? '4 5' : undefined} opacity={sel ? 1 : .55} />}
    <Hit label={`${c.id} ${c.name} · ${NODESTATUS[ctx.ns]}`} onClick={() => dispatch({ type: 'node', id: c.id })}><rect x={x - 57} y={y - 25} width={114} height={99} fill="transparent" /><circle className="hover-stroke" cx={x} cy={y} r={sel ? 22 : 18} fill={sel ? 'var(--accent)' : 'var(--bg)'} stroke={col} strokeWidth={1.2} /><Mono x={x} y={y + 5} size={15} color={sel ? 'var(--bg)' : 'var(--ink)'} textAnchor="middle">{c.id}</Mono><Text x={x} y={y + 44} textAnchor="middle">{c.name}</Text><Mono x={x} y={y + 63} size={9} textAnchor="middle">{LABELS[String(ctx.cfg)]}</Mono></Hit>
    {ctx.ns === 'running' && <circle className="pulse" cx={x} cy={y} r={27} fill="none" stroke="var(--accent)" />}
    <ExecutionSectionResourceCard {...{ x, ctx, sel, unused, onSelect }} /><ExecutionSectionEnvironmentCard {...{ x, ctx, sel, unused, onSelect }} />
  </g>;
}
export function ExecutionSectionStage() {
  const { state } = useStudio(), v = selectedVariant(state), xs = COMPONENTS.map((_, i) => 264 + i * 192), y = Y.definition;
  return <StudioSvg label="节点到资源和虚拟环境的剖面映射"><path d="M187 55H1357V421H187Z" fill="url(#dots)" />
    {Object.values(Y).map((yy, i) => <Fragment key={yy}><line x1={189} y1={yy} x2={1340} y2={yy} stroke="var(--line)" /><Mono x={0} y={yy - 8} size={9} color="var(--accent)">{['01 / DEFINITION', '02 / EXECUTOR', '03 / ISOLATION'][i]}</Mono><Text x={0} y={yy + 13} size={13}>{['逻辑节点', '执行宿主', 'Python 环境'][i]}</Text><Mono x={0} y={yy + 33} size={8}>{['Graph + configuration', 'Actor / process / host', 'Per-node environment'][i]}</Mono></Fragment>)}
    {edges(v).map(([a, b]) => { const xa = xs[COMPONENTS.findIndex(c => c.id === a)], xb = xs[COMPONENTS.findIndex(c => c.id === b)], extra = v.extra.some(e => e[0] === a && e[1] === b); return <Fragment key={a + b}><path d={extra ? `M${xa},${y - 16} C${xa + 35},${y - 75} ${xb - 35},${y - 75} ${xb},${y - 16}` : `M${xa + 22},${y} H${xb - 23}`} stroke={extra ? 'var(--accent)' : 'var(--muted)'} strokeWidth={1.2} fill="none" markerEnd="url(#arrowhead)" />{extra && <Mono x={(xa + xb) / 2} y={y - 47} size={8} color="var(--accent)" textAnchor="middle">{a} → {b} 旁路</Mono>}</Fragment>; })}
    {COMPONENTS.map((c, i) => <ExecutionSectionNodeColumn key={c.id} index={i} />)}<Mono x={0} y={444} size={8}>SCOPE / HOST-AWARE</Mono><Text x={213} y={444} size={10} color="var(--muted)">本地控制端 workspace 与执行端目录分别标识。点击资源层打开完整路径。</Text>
  </StudioSvg>;
}
