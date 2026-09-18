import { Fragment } from 'react';
import { COMPONENTS, CONFIGS, LABELS, SHORT, STATUS, byVariant, configurationDifference as diff, edgeDifference as edgeDiff, statusColor, variants, type StudioVariant } from './StudioDemoData';
import { useStudio } from './StudioProvider';
import { latestRun, selectedVariant, type DemoState } from './StudioState';
import { Hit, Mono, StudioSvg, Text } from './StudioSvg';
export function arc(cx: number, cy: number, r: number, a: number, b: number) { const A = a * Math.PI / 180, B = b * Math.PI / 180; return `M${cx + r * Math.cos(A)},${cy + r * Math.sin(A)} A${r},${r} 0 ${b - a > 180 ? 1 : 0} 1 ${cx + r * Math.cos(B)},${cy + r * Math.sin(B)}`; }
function compactLabel(value: string, max = 14) { return value.length > max ? `${value.slice(0, max - 1)}…` : value; }
export function atlasGeometry(state: DemoState) {
  const xd = state.xDim, yd = state.yDim, xc = CONFIGS[COMPONENTS[xd].id], yc = CONFIGS[COMPONENTS[yd].id], x0 = 130, y0 = 56, cw = 930 / xc.length, maxCols = Math.max(1, Math.floor(cw / 100));
  const rowHeights = yc.map(y => Math.max(104, ...xc.map(x => Math.ceil(variants.filter(v => v.cfg[xd] === x && v.cfg[yd] === y).length / maxCols) * 94 + 10)));
  const rowStarts = rowHeights.map((_, j) => y0 + rowHeights.slice(0, j).reduce((a, b) => a + b, 0)), bottom = y0 + rowHeights.reduce((a, b) => a + b, 0);
  return { xd, yd, xc, yc, x0, cw, maxCols, rowHeights, rowStarts, bottom, height: Math.max(453, bottom + 87) };
}
export function VariantAtlasGlyph({ v, cx, cy, rr }: { v: StudioVariant; cx: number; cy: number; rr: number }) {
  const { state, dispatch } = useStudio(), base = byVariant(state.baseline), sel = v.id === selectedVariant(state).id, rs = latestRun(state, v).status;
  return <Hit label={`${v.name} · ${v.label} · ${STATUS[rs]} · 配置差异 ${diff(v, base)}`} onClick={() => dispatch({ type: 'variant', id: v.id })}><circle cx={cx} cy={cy} r={rr + 13} fill={sel ? 'var(--bg)' : 'transparent'} stroke={sel ? 'var(--accent)' : 'none'} strokeWidth={1} /><circle className="hover-stroke" cx={cx} cy={cy} r={rr + 7} fill="none" stroke={statusColor(rs)} strokeWidth={sel ? 1.8 : .8} strokeDasharray={rs === 'idle' ? '2 3' : undefined} />
    {COMPONENTS.map((c, i) => <path key={c.id} d={arc(cx, cy, rr, i * 60 - 87, i * 60 - 38)} stroke={v.cfg[i] === null ? 'var(--line)' : v.cfg[i] !== base.cfg[i] ? 'var(--accent)' : 'var(--ink)'} strokeWidth={v.cfg[i] === null ? 1.6 : 3.6} fill="none" strokeLinecap="round" strokeDasharray={v.cfg[i] === null ? '1 4' : undefined} />)}
    <Mono x={cx} y={cy + 3} size={10} color={sel ? 'var(--accent)' : 'var(--ink)'} textAnchor="middle"><title>{v.id}</title>{compactLabel(v.id.slice(0, 8), 7)}</Mono><Mono x={cx} y={cy + rr + 26} size={8} color={sel ? 'var(--accent)' : 'var(--muted)'} textAnchor="middle"><title>{v.name}</title>{compactLabel(v.name)}</Mono>{v.extra.length > 0 && <Text x={cx + rr + 11} y={cy - rr - 3} color="var(--accent)">↗</Text>}
  </Hit>;
}
export function VariantAtlasConfigurationCell({ i, j, geometry: g }: { i: number; j: number; geometry: ReturnType<typeof atlasGeometry> }) {
  const { x0, cw, xd, yd, xc, yc, rowStarts, rowHeights, maxCols } = g, x = x0 + i * cw, y = rowStarts[j], ch = rowHeights[j], members = variants.filter(v => v.cfg[xd] === xc[i] && v.cfg[yd] === yc[j]);
  const cols = Math.min(members.length, maxCols), rows = Math.ceil(members.length / cols), cellH = ch / rows;
  return <><rect x={x + 1} y={y + 1} width={cw - 2} height={ch - 2} fill={members.length ? 'var(--surface)' : 'none'} opacity={members.length ? .62 : 1} stroke="var(--line)" strokeWidth={.6} />{!members.length ? <path d={`M${x + cw / 2 - 4},${y + ch / 2}h8M${x + cw / 2},${y + ch / 2 - 4}v8`} stroke="var(--line)" /> : members.map((v, k) => { const col = k % cols, row = Math.floor(k / cols), cx = x + cw / 2 + (col - (Math.min(cols, members.length - row * cols) - 1) / 2) * Math.min(100, cw / cols), cy = y + (row + .5) * cellH - 3, rr = Math.max(12, Math.min(24, cellH / 2 - 15)); return <VariantAtlasGlyph key={v.id} {...{ v, cx, cy, rr }} />; })}</>;
}
export function VariantAtlasLegend({ bottom }: { bottom: number }) {
  const { state } = useStudio(), v = selectedVariant(state), base = byVariant(state.baseline), gx = 1228, gy = 105, gr = 32;
  return <><line x1={1100} y1={29} x2={1100} y2={bottom} stroke="var(--line)" /><Mono x={1130} y={47} size={9}>READ THE GLYPH</Mono>
    {COMPONENTS.map((c, i) => { const a = i * 60 - 87, b = i * 60 - 38, mid = (a + b) / 2 * Math.PI / 180; return <Fragment key={c.id}><path d={arc(gx, gy, gr, a, b)} fill="none" stroke={v.cfg[i] === null ? 'var(--line)' : v.cfg[i] !== base.cfg[i] ? 'var(--accent)' : 'var(--ink)'} strokeWidth={5} strokeLinecap="round" strokeDasharray={v.cfg[i] === null ? '2 5' : undefined} /><Mono x={gx + (gr + 17) * Math.cos(mid)} y={gy + (gr + 17) * Math.sin(mid) + 3} size={9} textAnchor="middle"><title>{c.id}</title>{compactLabel(c.id, 7)}</Mono></Fragment>; })}
    <Mono x={gx} y={gy + 5} size={15} color="var(--accent)" textAnchor="middle"><title>{v.id}</title>{compactLabel(v.id.toUpperCase(), 7)}</Mono>
    {[['内环：六个组件槽位', 'var(--ink)'], ['断线：该组件未使用', 'var(--muted)'], ['强调色：与基线配置不同', 'var(--accent)'], ['外环：当前运行状态', 'var(--muted)'], ['↗：包含额外旁路边', 'var(--muted)']].map(([label, color], i) => <Text key={label} x={1130} y={170 + i * 18} size={10} color={color}>{label}</Text>)}
    <Mono x={1130} y={268} size={9}><title>{base.name}</title>BASELINE / {compactLabel(base.name, 14)}</Mono>
  </>;
}
export function VariantAtlasDifferenceStrip({ bottom }: { bottom: number }) {
  const { state } = useStudio(), v = selectedVariant(state), base = byVariant(state.baseline);
  return <><line x1={0} y1={bottom + 21} x2={1360} y2={bottom + 21} stroke="var(--line)" /><Mono x={0} y={bottom + 46} size={8}>PAIRWISE DIFFERENCE</Mono><Text x={0} y={bottom + 72}>{base.name} → {v.name}</Text>
    {COMPONENTS.map((c, i) => <Fragment key={c.id}><Mono x={257 + i * 155} y={bottom + 46} size={8}>{c.id} / {c.name}</Mono><Text x={257 + i * 155} y={bottom + 71} size={10} color={base.cfg[i] !== v.cfg[i] ? 'var(--accent)' : 'var(--muted)'}>{SHORT[String(base.cfg[i])]} → {SHORT[String(v.cfg[i])]}</Text></Fragment>)}<Mono x={1235} y={bottom + 46} size={8}>Δcfg / Δedge</Mono><Text x={1250} y={bottom + 71} size={15} color="var(--accent)">{diff(base, v)} / {edgeDiff(base, v)}</Text>
  </>;
}
export function VariantAtlasStage() {
  const { state } = useStudio(), g = atlasGeometry(state);
  return <StudioSvg height={g.height} label="按配置维度排列的变体图谱"><Mono x={0} y={21} size={9}>{COMPONENTS[g.yd].id} / {COMPONENTS[g.yd].en}</Mono><Mono x={920} y={21} size={9}>{COMPONENTS[g.xd].id} / {COMPONENTS[g.xd].en} →</Mono>
    {g.xc.map((c, i) => <Mono key={String(c)} x={g.x0 + (i + .5) * g.cw} y={41} size={10} color="var(--ink)" textAnchor="middle">{LABELS[String(c)]}</Mono>)}{g.yc.map((c, j) => <Mono key={String(c)} x={10} y={g.rowStarts[j] + g.rowHeights[j] / 2} size={10}>{LABELS[String(c)]}</Mono>)}
    {g.yc.flatMap((_, j) => g.xc.map((_, i) => <VariantAtlasConfigurationCell key={`${j}-${i}`} i={i} j={j} geometry={g} />))}<VariantAtlasLegend bottom={g.bottom} /><VariantAtlasDifferenceStrip bottom={g.bottom} />
  </StudioSvg>;
}
