import { useState } from 'react';
import type { Batch, Catalog, Execution, Page, Variant } from '../domain';
import { api } from '../service';
import { useRemote } from '../useRemote';
import { JsonView, StatusBadge } from './common';
import { WorkflowCanvas } from './WorkflowCanvas';

export function ComponentLibrary({ catalog }: { catalog: Catalog }) {
  return <section className="panel panel-body"><h2>已注册节点实现</h2><table><thead><tr><th>组件</th><th>名称</th><th>版本</th><th>标签</th></tr></thead><tbody>{catalog.components.map(c => <tr key={c.id}><td>{c.id}</td><td>{c.name}</td><td>{c.version ?? '未提供'}</td><td>{c.tags.join(', ') || '未提供'}</td></tr>)}</tbody></table></section>;
}
export function ComparePage({ variants }: { variants: Variant[] }) {
  const [left, setLeft] = useState(variants[0]?.id);
  const [right, setRight] = useState(variants[1]?.id ?? variants[0]?.id);
  return <section className="panel panel-body"><h2>变体对照</h2><div className="compare-grid">{[[left,setLeft],[right,setRight]].map(([id,set], i) => { const v=variants.find(v => v.id === id); return <div key={i}><select aria-label={`对照变体 ${i+1}`} value={id as string} onChange={e => (set as (v:string)=>void)(e.target.value)}>{variants.map(v => <option key={v.id} value={v.id}>{v.id}</option>)}</select>{v && <><WorkflowCanvas model={v} compact /><JsonView value={v.nodeVariants} /></>}</div>; })}</div><table><thead><tr><th>变体</th><th>节点实现</th></tr></thead><tbody>{variants.map(v => <tr key={v.id}><td>{v.id}</td><td>{v.nodes.map(n => `${n.id}: ${n.implementation}`).join(' / ')}</td></tr>)}</tbody></table></section>;
}
export function BatchPage({ catalog, revision, onExecution }: { catalog: Catalog; revision: number; onExecution: (id: string) => void }) {
  const [chosen, setChosen] = useState<string[]>([]);
  const [inputs, setInputs] = useState('[{"text":"A","repeat":1},{"text":"B","repeat":2}]');
  const [busy,setBusy] = useState(false); const [message,setMessage] = useState(''); const [selected,setSelected] = useState('');
  const batches = useRemote<Batch[]>('/batch-tests',revision);
  const batch = batches.data?.find(b => b.id === selected) ?? batches.data?.[0];
  const results = useRemote<Page<Execution>>(batch ? `/batch-tests/${batch.id}/results` : undefined, revision);
  const submit = async () => { setBusy(true); try { const parsed: unknown = JSON.parse(inputs); if (!Array.isArray(parsed) || parsed.some(s=>!s || typeof s !== 'object' || Array.isArray(s))) throw new Error('输入必须是 JSON 对象数组'); const b=await api<Batch>('/batch-tests',{variants:chosen,inputs:parsed}); setSelected(b.id); setMessage('已提交真实批量任务'); } catch(e) {setMessage(String(e));} finally{setBusy(false);} };
  const completed=results.data?.items.filter(e=>e.status==='completed') ?? [];
  const terminal=results.data?.items.filter(e=>['completed','failed','cancelled'].includes(e.status)) ?? [];
  return <section className="panel panel-body"><h2>本地批量实验</h2><p>最多两个任务同时运行。质量分数：未评估。</p>{catalog.variants.map(v => <label key={v.id} style={{marginRight:16}}><input type="checkbox" checked={chosen.includes(v.id)} onChange={e=>setChosen(e.target.checked?[...chosen,v.id]:chosen.filter(id=>id!==v.id))}/>{v.id}</label>)}<textarea aria-label="批量输入" value={inputs} onChange={e=>setInputs(e.target.value)} rows={4}/><button disabled={busy || !chosen.length} onClick={()=>void submit()}>提交批量实验</button><p role="status">{message}</p><select aria-label="批量历史" value={batch?.id ?? ''} onChange={e=>setSelected(e.target.value)}>{batches.data?.map(b=><option key={b.id} value={b.id}>{b.id}</option>)}</select><p>完成 {terminal.length} / {batch?.executionIds.length ?? 0} · 已结束任务成功率 {terminal.length ? (completed.length/terminal.length*100).toFixed(0)+'%' : '—'}</p>{results.error && <p>{results.error}</p>}<table><thead><tr><th>执行</th><th>变体</th><th>状态</th><th>耗时</th><th>操作</th></tr></thead><tbody>{results.data?.items.map(e=><tr key={e.id}><td>{e.id.slice(0,8)}</td><td>{e.variantId}</td><td><StatusBadge status={e.status}/></td><td>{e.elapsedMs} ms</td><td><button onClick={()=>onExecution(e.id)}>查看 / 回退 / Fork</button></td></tr>)}</tbody></table></section>;
}
export function SystemPage({revision}:{revision:number}) {
  const result=useRemote<Record<string,unknown>>('/system',revision);
  const [message,setMessage]=useState(''); const [busy,setBusy]=useState(false);
  return <section className="panel panel-body"><h2>系统信息</h2><p>本机服务 · SQLite · 真实 CAS。未采集的数据不估算。</p>{result.error && <p>{result.error}</p>}<JsonView value={result.data}/><button disabled={busy} onClick={()=>{setBusy(true); void api('/system/integrity',{}).then(()=>setMessage('检查已完成')).catch(e=>setMessage(String(e))).finally(()=>setBusy(false));}}>检查文件完整性</button><p>{message}</p></section>;
}
