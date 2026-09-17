export function StudioGuide({ onClose }: { onClose: () => void }) {
  return <>
 <button className="outline-btn close-guide" id="close-guide" onClick={onClose}>关闭 ×</button>
 <div className="modal-eyebrow">THE UNDERLYING STRUCTURE</div><h2>Not a dashboard.<br /><em>A set of projections.</em></h2>
 <p>这里不是四套数据，也不是四张不同配色的流程图。四个视角共用同一组 WTB 项目、变体、运行、检查点与资源关系，只选择不同的数学投影。</p>
 <h3>01 / 定义层：带类型、带参数的图族</h3>
 <div className="formula">{"C = { A, B, C, D, E, F }                         组件类型集合\nGᵥ = (Nᵥ, Eᵥ, τᵥ, θᵥ)                         变体定义\nτᵥ : Nᵥ → C                                    节点 → 类型\nθᵥ(n) = (implementation, config, env_spec)       节点配置"}</div>
 <p>组件类型不等于节点实例；一个类型可以在图中出现多次。矩阵 M[v,c] 能表示组件是否被使用，却不能完整表示边、顺序、参数与多实例关系。这个原型采用「每个类型至多一个实例、可选旁路边」的简化数据；生产版的循环需要按实际执行步展开。</p>
 <h3>02 / 执行层：运行、事件与检查点</h3>
 <div className="formula">{"π : Run → Variant                              一个变体可有多次运行\nsᵣ(t) = (cursor, node_states, runtime_status)    当前运行状态\nH = (Checkpoints ∪ Events, ParentEdges)         执行谱系\nκ = (graph_state, tracked_files, metadata)      检查点的恢复范围"}</div>
 <p>Fork 从已有检查点建立新 run 与独立 workspace，不自动建立新变体。回退在原 run 内新建一个执行尝试，保留旧尝试与旧检查点。两者都不删除历史；不会把未来执行“抹去”。本原型的暂停在下一个节点边界生效。</p>
 <h3>03 / 承载层：随时间变化的映射，不是节点的固有属性</h3>
 <div className="formula">{"μᵣ,t : NodeAttempt → Executor                   Actor 或本地进程\nηᵣ,t : NodeAttempt → (Host, PythonEnv)           节点对应环境\nωᵣ,t : NodeAttempt → (Host, WorkingDirectory)    实际执行目录\nworkspace(Run) ≠ actor_workdir(NodeAttempt)     控制端与执行端分开"}</div>
 <p>Ray Actor 不是变体本身的属性；重试、重新调度后映射会变化。未调度节点不显示虚构的 Actor。Fork / 回退恢复的是图状态与已纳入跟踪的文件，进程、Actor 内存、venv 安装状态与外部工具副作用不在“完整恢复”的承诺中。</p>
 <h3>04 / 四个投影，四种任务</h3>
 <table><thead><tr><th>视角</th><th>几何编码</th><th>适用任务 / 边界</th></tr></thead><tbody>
 <tr><td>路径织谱</td><td>组件阶段为经；每个变体为纬；配置决定经线上位置。</td><td>比较组成与参数。重合只表示配置相同，不代表结果已缓存。额外旁路边独立标注。</td></tr>
 <tr><td>分支地形</td><td>横轴为本变体的逻辑进度，纵向展开执行尝试；线连接真实父检查点。</td><td>回退 / Fork 与历史审计。不是墙钟时间轴；不会用线长暗示耗时。</td></tr>
 <tr><td>执行剖面</td><td>工作流、执行宿主、环境三个平行层，由映射连线贯穿。</td><td>查 Actor、主机、workspace、Python 路径；实线为已分配，虚线为尚未调度。</td></tr>
 <tr><td>变体图谱</td><td>两个离散配置维度组成平面；环段表示已注册节点的使用 / 缺席；外环表示状态。</td><td>查看实验覆盖与选定两项比较。点间距离无连续数值意义。</td></tr>
 </tbody></table>
 <h3>交互约定</h3><p>点击变体或谱线切换运行；点击节点切换执行上下文；下方小圆点选择已经持久化的检查点，不会自动回退。暂停 / 继续、回退与 Fork 会提交至 WTB 服务，再同步到四个视图。灰色圆点表示尚未生成的检查点；资源详情中的路径可复制。</p>
 <p>两变体的差异分开报告为「配置槽位差异数 Δcfg」和「组件级边集对称差 Δedge」；不将未经验证的权重合成一个所谓“相似度分数”。</p>
</>;
}
