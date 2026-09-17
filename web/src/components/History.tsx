import { useState } from 'react';
import type { AuditEvent, Checkpoint, Execution, FileVersion, Page } from '../domain';
import { useRemote } from '../useRemote';
import { Empty, JsonView, time } from './common';

export function EventTable({ executionId, revision }: { executionId?: string; revision: number }) {
  const [offset, setOffset] = useState(0);
  const result = useRemote<Page<AuditEvent>>(`/audit/events?offset=${offset}&limit=30${executionId ? `&executionId=${executionId}` : ''}`, revision);
  return <>{result.error && <p role="alert">{result.error}</p>}<table><thead><tr><th>顺序</th><th>时间</th><th>执行 / 类型</th><th>事件</th></tr></thead><tbody>{result.data?.items.map(e => <tr key={e.id}><td>{e.seq}</td><td>{time(e.time)}</td><td>{e.executionId?.slice(0, 8)} / {e.kind}</td><td>{e.message}</td></tr>)}</tbody></table><button disabled={!offset} onClick={() => setOffset(offset - 30)}>上一页</button><button disabled={offset + 30 >= (result.data?.total ?? 0)} onClick={() => setOffset(offset + 30)}>下一页</button></>;
}

function Files({ execution, checkpoint, revision }: { execution: Execution; checkpoint?: Checkpoint; revision: number }) {
  const cp = checkpoint?.checkpointId ?? execution.checkpointId;
  const [path, setPath] = useState('');
  const [compare, setCompare] = useState('');
  const base = cp ? `/executions/${execution.id}/checkpoints/${cp}` : undefined;
  const list = useRemote<FileVersion[]>(base ? base + '/files' : undefined, revision);
  const selected = list.data?.some(f => f.path === path) ? path : list.data?.[0]?.path;
  const preview = useRemote<{ content: string | null; reason?: string }>(base && selected ? `${base}/file?path=${encodeURIComponent(selected)}` : undefined);
  const cps = useRemote<Page<Checkpoint>>(`/executions/${execution.id}/checkpoints?limit=200`, revision);
  const before = useRemote<{ content: string | null; reason?: string }>(compare && selected ? `/executions/${execution.id}/checkpoints/${compare}/file?path=${encodeURIComponent(selected)}` : undefined);
  return <><p>文件内容读取自所选检查点的 CAS commit。</p>{list.error && <p role="alert">{list.error}</p>}{!list.data?.length && <Empty>此检查点没有输出文件</Empty>}<div className="toolbar"><select aria-label="文件" value={selected ?? ''} onChange={e => setPath(e.target.value)}>{list.data?.map(f => <option key={f.path} value={f.path}>{f.path} · {f.size} bytes</option>)}</select><select aria-label="对比检查点" value={compare} onChange={e => setCompare(e.target.value)}><option value="">选择历史版本对比</option>{cps.data?.items.map(c => <option key={c.id} value={c.checkpointId}>步骤 {c.step} · {c.checkpointId.slice(0, 8)}</option>)}</select></div><div className="compare-grid">{compare && <div><h4>对比版本</h4><pre>{before.error ?? before.data?.content ?? before.data?.reason ?? '读取中'}</pre></div>}<div><h4>当前所选版本</h4><pre>{preview.error ?? preview.data?.content ?? preview.data?.reason ?? '选择文件'}</pre></div></div></>;
}

export function History({ execution, checkpoint, revision, onCheckpoint, onExecution, command }: { execution: Execution; checkpoint?: Checkpoint; revision: number; onCheckpoint: (id?: string) => void; onExecution: (id: string) => void; command: (action: string) => void }) {
  const [tab, setTab] = useState('checkpoints');
  const [offset, setOffset] = useState(0);
  const cps = useRemote<Page<Checkpoint>>(tab === 'checkpoints' ? `/executions/${execution.id}/checkpoints?offset=${offset}&limit=20` : undefined, revision);
  const branches = useRemote<Execution[]>(tab === 'branches' ? `/executions/${execution.id}/branches` : undefined, revision);
  return <section className="panel history"><div className="tabs">{[['checkpoints','检查点'],['branches','执行分支'],['state','状态'],['files','文件版本'],['events','事件']].map(([id,label]) => <button className={tab === id ? 'active' : ''} key={id} onClick={() => setTab(id)}>{label}</button>)}</div><div className="panel-body">
    {tab === 'checkpoints' && <><button onClick={() => onCheckpoint()}>返回实时运行</button><button disabled={execution.status !== 'paused' || !!execution.pendingOperation || !!checkpoint} onClick={() => command('checkpoint')}>手动检查点</button>{cps.error && <p role="alert">{cps.error}</p>}<table><thead><tr><th>检查点</th><th>时间</th><th>下一步</th><th>文件提交</th></tr></thead><tbody>{cps.data?.items.map(c => <tr key={c.id}><td><button className="link" onClick={() => onCheckpoint(c.checkpointId)}>步骤 {c.step} · {c.checkpointId.slice(0,8)}</button></td><td>{time(c.createdAt)}</td><td>{c.nextNodes.join(', ') || '结束'}</td><td>{c.fileCommitId.slice(0,8)}</td></tr>)}</tbody></table><button disabled={!offset} onClick={() => setOffset(offset-20)}>上一页</button><button disabled={offset+20 >= (cps.data?.total ?? 0)} onClick={() => setOffset(offset+20)}>下一页</button></>}
    {tab === 'branches' && <>{execution.parentId && <button onClick={() => onExecution(execution.parentId!)}>父执行 {execution.parentId.slice(0,8)}</button>}<p>来源检查点：{execution.fromCheckpointId ?? '无（根执行）'}</p>{branches.error && <p>{branches.error}</p>}{branches.data?.map(e => <p key={e.id}><button onClick={() => onExecution(e.id)}>子执行 {e.id.slice(0,8)}</button> · {e.status}</p>)}</>}
    {tab === 'state' && <><button disabled={execution.status !== 'paused' || !!execution.pendingOperation || !!checkpoint} onClick={() => command('edit')}>修改状态</button><JsonView value={checkpoint?.state ?? execution.state} /></>}
    {tab === 'files' && <Files execution={execution} checkpoint={checkpoint} revision={revision} />}
    {tab === 'events' && <EventTable executionId={execution.id} revision={revision} />}
  </div></section>;
}
