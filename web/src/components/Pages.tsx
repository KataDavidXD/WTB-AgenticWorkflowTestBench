import { useState } from 'react';
import type { ConsoleService, DemoSnapshot, Execution, Mode, OperationResult, StartOptions } from '../domain';
import { terminal } from '../domain';
import { defaultOptions } from '../data';
import { Empty, Field, JsonView, StatusBadge, Tags, duration } from './common';
import { WorkflowCanvas } from './WorkflowCanvas';
import { EventTable } from './History';

export function ComponentLibrary({ snapshot }: { snapshot: DemoSnapshot }) {
  return <><div className="page-heading"><div><h1>组件库</h1><p>预注册的 a–f 组件，组成不同工作流变体。</p></div><span className="tag">只读定义 · 模拟</span></div><div className="component-grid">{snapshot.components.map(c => <article className="panel component-tile" key={c.id}>
    <div className="component-title"><span className="component-letter large">{c.id.toUpperCase()}</span><div><h2>{c.name}</h2><span className="muted">component:{c.id} · v{c.version}</span></div></div><p>{c.description}</p><Tags items={c.tags} /><h4>被以下变体使用</h4><div className="tags">{snapshot.variants.filter(v => v.nodes.some(n => n.componentId === c.id)).map(v => <span className="tag" key={v.id}>{v.name}</span>)}</div>
  </article>)}</div></>;
}

export function ComparePage({ snapshot }: { snapshot: DemoSnapshot }) {
  const [left, setLeft] = useState('workflow1'); const [right, setRight] = useState('workflow2');
  const a = snapshot.variants.find(v => v.id === left)!; const b = snapshot.variants.find(v => v.id === right)!;
  return <><div className="page-heading"><div><h1>变体对照</h1><p>比较组件组成、连接与配置。此处展示定义，不混入运行状态。</p></div><span className="tag">10 个变体 / 6 个组件</span></div>
    <section className="panel table-wrap"><table><thead><tr><th>变体</th>{snapshot.components.map(c => <th key={c.id}>{c.id.toUpperCase()} · {c.name}</th>)}</tr></thead><tbody>{snapshot.variants.map(v => <tr key={v.id}><th>{v.name}<div className="muted small">{v.description}</div></th>{snapshot.components.map(c => { const n = v.nodes.find(n => n.componentId === c.id); return <td key={c.id}>{n ? <><span className="yes">✓</span> <code>{n.implementation}</code></> : <span className="muted">—</span>}</td>; })}</tr>)}</tbody></table></section>
    <div className="comparison-grid">{[[a, setLeft], [b, setRight]].map(([value, set], i) => { const v = value as typeof a; const setter = set as (id: string) => void; return <section className="panel" key={i}><div className="panel-heading"><select aria-label={i === 0 ? '左侧变体' : '右侧变体'} value={v.id} onChange={e => setter(e.target.value)}>{snapshot.variants.map(x => <option key={x.id} value={x.id}>{x.name} · {x.description}</option>)}</select></div><WorkflowCanvas variant={v} components={snapshot.components} compact /><div className="comparison-config"><Tags items={v.tags} /><JsonView value={v.config} /></div></section>; })}</div>
    <div className="info-note">组成差异：{snapshot.components.filter(c => a.nodes.some(n => n.componentId === c.id) !== b.nodes.some(n => n.componentId === c.id)).map(c => c.id.toUpperCase()).join('、') || '组件集合相同'}；连接：{a.edges.length} / {b.edges.length} 条；检索：{a.config.retrieval} / {b.config.retrieval}；模型：{a.config.model} / {b.config.model}。</div>
  </>;
}

export function BatchPage({ snapshot, service, onResult, onExecution }: { snapshot: DemoSnapshot; service: ConsoleService; onResult: (r: OperationResult) => void; onExecution: (id: string, checkpoint?: string) => void }) {
  const [chosen, setChosen] = useState(['workflow1', 'workflow2', 'workflow3']);
  const [inputs, setInputs] = useState('总结季度业务表现\n检索产品相关资料'); const [mode, setMode] = useState<Mode>('ray');
  const [batchId, setBatchId] = useState('');
  const batch = snapshot.batches.find(b => b.id === batchId) ?? snapshot.batches[0];
  const runs = batch ? batch.executionIds.map(id => snapshot.executions.find(e => e.id === id)!).filter(Boolean) : [];
  const finished = runs.filter(terminal).length;
  const succeeded = runs.filter(e => e.status === 'completed');
  const scored = succeeded.filter(e => e.score !== undefined);
  return <><div className="page-heading"><div><h1>批量实验</h1><p>变体 × 测试输入。模拟调度在全局最多使用 2 个执行槽位。</p></div><span className="tag">所有指标均为演示值</span></div>
    <div className="batch-layout"><section className="panel batch-form"><h2>新建对照实验</h2><Field label="执行模式"><select value={mode} onChange={e => setMode(e.target.value as Mode)}><option value="ray">Ray</option><option value="local">本地</option></select></Field>
      <h4>选择变体</h4><div className="checkbox-grid">{snapshot.variants.map(v => <label key={v.id}><input type="checkbox" checked={chosen.includes(v.id)} onChange={e => setChosen(e.target.checked ? [...chosen, v.id] : chosen.filter(id => id !== v.id))} />{v.name}</label>)}</div>
      <Field label="测试输入（每行一条）"><textarea rows={4} value={inputs} onChange={e => setInputs(e.target.value)} /></Field>
      <button className="primary wide" onClick={() => { const r = service.startBatch(chosen, inputs.split('\n'), { ...defaultOptions, mode, nodeTicks: 2, environment: mode === 'ray' ? 'reused' : 'current' }); if (r.ok) setBatchId(service.getSnapshot().batches[0].id); onResult(r); }}>运行 {chosen.length * inputs.split('\n').filter(q => q.trim()).length} 个任务</button>
    </section><div><section className="panel"><div className="panel-heading"><h2>实验结果</h2><select aria-label="选择批次" value={batch?.id ?? ''} onChange={e => setBatchId(e.target.value)}>{snapshot.batches.map(b => <option key={b.id} value={b.id}>{b.name} · {b.id}</option>)}</select></div>
      <div className="metric-grid"><div><span>已结束</span><strong>{finished}<small> / {runs.length}</small></strong></div><div><span>成功率 · 已结束任务</span><strong>{finished ? Math.round(succeeded.length / finished * 100) : 0}%</strong></div><div><span>平均质量分 · 模拟</span><strong>{scored.length ? (scored.reduce((n, e) => n + e.score!, 0) / scored.length).toFixed(2) : '—'}</strong></div></div>
      <div className="progress"><span style={{ width: `${runs.length ? finished / runs.length * 100 : 0}%` }} /></div>
      <div className="table-wrap"><table><thead><tr><th>变体 / 输入</th><th>状态</th><th>耗时</th><th>质量分</th><th>操作</th></tr></thead><tbody>{runs.map(e => <tr key={e.id}><td><button className="link" onClick={() => onExecution(e.id)}>{e.variantId}</button><div className="small input-summary" title={String(e.state.query)}>{String(e.state.query)}</div><code className="small">{e.id}</code></td><td><StatusBadge status={e.status} /></td><td>{duration(e.elapsedMs)}</td><td>{e.score?.toFixed(2) ?? '—'}</td><td><button onClick={() => onExecution(e.id, e.checkpoints[0]?.id)}>检查点 / 回退 / Fork</button></td></tr>)}</tbody></table></div>
    </section></div></div>
  </>;
}

export function AuditPage({ snapshot, onExecution }: { snapshot: DemoSnapshot; onExecution: (id: string) => void }) {
  const [id, setId] = useState('all'); const [kind, setKind] = useState('all');
  const events = snapshot.events.filter(ev => (id === 'all' || ev.executionId === id) && (kind === 'all' || ev.kind === kind));
  return <><div className="page-heading"><div><h1>事件与审计</h1><p>模拟服务统一记录操作、节点、检查点与资源变化；最多保留 1,000 条。</p></div></div><section className="panel"><div className="section-toolbar filters"><Field label="执行筛选"><select value={id} onChange={e => setId(e.target.value)}><option value="all">所有执行</option>{snapshot.executions.map(e => <option key={e.id} value={e.id}>{e.id} · {e.variantId}</option>)}</select></Field><Field label="事件类型"><select value={kind} onChange={e => setKind(e.target.value)}><option value="all">所有类型</option>{['execution', 'node', 'checkpoint', 'control', 'runtime', 'system'].map(k => <option key={k}>{k}</option>)}</select></Field><span className="muted">{events.length} 条事件</span></div><EventTable events={events} onExecution={onExecution} /></section></>;
}

export function SystemPage({ snapshot, service }: { snapshot: DemoSnapshot; service: ConsoleService }) {
  const s = snapshot.system;
  return <><div className="page-heading"><div><h1>系统信息</h1><p>存储、缓存、Outbox 和完整性检查的功能展示。</p></div><span className="tag">未连接真实服务</span></div>
    <div className="system-grid"><section className="panel padded"><h2>存储与运行配置</h2><dl><dt>元数据</dt><dd>SQLite（模拟）</dd><dt>检查点后端</dt><dd>LangGraph SQLite（模拟）</dd><dt>文件存储</dt><dd>本地 CAS（内存模拟）</dd><dt>执行槽位</dt><dd>2 · 暂停不占推进槽位</dd><dt>数据生命周期</dt><dd>刷新或重置即恢复初始演示</dd></dl><div className="info-note">页面不连接数据库，不执行工作流，也不创建或删除磁盘文件。</div></section>
    <section className="panel padded"><h2>环境与模型缓存</h2><div className="metric-grid two"><div><span>命中 · 演示</span><strong>{s.cacheHits}</strong></div><div><span>未命中 · 演示</span><strong>{s.cacheMisses}</strong></div></div><p className="muted">复用环境的模拟运行增加命中；新环境增加未命中。</p><button onClick={() => service.systemAction('cache')}>重置模拟缓存统计</button></section>
    <section className="panel padded"><h2>Outbox 事件处理</h2><div className="metric-grid two"><div><span>待处理</span><strong>{s.outboxPending}</strong></div><div><span>已处理</span><strong>{s.outboxProcessed}</strong></div></div><p className="muted">演示事件写入后的后台处理阶段。</p><button onClick={() => service.systemAction('outbox')} disabled={!s.outboxPending}>处理待办事件（模拟）</button></section>
    <section className="panel padded"><h2>完整性检查</h2><p className={s.integrity === 'issues' ? 'error-text' : 'muted'}>{s.integrity === 'unchecked' ? '尚未检查' : s.integrity === 'healthy' ? '✓ 演示记录未发现关联问题' : `! 发现 ${s.findings.length} 个演示问题`}</p><button onClick={() => service.systemAction('check')}>运行模拟检查</button>{s.checkedAt && <p className="small muted">检查时间：{new Date(s.checkedAt).toLocaleString('zh-CN')}</p>}{s.findings.map(f => <div className="error-note info-note" key={f}>{f}</div>)}</section></div>
    <section className="panel padded"><h2>能力边界</h2><p>当前是模拟数据交互 MVP，覆盖 WTB 的项目、变体、执行、检查点、文件版本、分支、批量、评估、审计及运行环境入口。</p><p className="muted">真实 HTTP/WebSocket、Python 工作流执行、Ray 集群、UV 服务、文件上传、拖拽编排与账号权限尚未接入。相关信息不代表当前机器的真实配置。</p></section>
  </>;
}

export function RunForm({ options, onChange }: { options: StartOptions; onChange: (options: StartOptions) => void }) {
  const patch = (v: Partial<StartOptions>) => onChange({ ...options, ...v });
  return <div className="run-form">
    <Field label="测试输入"><textarea value={options.input} onChange={e => patch({ input: e.target.value })} rows={3} /></Field>
    <div className="form-grid"><Field label="执行模式"><select value={options.mode} onChange={e => patch({ mode: e.target.value as Mode })}><option value="local">本地进程</option><option value="ray">Ray Actor</option></select></Field>
      <Field label="环境方式"><select value={options.environment} onChange={e => patch({ environment: e.target.value as StartOptions['environment'] })}><option value="current">当前 Python 环境</option><option value="venv">独立虚拟环境</option><option value="reused">复用虚拟环境</option></select></Field>
      <Field label="模型变体"><select value={options.model} onChange={e => patch({ model: e.target.value })}><option value="model-small">model-small</option><option value="model-large">model-large</option></select></Field>
      <Field label="检索节点变体"><select value={options.nodeImplementation} onChange={e => patch({ nodeImplementation: e.target.value })}><option value="inherit">沿用变体定义</option><option value="dense">dense</option><option value="bm25">bm25</option><option value="hybrid">hybrid</option></select></Field>
      <Field label="温度（0–2）"><input type="number" min="0" max="2" step="0.1" value={options.temperature} onChange={e => patch({ temperature: Number(e.target.value) })} /></Field>
      <Field label="每节点模拟秒数"><input type="number" min="1" max="15" value={options.nodeTicks} onChange={e => patch({ nodeTicks: Number(e.target.value) })} /></Field>
    </div>
  </div>;
}

export function ExecutionPicker({ executions, selectedId, onSelect }: { executions: Execution[]; selectedId?: string; onSelect: (id: string) => void }) {
  if (!executions.length) return <Empty>暂无运行记录</Empty>;
  return <select aria-label="运行记录" value={selectedId ?? executions[0].id} onChange={e => onSelect(e.target.value)}>{executions.map(e => <option key={e.id} value={e.id}>{e.id} · {e.parentId ? 'Fork · ' : ''}{e.runtime.mode === 'ray' ? 'Ray' : '本地'} · {e.status}</option>)}</select>;
}
