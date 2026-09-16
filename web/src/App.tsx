import { useEffect, useState, useSyncExternalStore } from 'react';
import type { Checkpoint, Execution, Operation, OperationResult, Variables } from './domain';
import { service } from './service';
import { useRemote } from './useRemote';
import { Empty, Field, Modal, StatusBadge } from './components/common';
import { WorkflowCanvas } from './components/WorkflowCanvas';
import { Inspector } from './components/Inspector';
import { EventTable, History } from './components/History';
import { BatchPage, ComparePage, ComponentLibrary, SystemPage } from './components/Pages';

function object(text:string):Variables { const value:unknown=JSON.parse(text); if(!value || typeof value!=='object' || Array.isArray(value)) throw new Error('状态必须为 JSON 对象'); return value as Variables; }
export default function App() {
  const snapshot=useSyncExternalStore(service.subscribe,service.getSnapshot);
  useEffect(()=>service.connect(),[]);
  const [view,setView]=useState('workflow'); const [variantId,setVariantId]=useState(''); const [executionId,setExecutionId]=useState(''); const [nodeId,setNodeId]=useState<string>(); const [cpId,setCpId]=useState<string>();
  const [modal,setModal]=useState<string>(); const [json,setJson]=useState('{}'); const [breakpoints,setBreakpoints]=useState<string[]>([]); const [overrides,setOverrides]=useState<Record<string,string>>({});
  const [busy,setBusy]=useState(false); const [message,setMessage]=useState(''); const [operation,setOperation]=useState<OperationResult>();
  const detail=useRemote<Execution>(executionId?`/executions/${executionId}`:undefined,snapshot.revision);
  const execution=detail.data;
  const cp=useRemote<Checkpoint>(executionId&&cpId?`/executions/${executionId}/checkpoints/${cpId}`:undefined);
  const op=useRemote<Operation>(operation?`/operations/${operation.operationId}`:undefined,snapshot.revision);
  const catalog=snapshot.catalog;
  const variant=catalog?.variants.find(v=>v.id===variantId) ?? catalog?.variants[0];
  const graph=execution?.graph ?? variant;
  const project=catalog?.projects.find(p=>p.id===variant?.projectId);
  const selectExecution=(id:string)=>{setExecutionId(id);setCpId(undefined);setView('workflow');setNodeId(undefined);};
  useEffect(()=>{if(op.data && ['completed','failed'].includes(op.data.status)){setMessage(op.data.error??'操作已完成'); if(op.data.resultExecutionId)selectExecution(op.data.resultExecutionId);setOperation(undefined);void service.refresh();}},[op.data]);
  const accepted=(result:OperationResult)=>{setOperation(result);setMessage('请求已接受，等待后端完成');void service.refresh();};
  const send=async(action:string,payload:Variables={})=>{if(!execution)return;setBusy(true);try{accepted(await service.command(execution.id,action,payload));setModal(undefined);}catch(e){setMessage(String(e));}finally{setBusy(false);}};
  const open=(action:string)=>{setJson(action==='run'?JSON.stringify(project?.initialState??{},null,2):'{}');setOverrides({});setBreakpoints([]);setModal(action);};
  const submit=async()=>{setBusy(true);try {const state=object(json); if(modal==='run'&&variant){const result=await service.start(variant.projectId,variant.id,state,breakpoints,overrides);accepted(result);selectExecution(result.executionId);setModal(undefined);}else if(modal==='fork'){await send('branches',{checkpointId:cpId,state});}else await send('state',{state});}catch(e){setMessage(String(e));}finally{setBusy(false);}};
  const blocked=busy||!!operation||!!execution?.pendingOperation||snapshot.connection==='offline';
  const active=!!execution&&['queued','pending','running','pausing','stopping'].includes(execution.status);
  const navigate=(id:string)=>{setVariantId(id);setExecutionId('');setCpId(undefined);setNodeId(undefined);};
  return <div className="app-shell"><header className="app-header"><div><h1>WTB 工作流实验台</h1><p>真实执行 · 本地控制台</p></div><span className="tag">{({connecting:'连接中',live:'实时连接',polling:'轮询更新',offline:'后端离线'})[snapshot.connection]}</span><button onClick={()=>void service.refresh()}>刷新</button></header>
    <nav className="main-nav">{[['workflow','工作流'],['components','组件库'],['compare','变体对照'],['batch','批量实验'],['audit','事件与审计'],['system','系统信息']].map(([id,label])=><button className={view===id?'active':''} key={id} onClick={()=>setView(id)}>{label}</button>)}</nav>
    {(message||snapshot.error||detail.error||cp.error)&&<div className="notice" role="status">{snapshot.error??detail.error??cp.error??message}</div>}
    {snapshot.loading?<Empty>正在读取后端项目…</Empty>:!catalog?<Empty>无法连接后端。请先启动 python -m wtb.api.console，再点击刷新。</Empty>:<>
    {view==='workflow'&&<div className="workspace-layout"><aside className="sidebar panel"><h3>项目与变体</h3>{catalog.projects.map(p=><div key={p.id}><h4>{p.name}</h4>{catalog.variants.filter(v=>v.projectId===p.id).map(v=><button className={`variant-item ${variant?.id===v.id?'selected':''}`} key={v.id} onClick={()=>navigate(v.id)}>{v.name}</button>)}</div>)}<p className="muted">Ray / 独立 venv：当前不可用</p></aside><main className="main-content">
      {!graph?<Empty>没有已注册工作流，请检查后端项目配置。</Empty>:<><section className="panel"><div className="panel-heading"><h2>{graph.projectId} / {graph.name}</h2><button className="primary" disabled={blocked} onClick={()=>open('run')}>启动真实执行</button></div><div className="toolbar"><select aria-label="执行记录" value={executionId} onChange={e=>selectExecution(e.target.value)}><option value="">选择一次执行</option>{executionId&&!snapshot.executions.some(e=>e.id===executionId)&&<option value={executionId}>{executionId}</option>}{snapshot.executions.filter(e=>e.variantId===variant?.id||e.id===executionId).map(e=><option key={e.id} value={e.id}>{e.id.slice(0,8)} · {e.status}</option>)}</select><button disabled={!snapshot.offset} onClick={()=>void service.page(snapshot.offset-50)}>上一页</button><button disabled={snapshot.offset+50>=snapshot.total} onClick={()=>void service.page(snapshot.offset+50)}>下一页</button>{execution&&<StatusBadge status={execution.status}/>}</div>
      {execution&&<div className="toolbar"><button disabled={busy||snapshot.connection==='offline'||execution.status!=='running'} onClick={()=>void send('pause')}>暂停</button><button disabled={blocked||execution.status!=='paused'} onClick={()=>{setCpId(undefined);void send('resume');}}>继续</button><button disabled={busy||['completed','failed','cancelled','stopping'].includes(execution.status)} onClick={()=>void send('stop')}>停止</button><button disabled={blocked||active||!cpId} title="暂停或结束后选择检查点" onClick={()=>void send('rollback',{checkpointId:cpId})}>回退</button><button disabled={blocked||active||!cpId} title="选择来源检查点" onClick={()=>open('fork')}>Fork</button><span>{execution.elapsedMs} ms · {execution.id}</span></div>}
      {execution?.error&&<p className="error-text">{execution.error}</p>}{cpId&&<div className="notice">历史快照 · {cpId}<button onClick={()=>setCpId(undefined)}>返回实时</button></div>}<div className="graph-layout"><WorkflowCanvas model={graph} execution={execution} checkpoint={cp.data} onSelect={setNodeId}/><Inspector graph={graph} execution={execution} nodeId={nodeId} checkpoint={cp.data} breakpoint={node=>{if(execution)void send('breakpoints',{nodes:execution.breakpoints.includes(node)?execution.breakpoints.filter(n=>n!==node):[...execution.breakpoints,node]});}}/></div></section>
      {execution&&<History key={execution.id} execution={execution} checkpoint={cp.data} revision={snapshot.revision} onCheckpoint={setCpId} onExecution={selectExecution} command={action=>action==='edit'?open('edit'):void send('checkpoints')}/>}</>}
    </main></div>}
    {view==='components'&&<ComponentLibrary catalog={catalog}/>}{view==='compare'&&<ComparePage variants={catalog.variants}/>}{view==='batch'&&<BatchPage catalog={catalog} revision={snapshot.revision} onExecution={selectExecution}/>}{view==='audit'&&<section className="panel panel-body"><h2>事件与审计</h2><Field label="按执行筛选"><select value={executionId} onChange={e=>setExecutionId(e.target.value)}><option value="">全部</option>{snapshot.executions.map(e=><option key={e.id} value={e.id}>{e.id}</option>)}</select></Field><EventTable key={executionId} executionId={executionId||undefined} revision={snapshot.revision}/></section>}{view==='system'&&<SystemPage revision={snapshot.revision}/>}</>}
    {modal&&<Modal title={modal==='run'?'启动真实工作流':modal==='fork'?'从检查点 Fork':'修改暂停状态'} onClose={()=>!busy&&setModal(undefined)}><Field label={modal==='fork'?'覆盖状态 JSON（与检查点状态合并）':'初始 / 修改状态 JSON'}><textarea rows={10} value={json} onChange={e=>setJson(e.target.value)}/></Field>{modal==='run'&&<><p>执行环境：当前后端 Python；工作目录由后端独立分配。</p>{Object.entries(project?.nodeVariants??{}).map(([node,values])=><Field key={node} label={`${node} 实现`}><select value={overrides[node]??''} onChange={e=>setOverrides(old=>{const next={...old};if(e.target.value)next[node]=e.target.value;else delete next[node];return next;})}><option value="">使用变体配置</option>{values.map(v=><option key={v} value={v}>{v}</option>)}</select></Field>)}<p>执行前断点：</p>{variant?.nodes.filter(n=>!n.id.startsWith('__')).map(n=><label key={n.id}><input type="checkbox" checked={breakpoints.includes(n.id)} onChange={e=>setBreakpoints(e.target.checked?[...breakpoints,n.id]:breakpoints.filter(id=>id!==n.id))}/>{n.id} </label>)}</>}<p role="status">{message}</p><div className="modal-actions"><button disabled={busy} onClick={()=>setModal(undefined)}>取消</button><button className="primary" disabled={busy} onClick={()=>void submit()}>提交</button></div></Modal>}
  </div>;
}
