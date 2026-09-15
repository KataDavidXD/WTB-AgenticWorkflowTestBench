import type { ComponentDef, StartOptions, Variant } from './domain';

export const components: ComponentDef[] = [
  { id: 'a', name: '输入与文档', description: '接收问题、加载演示文档，建立输入状态。', version: '1.0', tags: ['输入', '文档'] },
  { id: 'b', name: '文本切块', description: '将文档切分成可检索片段。', version: '1.2', tags: ['处理', '切块'] },
  { id: 'c', name: '检索组件', description: '按 dense、BM25 或 hybrid 方案选择相关资料。', version: '2.1', tags: ['RAG', '检索'] },
  { id: 'd', name: '相关性评估', description: '评估资料质量，决定生成答案或再次检索。', version: '1.1', tags: ['评估', '路由'] },
  { id: 'e', name: '模型生成', description: '模拟模型生成回复；演示中不发出任何模型请求。', version: '2.0', tags: ['LLM', '生成'] },
  { id: 'f', name: '文件输出', description: '生成报告文本及摘要，在模拟 CAS 中保存版本。', version: '1.0', tags: ['文件', 'CAS'] },
];

const definitions = [
  { route: ['a', 'b', 'c', 'e', 'f'], label: '标准 RAG', retrieval: 'dense' },
  { route: ['a', 'c', 'd', 'e', 'f'], label: '质量评估', retrieval: 'hybrid' },
  { route: ['a', 'b', 'c', 'd', 'e', 'f'], label: '完整流程', retrieval: 'bm25' },
  { route: ['a', 'e', 'f'], label: '直接生成', retrieval: 'none' },
  { route: ['a', 'c', 'f'], label: '检索导出', retrieval: 'bm25' },
  { route: ['a', 'b', 'c', 'e', 'f'], label: '大块切分', retrieval: 'hybrid' },
  { route: ['a', 'c', 'd', 'c', 'd', 'e', 'f'], label: '循环改进', retrieval: 'dense' },
  { route: ['a', 'b', 'f'], label: '文档预处理', retrieval: 'none' },
  { route: ['a', 'c', 'd', 'f'], label: '评估后导出', retrieval: 'hybrid' },
  { route: ['a', 'b', 'c', 'd', 'e', 'f'], label: '模型对照', retrieval: 'dense' },
];

export const variants: Variant[] = definitions.map((def, i) => {
  const ids = [...new Set(def.route)];
  const edges: Variant['edges'] = [];
  def.route.slice(1).forEach((target, n) => {
    const source = def.route[n];
    if (edges.some(e => e.source === source && e.target === target)) return;
    edges.push({ id: `${source}-${target}`, source, target, conditional: source === 'd', label: source === 'd' ? (target === 'c' ? '不足 · 重试一次' : '通过') : undefined });
  });
  if (i === 1) edges.push({ id: 'd-f', source: 'd', target: 'f', label: '不通过 · 本次未选', conditional: true });
  return {
    id: `workflow${i + 1}`, projectId: 'workflow-a', name: `workflow${i + 1}`,
    description: def.label, tags: [i === 6 ? '循环' : i === 1 ? '条件分支' : '线性', def.retrieval === 'none' ? '处理' : 'RAG'],
    route: def.route,
    nodes: ids.map((id, n) => ({ id, componentId: id, implementation: id === 'c' ? def.retrieval : id === 'e' ? (i === 9 ? 'model-large' : 'model-small') : id === 'b' ? (i === 5 ? 'chunk-1000' : 'chunk-500') : 'default', x: (n % 3) * 230, y: Math.floor(n / 3) * 175 })),
    edges,
    config: { model: i === 9 ? 'model-large' : 'model-small', temperature: i === 9 ? 0.7 : 0, retrieval: def.retrieval },
  };
});

export const defaultOptions: StartOptions = {
  variantId: 'workflow2', mode: 'ray', scenario: 'normal', environment: 'venv',
  input: '总结本季度的业务表现，并生成报告。', model: 'model-small', temperature: 0,
  nodeImplementation: 'inherit', nodeTicks: 4,
};
