import { useState } from 'react';
import type { ComponentDef, Execution, Variant } from '../domain';
import { PathField, Tags, duration } from './common';

export function Inspector({ execution, variant, components, nodeId, onBreakpoint, onCopy }: {
  execution?: Execution; variant: Variant; components: ComponentDef[]; nodeId?: string;
  onBreakpoint: (id: string) => void; onCopy: (text: string) => void;
}) {
  const [tab, setTab] = useState('node');
  const node = (execution?.graph ?? variant).nodes.find(n => n.id === nodeId) ?? (execution?.graph ?? variant).nodes[0];
  const component = components.find(c => c.id === node.componentId)!;
  const runtime = execution?.runtime;
  return <aside className="inspector panel">
    <div className="panel-heading"><h2>详情</h2><span className="muted small">实时资源 · 模拟</span></div>
    <div className="tabs" aria-label="详情分类">{[['node', '组件'], ['runtime', '运行资源'], ['environment', '环境']].map(([id, title]) => <button key={id} className={tab === id ? 'active' : ''} onClick={() => setTab(id)}>{title}</button>)}</div>
    <div className="inspector-body">
      {tab === 'node' && <>
        <div className="component-title"><span className="component-letter large">{component.id.toUpperCase()}</span><div><h3>{component.name}</h3><span className="muted">节点 {node.id} · v{component.version}</span></div></div>
        <p>{component.description}</p><Tags items={component.tags} />
        <dl><dt>实现</dt><dd><code>{node.implementation}</code></dd><dt>模型配置</dt><dd>{(execution?.graph ?? variant).config.model}</dd><dt>温度</dt><dd>{(execution?.graph ?? variant).config.temperature}</dd><dt>累计耗时</dt><dd>{duration(execution?.nodeRuns.filter(r => r.nodeId === node.id).reduce((n, r) => n + r.elapsedMs, 0) ?? 0)}</dd></dl>
        <button className="wide" disabled={!execution} title={!execution ? '请先启动一次运行' : '在下一次进入该节点之前暂停'} onClick={() => onBreakpoint(node.id)}>{execution?.breakpoints.includes(node.id) ? '取消节点前断点' : '设置节点前断点'}</button>
        <h4>节点执行记录</h4><div className="small muted">包含循环、回退之前的历史记录</div>
        {execution?.nodeRuns.filter(r => r.nodeId === node.id).map(r => <div key={r.id} className="record-row"><span>第 {r.visit} 次 · 分段 {r.epoch}</span><span>{r.status === 'completed' ? '完成' : r.status === 'failed' ? '失败' : '曾启动'} · {duration(r.elapsedMs)}</span></div>)}
      </>}
      {tab === 'runtime' && (runtime ? <>
        <div className="info-note">以下路径与标识均为模拟数据；复制不会打开目录。</div>
        <dl><dt>执行模式</dt><dd>{runtime.mode === 'ray' ? 'Ray Actor' : '本地进程'}</dd><dt>主机</dt><dd>{runtime.host}</dd><dt>进程 PID</dt><dd>{runtime.pid}</dd><dt>资源</dt><dd>{runtime.cpus} CPU / {runtime.gpus} GPU</dd></dl>
        {runtime.mode === 'ray' && <><PathField label="WTB Actor" path={runtime.actorId!} onCopy={onCopy} /><PathField label="Ray Actor ID" path={runtime.rayActorId!} onCopy={onCopy} /></>}
        <h4>Workspace</h4>
        <PathField label="工作目录" path={runtime.workspace} onCopy={onCopy} /><PathField label="输出目录" path={runtime.output} onCopy={onCopy} />
        <PathField label="检查点数据库" path={runtime.checkpoints} onCopy={onCopy} /><PathField label="CAS 存储" path={runtime.cas} onCopy={onCopy} />
        {runtime.assignmentHistory.length > 0 && <><h4>Actor 分配历史</h4>{runtime.assignmentHistory.map(a => <div className="assignment" key={a.actorId}><code>{a.actorId}</code><small>{a.reason}</small></div>)}</>}
      </> : <p className="muted">尚未运行，暂无资源绑定。</p>)}
      {tab === 'environment' && (runtime ? <>
        <div className={runtime.environment.match ? 'info-note success-note' : 'info-note error-note'}>{runtime.environment.match ? '✓ 配置与实际解释器一致（模拟）' : '! 环境不匹配：模拟回退到了宿主 Python'}</div>
        <dl><dt>环境方式</dt><dd>{{ current: '当前环境', venv: '独立虚拟环境', reused: '复用虚拟环境' }[runtime.environment.kind]}</dd><dt>提供者</dt><dd>{runtime.environment.provider}</dd><dt>准备状态</dt><dd>已就绪（模拟）</dd><dt>Python 版本</dt><dd>{runtime.environment.version}</dd></dl>
        <PathField label="要求的环境路径" path={runtime.environment.requestedPath} onCopy={onCopy} />
        <PathField label="实际解释器" path={runtime.environment.actualPython} onCopy={onCopy} />
        <h4>依赖规格</h4><Tags items={runtime.environment.dependencies} />
        <p className="muted small">此面板展示配置与运行环境的区别，不安装依赖、不创建真实 venv。</p>
      </> : <p className="muted">选择一次执行以查看环境。</p>)}
    </div>
  </aside>;
}
