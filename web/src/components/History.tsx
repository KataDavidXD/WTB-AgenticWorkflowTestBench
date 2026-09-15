import { useState } from 'react';
import type { AuditEvent, Checkpoint, Execution } from '../domain';
import { Empty, JsonView, StatusBadge, time } from './common';

export function History({ execution, executions, events, checkpoint, onCheckpoint, onExecution, onEdit, onManual }: {
  execution?: Execution; executions: Execution[]; events: AuditEvent[]; checkpoint?: Checkpoint;
  onCheckpoint: (id?: string) => void; onExecution: (id: string) => void; onEdit: () => void; onManual: () => void;
}) {
  const [tab, setTab] = useState('checkpoints');
  const [filePath, setFilePath] = useState('report.txt');
  if (!execution) return <div className="panel"><Empty>这个变体尚无运行。点击「启动运行」创建演示执行。</Empty></div>;
  const e = execution;
  const viewFiles = checkpoint?.files ?? e.files;
  const file = viewFiles.find(f => f.path === filePath) ?? viewFiles[0];
  const index = checkpoint ? e.checkpoints.findIndex(c => c.id === checkpoint.id) : e.checkpoints.length - 1;
  const previous = index > 0 ? e.checkpoints[index - 1] : undefined;
  const previousContent = previous?.files.find(f => f.path === file?.path)?.content ?? '（之前没有该文件）';
  const ancestors: Execution[] = [];
  let parent = e.parentId;
  while (parent && ancestors.length < executions.length) {
    const found = executions.find(x => x.id === parent); if (!found) break;
    ancestors.unshift(found); parent = found.parentId;
  }
  const children = executions.filter(x => x.parentId === e.id);
  return <section className="panel history-panel">
    <div className="tabs"><div className="tab-scroll">{[['checkpoints', `检查点 ${e.checkpoints.length}`], ['branches', '执行分支'], ['state', '状态'], ['files', '文件版本'], ['events', '事件']].map(([id, label]) => <button key={id} className={tab === id ? 'active' : ''} onClick={() => setTab(id)}>{label}</button>)}</div><span className="muted small">{checkpoint ? `历史快照 ${checkpoint.id}` : '实时视图'}</span></div>
    <div className="history-body">
      {tab === 'checkpoints' && <>
        <div className="section-toolbar"><p className="muted small">选择检查点查看状态和文件；回退后仍保留所有历史。</p><button disabled={e.status !== 'paused'} title="需暂停后创建" onClick={onManual}>＋ 手动检查点</button></div>
        <div className="checkpoint-strip">{e.checkpoints.map((cp, i) => <button key={cp.id} className={`checkpoint ${checkpoint?.id === cp.id ? 'selected' : ''}`} onClick={() => onCheckpoint(cp.id)}>
          <span className="checkpoint-index">{String(i).padStart(2, '0')}</span><strong>{cp.id}</strong><span>{cp.label}</span><small>{cp.files.length} 个文件 · {time(cp.createdAt)}</small>{e.recoveryCheckpointId === cp.id && <span className="tag">当前恢复位置</span>}
        </button>)}</div>
        {checkpoint && <div className="checkpoint-details"><div><strong>{checkpoint.id}</strong><span className="muted"> · 下一步：{e.graph.route[checkpoint.cursor] ?? 'END'}</span><p className="small">文件提交 <code>{checkpoint.fileCommitId}</code> · 已完成 {checkpoint.cursor}/{e.graph.route.length} 步</p></div><button onClick={() => onCheckpoint(undefined)}>返回实时</button></div>}
      </>}
      {tab === 'branches' && <div className="lineage">
        {[...ancestors, e].map((x, i) => <div key={x.id} className={`branch ${x.id === e.id ? 'current' : ''}`} style={{ marginLeft: i * 20 }}>
          {x.fromCheckpointId && <div className="small muted">↳ 来自 {x.fromCheckpointId}</div>}<button className="link" onClick={() => onExecution(x.id)}>{x.id}</button><StatusBadge status={x.status} /><code>{x.runtime.workspace}</code>
        </div>)}
        {children.map(x => <div key={x.id} className="branch" style={{ marginLeft: (ancestors.length + 1) * 20 }}><div className="small muted">↳ 从 {x.fromCheckpointId} Fork</div><button className="link" onClick={() => onExecution(x.id)}>{x.id}</button><StatusBadge status={x.status} /><code>{x.runtime.workspace}</code></div>)}
        {!children.length && <p className="muted small">当前执行暂无子分支。暂停后选择检查点，点击 Fork。</p>}
      </div>}
      {tab === 'state' && <><div className="section-toolbar"><span>{checkpoint ? '历史状态（只读）' : '当前工作流状态'}</span><button disabled={e.status !== 'paused' || !!checkpoint} title="仅可修改暂停执行的当前状态" onClick={onEdit}>修改状态</button></div><JsonView value={checkpoint?.state ?? e.state} /></>}
      {tab === 'files' && (file ? <>
        <div className="section-toolbar"><div className="button-row">{viewFiles.map(f => <button key={f.path} className={file.path === f.path ? 'selected' : ''} onClick={() => setFilePath(f.path)}>{f.path}</button>)}</div><span className="muted small">内存中的模拟 CAS · 未写磁盘</span></div>
        <div className="file-diff"><div><h4>上一版本 · {previous?.id ?? '无'}</h4><pre>{previousContent}</pre></div><div><h4>{checkpoint ? `所选版本 · ${checkpoint.id}` : '当前文件'}</h4><pre>{file.content}</pre></div></div>
      </> : <Empty>此时尚未生成文件，选择后续检查点或继续执行。</Empty>)}
      {tab === 'events' && <EventTable events={events.filter(v => v.executionId === e.id).slice(0, 30)} />}
    </div>
  </section>;
}

export function EventTable({ events, onExecution }: { events: AuditEvent[]; onExecution?: (id: string) => void }) {
  if (!events.length) return <Empty>没有匹配的事件。</Empty>;
  return <div className="table-wrap"><table><thead><tr><th>时间</th><th>类型</th><th>执行</th><th>内容</th></tr></thead><tbody>{events.map(ev => <tr key={ev.id}><td className="nowrap">{time(ev.time)}</td><td><span className={`tag ${ev.level === 'error' ? 'error-tag' : ''}`}>{ev.kind}</span></td><td>{ev.executionId ? <button className="link" disabled={!onExecution} onClick={() => onExecution?.(ev.executionId!)}>{ev.executionId}</button> : '系统'}</td><td>{ev.message}</td></tr>)}</tbody></table></div>;
}
