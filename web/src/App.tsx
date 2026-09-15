import { useEffect, useState, useSyncExternalStore } from 'react';
import { service } from './service';
import { defaultOptions } from './data';
import { busy, commandReason, scenarioNames, statusNames } from './domain';
import type { Command, OperationResult, Scenario, StartOptions, Variables } from './domain';
import { WorkflowCanvas } from './components/WorkflowCanvas';
import { Inspector } from './components/Inspector';
import { History } from './components/History';
import { AuditPage, BatchPage, ComparePage, ComponentLibrary, ExecutionPicker, RunForm, SystemPage } from './components/Pages';
import { Field, Modal, StatusBadge, Tags, duration } from './components/common';

type View = 'workflow' | 'components' | 'compare' | 'batch' | 'audit' | 'system';
const pages: [View, string, string][] = [['workflow', '工作流', '01'], ['components', '组件库', '02'], ['compare', '变体对照', '03'], ['batch', '批量实验', '04'], ['audit', '事件与审计', '05'], ['system', '系统信息', '06']];

export default function App() {
  const snapshot = useSyncExternalStore(service.subscribe, service.getSnapshot);
  const [view, setView] = useState<View>('workflow');
  const [variantId, setVariantId] = useState('workflow2');
  const [executionId, setExecutionId] = useState(snapshot.executions[0].id);
  const [nodeId, setNodeId] = useState('c');
  const [checkpointId, setCheckpointId] = useState<string>();
  const [auto, setAuto] = useState(true);
  const [search, setSearch] = useState(''); const [statusFilter, setStatusFilter] = useState('all');
  const [toast, setToast] = useState<OperationResult>();
  const [modal, setModal] = useState<'run' | 'fork' | 'edit' | 'reset' | null>(null);
  const [options, setOptions] = useState<StartOptions>(defaultOptions);
  const [stateText, setStateText] = useState('{}');
  const [formError, setFormError] = useState('');
  const variant = snapshot.variants.find(v => v.id === variantId)!;
  const executions = snapshot.executions.filter(e => e.variantId === variantId);
  const execution = executions.find(e => e.id === executionId) ?? executions[0];
  const checkpoint = execution?.checkpoints.find(c => c.id === checkpointId);
  const runningCount = snapshot.executions.filter(busy).length;

  useEffect(() => { if (auto) return service.startClock(); }, [auto]);
  useEffect(() => { if (!toast) return; const timer = setTimeout(() => setToast(undefined), 6500); return () => clearTimeout(timer); }, [toast]);
  const selectExecution = (id: string, cp?: string) => {
    const e = service.getSnapshot().executions.find(e => e.id === id); if (!e) return;
    setVariantId(e.variantId); setExecutionId(e.id); setCheckpointId(cp); setNodeId(e.graph.route[e.cursor] ?? e.graph.nodes[0].id); setView('workflow');
  };
  const selectVariant = (id: string) => {
    setVariantId(id); setCheckpointId(undefined);
    const e = snapshot.executions.find(e => e.variantId === id); setExecutionId(e?.id ?? '');
    setNodeId(snapshot.variants.find(v => v.id === id)!.nodes[0].id);
  };
  const command = (c: Command) => {
    if (!execution) return;
    const result = service.command(execution.id, c); setToast(result);
    if (result.ok) {
      if (result.executionId) selectExecution(result.executionId);
      else if (['rollback', 'resume', 'edit'].includes(c.type)) setCheckpointId(undefined);
    }
    return result;
  };
  const showModal = (kind: typeof modal) => { setFormError(''); setModal(kind); };
  const openRun = () => { setOptions({ ...defaultOptions, variantId, mode: execution?.runtime.mode ?? 'local', model: variant.config.model, temperature: variant.config.temperature }); showModal('run'); };
  const openEdit = () => { setStateText(JSON.stringify(execution?.state ?? {}, null, 2)); showModal('edit'); };
  const openFork = () => { setStateText(JSON.stringify({ query: `${String(checkpoint?.state.query ?? '')}（分支）`, messages: ['forked'] }, null, 2)); showModal('fork'); };
  const submitState = () => {
    let state: Variables;
    try {
      const parsed: unknown = JSON.parse(stateText);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('需要 JSON 对象');
      state = parsed as Variables;
    } catch { setFormError('请输入有效的 JSON 对象，例如 {"query":"新的问题"}'); return; }
    const result = modal === 'fork' && checkpoint ? command({ type: 'fork', checkpointId: checkpoint.id, state }) : command({ type: 'edit', state });
    if (result?.ok) setModal(null); else setFormError(result?.message ?? '操作失败');
  };
  const copyPath = async (value: string) => {
    try { await navigator.clipboard.writeText(value); setToast({ ok: true, message: '模拟路径已复制；未访问本地目录' }); }
    catch { setToast({ ok: false, message: '浏览器不允许复制，请选中路径手动复制' }); }
  };
  const controlButton = (type: 'pause' | 'resume' | 'stop' | 'rollback' | 'fork', label: string, action: () => void) => {
    const reason = execution ? commandReason(execution, type) ?? (['rollback', 'fork'].includes(type) && !checkpoint ? '先在下方选择检查点' : undefined) : '请先启动运行';
    return <button disabled={!!reason} title={reason ?? label} onClick={action}>{label}</button>;
  };
  return <div className="app-shell">
    <header className="topbar"><a href="#" className="brand" onClick={e => { e.preventDefault(); setView('workflow'); }}><span className="brand-mark">W</span><strong>WTB</strong><span className="brand-caption">工作流实验控制台</span><span className="version">MVP</span></a>
      <div className="topbar-actions"><span className="demo-badge">演示模式 · 模拟数据</span><button onClick={() => showModal('reset')}>重置演示</button></div>
    </header>
    <nav className="main-nav" aria-label="主导航">{pages.map(([id, label, index]) => <button key={id} className={view === id ? 'active' : ''} onClick={() => setView(id)}><span>{index}</span>{label}</button>)}<div className="clock-controls"><label><input type="checkbox" checked={auto} onChange={e => setAuto(e.target.checked)} />自动推进</label><button onClick={() => service.tick()} disabled={auto} title={auto ? '关闭自动推进后可手动推进一秒' : '推进所有模拟任务一秒'}>推进 1 秒</button></div></nav>
    <div className="demo-notice">所有执行、指标、Actor、目录与环境均为演示数据。操作仅发生在浏览器内存中，不连接模型、Ray 或 WTB 后端。</div>
    {view === 'workflow' ? <div className="workbench">
      <aside className="sidebar"><div className="project-label">项目</div><div className="project-card"><strong>{snapshot.projects[0].name}</strong><small>6 个组件 · 10 个变体</small></div>
        <div className="section-toolbar"><h2>工作流变体</h2><span className="count">10</span></div>
        <input className="search" aria-label="搜索变体" placeholder="搜索名称、标签…" value={search} onChange={e => setSearch(e.target.value)} />
        <select aria-label="筛选变体状态" value={statusFilter} onChange={e => setStatusFilter(e.target.value)}><option value="all">所有运行状态</option><option value="none">尚未运行</option>{Object.entries(statusNames).map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select>
        <div className="variant-list">{snapshot.variants.filter(v => `${v.name} ${v.description} ${v.tags.join(' ')}`.toLowerCase().includes(search.toLowerCase())).filter(v => statusFilter === 'all' || (snapshot.executions.find(e => e.variantId === v.id)?.status ?? 'none') === statusFilter).map(v => {
          const latest = snapshot.executions.find(e => e.variantId === v.id);
          return <button className={`variant-item ${v.id === variantId ? 'active' : ''}`} key={v.id} onClick={() => selectVariant(v.id)}><div><strong>{v.name}</strong><span>{v.nodes.length} 节点</span></div><span className="variant-description">{v.description}</span><div>{latest ? <StatusBadge status={latest.status} /> : <span className="muted small">○ 尚未运行</span>}<span className="small muted">{latest ? duration(latest.elapsedMs) : '—'}</span></div></button>;
        })}</div><div className="sidebar-footer"><span className="live-dot" /> {runningCount} / 2 推进槽位使用中<br /><span className="small muted">暂停的演示任务释放推进槽位</span></div>
      </aside>
      <main className="workspace-main"><div className="page-heading"><div><div className="breadcrumb">Workflow A / {variant.name}</div><h1>{variant.name} <span>{variant.description}</span></h1><Tags items={variant.tags} /></div><button className="primary" onClick={openRun}>＋ 启动运行</button></div>
        <div className="run-toolbar"><div className="button-row"><ExecutionPicker executions={executions} selectedId={execution?.id} onSelect={id => selectExecution(id)} />{execution && <StatusBadge status={execution.status} />}</div><span className="muted small">{execution ? `${execution.runtime.mode === 'ray' ? 'Ray Actor' : '本地进程'} · ${scenarioNames[execution.scenario]}` : '预注册工作流定义'}</span></div>
        <div className="control-toolbar"><div className="button-row">
          {controlButton('pause', 'Ⅱ 暂停', () => command({ type: 'pause' }))}{controlButton('resume', '▶ 继续', () => command({ type: 'resume' }))}{controlButton('stop', '■ 停止', () => command({ type: 'stop' }))}<span className="divider" />
          {controlButton('rollback', '↶ 回退到此处', () => checkpoint && command({ type: 'rollback', checkpointId: checkpoint.id }))}{controlButton('fork', '⑂ Fork', openFork)}
        </div><span className="muted small">{execution ? `${checkpoint?.cursor ?? execution.cursor} / ${execution.graph.route.length} 步 · ${duration(execution.elapsedMs)}` : '等待运行'}</span></div>
        {execution?.operationError && <div className="info-note error-note">{execution.operationError}</div>}
        {execution?.error && <div className="info-note error-note">{execution.error}</div>}
        {checkpoint && <div className="historical-banner"><span>历史查看 · {checkpoint.id} · 图和文件按此快照展示；右侧资源为当前绑定</span><button onClick={() => setCheckpointId(undefined)}>返回实时</button></div>}
        <div className="canvas-layout"><section className="panel graph-panel"><div className="panel-heading"><h2>组件执行图</h2><span className="muted small">点击节点查看详情 / 设置断点</span></div><WorkflowCanvas variant={variant} execution={execution} checkpoint={checkpoint} components={snapshot.components} selectedNode={nodeId} onSelect={setNodeId} /><div className="graph-legend"><span>○ 待执行</span><span className="blue">▶ 运行中</span><span className="yes">✓ 已完成</span><span className="error-text">! 失败</span><span>● 节点前断点</span><span>虚线：条件路由</span></div></section>
          <Inspector execution={execution} variant={variant} components={snapshot.components} nodeId={nodeId} onBreakpoint={id => command({ type: 'breakpoint', nodeId: id })} onCopy={copyPath} />
        </div>
        <History execution={execution} executions={snapshot.executions} events={snapshot.events} checkpoint={checkpoint} onCheckpoint={setCheckpointId} onExecution={selectExecution} onEdit={openEdit} onManual={() => command({ type: 'checkpoint' })} />
      </main>
    </div> : <main className="secondary-page">
      {view === 'components' && <ComponentLibrary snapshot={snapshot} />}
      {view === 'compare' && <ComparePage snapshot={snapshot} />}
      {view === 'batch' && <BatchPage snapshot={snapshot} service={service} onResult={setToast} onExecution={selectExecution} />}
      {view === 'audit' && <AuditPage snapshot={snapshot} onExecution={selectExecution} />}
      {view === 'system' && <SystemPage snapshot={snapshot} service={service} />}
    </main>}
    {toast && <div className={`toast ${toast.ok ? '' : 'error-toast'}`} role="status"><span>{toast.ok ? '✓' : '!'} {toast.message}</span><button aria-label="关闭提示" onClick={() => setToast(undefined)}>×</button></div>}
    {modal === 'run' && <Modal title={`启动 ${variant.name} · 模拟执行`} onClose={() => setModal(null)}><RunForm options={options} onChange={setOptions} /><Field label="演示场景"><select value={options.scenario} onChange={e => setOptions({ ...options, scenario: e.target.value as Scenario })}>{Object.entries(scenarioNames).map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></Field>{formError && <p className="error-text" role="alert">{formError}</p>}<div className="modal-actions"><button onClick={() => setModal(null)}>取消</button><button className="primary" onClick={() => { const result = service.start({ ...options, variantId }); setToast(result); if (result.ok) { selectExecution(result.executionId!); setModal(null); } else setFormError(result.message); }}>创建并运行</button></div></Modal>}
    {(modal === 'fork' || modal === 'edit') && <Modal title={modal === 'fork' ? `从 ${checkpoint?.id} Fork` : '修改当前状态'} onClose={() => setModal(null)}><p className="muted">{modal === 'fork' ? '新分支继承检查点状态和文件，以下字段覆盖原状态。创建后保持暂停。' : '以下字段合并到当前状态，并记录修改前后的检查点。'}</p><Field label="状态覆盖（JSON 对象）"><textarea className="code-input" rows={10} value={stateText} onChange={e => setStateText(e.target.value)} /></Field>{formError && <p className="error-text" role="alert">{formError}</p>}<div className="modal-actions"><button onClick={() => setModal(null)}>取消</button><button className="primary" onClick={submitState}>{modal === 'fork' ? '创建分支' : '保存状态'}</button></div></Modal>}
    {modal === 'reset' && <Modal title="重置演示" onClose={() => setModal(null)}><p>清除当前浏览器中的模拟运行与操作，恢复初始场景。不会操作磁盘。</p><div className="modal-actions"><button onClick={() => setModal(null)}>取消</button><button className="primary" onClick={() => { service.reset(); setView('workflow'); setVariantId('workflow2'); setExecutionId(service.getSnapshot().executions[0].id); setCheckpointId(undefined); setNodeId('c'); setSearch(''); setStatusFilter('all'); setModal(null); setToast({ ok: true, message: '已恢复初始演示场景' }); }}>确认重置</button></div></Modal>}
  </div>;
}
