import { describe, expect, it } from 'vitest';
import { canControl, displayNodeStatus, unionNodeIds } from './studio-model';

describe('真实 Studio 数据投影', () => {
  it('按 API 返回的图动态合并节点，不假设 A 到 F', () => {
    expect(unionNodeIds([{ id: 'a', nodes: [{ id: 'prepare', implementation: 'prepare' }], edges: [] }, { id: 'b', nodes: [{ id: 'finish', implementation: 'finish' }, { id: 'prepare', implementation: 'prepare' }], edges: [] }])).toEqual(['prepare', 'finish']);
  });
  it('节点状态只从实际节点运行记录产生', () => {
    expect(displayNodeStatus('transform', [{ id: 'transform', implementation: 'uppercase' }], [{ nodeId: 'transform', status: 'completed' }])).toBe('completed');
    expect(displayNodeStatus('missing', [], [])).toBe('missing');
  });
  it('控制按钮遵循服务端执行状态', () => {
    expect(canControl('running', 'pause')).toBe(true);
    expect(canControl('running', 'rollback')).toBe(false);
    expect(canControl('paused', 'fork')).toBe(true);
  });
});
