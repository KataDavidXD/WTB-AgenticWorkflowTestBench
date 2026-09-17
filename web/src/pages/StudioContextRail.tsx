import { COMPONENTS, NODESTATUS } from './StudioDemoData';
import { StudioIcon } from './StudioIcon';
import { useStudio } from './StudioProvider';
import { nodeContext } from './StudioState';
export function CopyButton({ value, label }: { value: string; label: string }) {
  const { notify } = useStudio();
  return <button className="copy-btn" aria-label={label} onClick={async () => { try { await navigator.clipboard.writeText(value); notify('已复制：' + value); } catch { notify('当前浏览器限制复制。完整路径可在执行上下文中选中。'); } }}><StudioIcon name="copy" size={12} /></button>;
}
export function StudioContextRail() {
  const { state, setDrawer } = useStudio(), c = nodeContext(state), comp = COMPONENTS.find(x => x.id === c.c)!, actor = c.inherited ? '继承状态 · 无当前 Actor' : c.actor, env = c.envPath || (c.inherited ? '恢复时重新解析环境' : c.cfg ? '待调度 · ' + c.envSpec : '不属于当前变体');
  return <div className="context-rail" id="context-rail"><div className="context-cell"><div className="label">当前节点上下文 · {NODESTATUS[c.ns]}</div><div className="value"><span className="node-key">{c.c}</span><span className="truncate">{comp.name} <span className="muted mono">/ {actor}</span></span></div></div><div className="context-cell"><div className="label"><StudioIcon name="box" size={11} /> 本地控制端 WORKSPACE · 实际路径</div><div className="value"><code className="truncate" title={c.workspace}>{c.workspace}</code><CopyButton value={c.workspace} label="复制本地 workspace" /></div></div><div className="context-cell"><div className="label"><StudioIcon name="terminal" size={11} /> {c.allocated ? c.host : 'EXECUTION ENVIRONMENT'} · 节点 PYTHON</div><div className="value"><code className="truncate" title={env}>{env}</code>{c.envPath && <CopyButton value={c.envPath} label="复制 Python 环境路径" />}</div></div><button className="outline-btn" onClick={() => setDrawer(true)}>执行上下文 <StudioIcon name="arrow" size={13} /></button></div>;
}
