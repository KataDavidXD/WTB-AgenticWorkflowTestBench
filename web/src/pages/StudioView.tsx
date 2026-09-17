import type { ReactNode } from 'react';
import { StudioContextRail } from './StudioContextRail';
import { byVariant, configurationDifference as diff, edgeDifference as edgeDiff, NODESTATUS } from './StudioDemoData';
import { lineageGeometry } from './BranchAtlasStage';
import { useStudio } from './StudioProvider';
import { nodeContext, selectedCheckpoint, selectedVariant } from './StudioState';
export function StudioView({ children, toolbar }: { children: ReactNode; toolbar: ReactNode }) {
  return <main className="work-surface"><div className="view-toolbar" id="view-toolbar">{toolbar}</div><div className="stage" id="stage" aria-label="交互可视化画布">{children}</div><StudioFootnote /><StudioContextRail /></main>;
}
function StudioFootnote() {
  const { state } = useStudio(), v = selectedVariant(state), ctx = nodeContext(state), base = byVariant(state.baseline), lineage = lineageGeometry(state);
  const notes = {
    loom: ['同一配置自然成束；空位表示未使用。重合不代表已经复用缓存。下方保留选中变体的真实边关系。', `${v.name} · 点击线 / 名字选变体，点击节点查资源`],
    lineage: [`当前定义 ${v.name} · ${lineage.familyRuns.length} 次运行 · ${lineage.tracks.length} 条执行尝试。相同 run 内回退，新 run 上 Fork；旧历史始终保留。`, `选中 ${selectedCheckpoint(state).id} · 使用下方控制操作`],
    section: ['每条竖线都是节点执行映射，不是固定归属。虚线不冒充已分配 Actor；恢复的上游状态不冒充重新执行。', `${state.selectedNode} · ${ctx.host} · ${NODESTATUS[ctx.ns]}`],
    atlas: ['坐标轴是离散配置，不暗示模型好坏或数值距离。空格仅表示这 10 个示例变体尚未覆盖，不代表组合可执行。', `${base.name} → ${v.name} · 配置 Δ${diff(base, v)} / 边集 Δ${edgeDiff(base, v)}`],
  }[state.view];
  return <div className="view-footnote" id="view-footnote"><span>{notes[0]}</span><span className="tag">{notes[1]}</span></div>;
}
