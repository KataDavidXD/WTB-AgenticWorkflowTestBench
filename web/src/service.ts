import { components, defaultOptions, variants } from './data';
import { busy, commandReason } from './domain';
import type { AuditEvent, Checkpoint, Command, ConsoleService, DemoSnapshot, Execution, OperationResult, RuntimeBinding, StartOptions } from './domain';

const clone = <T,>(v: T): T => structuredClone(v);

export class MockConsoleService implements ConsoleService {
  private snapshot!: DemoSnapshot;
  private listeners = new Set<() => void>();
  private serial = 0;
  private clock?: ReturnType<typeof setInterval>;
  private clockRequested = false;
  constructor() { this.seed(); }
  getSnapshot = () => this.snapshot;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  private id(prefix: string) { return `${prefix}-${String(++this.serial).padStart(4, '0')}`; }
  private now() { return new Date().toISOString(); }
  private update(fn: (draft: DemoSnapshot) => void) {
    const draft = clone(this.snapshot);
    fn(draft);
    draft.revision++;
    this.snapshot = draft;
    this.listeners.forEach(l => l());
  }
  private event(s: DemoSnapshot, kind: AuditEvent['kind'], message: string, executionId?: string, level: AuditEvent['level'] = 'info') {
    s.events.unshift({ id: this.id('event'), executionId, kind, message, time: this.now(), level });
    s.events = s.events.slice(0, 1000);
    s.system.outboxPending++;
  }
  private runtime(id: string, o: StartOptions): RuntimeBinding {
    const workspace = `E:/WTB-DEMO/workspaces/${id}`;
    const requestedPath = o.environment === 'current' ? 'C:/Python312' : `E:/WTB-DEMO/venvs/${o.environment === 'reused' ? 'shared-rag' : id}`;
    const actorId = `actor-${id}`;
    const rayActorId = `demo-ray-${id.slice(5)}-9fa2b718`;
    return {
      mode: o.mode, host: 'localhost · 演示主机', pid: 18000 + this.serial, cpus: 1, gpus: 0,
      actorId: o.mode === 'ray' ? actorId : undefined, rayActorId: o.mode === 'ray' ? rayActorId : undefined,
      assignmentHistory: o.mode === 'ray' ? [{ actorId, rayActorId, reason: '初始分配（模拟）' }] : [],
      workspace, output: `${workspace}/outputs`, checkpoints: `${workspace}/checkpoints.sqlite`, cas: 'E:/WTB-DEMO/cas',
      environment: { kind: o.environment, provider: o.environment === 'current' ? '当前进程' : 'UV（模拟）', requestedPath,
        actualPython: `${o.scenario === 'environment_mismatch' ? 'C:/Python312' : requestedPath}/python.exe`,
        version: '3.12.0（演示）', match: o.scenario !== 'environment_mismatch',
        dependencies: ['langgraph', 'wtb', ...(o.mode === 'ray' ? ['ray'] : [])] },
    };
  }
  private checkpoint(e: Execution, label: string): Checkpoint {
    const cp: Checkpoint = { id: this.id('cp'), label, cursor: e.cursor, state: clone(e.state), files: clone(e.files), fileCommitId: this.id('commit'), createdAt: this.now(), epoch: e.epoch };
    e.checkpoints.push(cp);
    e.recoveryCheckpointId = cp.id;
    return cp;
  }
  private makeExecution(o: StartOptions): Execution {
    const variant = variants.find(v => v.id === o.variantId);
    if (!variant) throw new Error('变体不存在');
    const id = this.id('exec');
    const graph = clone(variant);
    graph.config.model = o.model;
    graph.config.temperature = o.temperature;
    graph.nodes.forEach(n => {
      if (n.id === 'c' && o.nodeImplementation !== 'inherit') n.implementation = o.nodeImplementation;
      if (n.id === 'e') n.implementation = o.model;
    });
    if (o.nodeImplementation !== 'inherit') graph.config.retrieval = o.nodeImplementation;
    const e: Execution = { id, variantId: variant.id, graph, status: 'queued', cursor: 0, progressTicks: 0, nodeTicks: o.nodeTicks,
      elapsedMs: 0, createdAt: this.now(), scenario: o.scenario, scenarioApplied: false,
      state: { query: o.input, count: 0, messages: [], temperature: o.temperature }, files: [], checkpoints: [], nodeRuns: [],
      runtime: this.runtime(id, o), breakpoints: [], epoch: 0 };
    this.checkpoint(e, '初始状态');
    return e;
  }
  private enter(s: DemoSnapshot, e: Execution) {
    if (e.cursor >= e.graph.route.length) {
      e.status = 'completed'; e.score = Number((0.81 + (Number(e.variantId.replace('workflow', '')) % 5) * 0.035).toFixed(2));
      this.event(s, 'execution', '执行完成 · 已生成演示评估结果', e.id); return;
    }
    const nodeId = e.graph.route[e.cursor];
    if (e.breakpoints.includes(nodeId) && e.skipBreakpoint !== nodeId) {
      e.status = 'paused';
      this.event(s, 'control', `在节点 ${nodeId} 前命中断点`, e.id); return;
    }
    e.skipBreakpoint = undefined;
    e.nodeRuns.push({ id: this.id('node-run'), nodeId, visit: e.nodeRuns.filter(n => n.nodeId === nodeId).length + 1, status: 'running', elapsedMs: 0, epoch: e.epoch });
    e.progressTicks = 0;
    this.event(s, 'node', `节点 ${nodeId} 开始`, e.id);
  }
  private advance(s: DemoSnapshot, e: Execution) {
    const nodeId = e.graph.route[e.cursor];
    let run = e.nodeRuns.at(-1);
    if (!run || run.status !== 'running' || run.epoch !== e.epoch) {
      this.enter(s, e);
      if (!busy(e)) return;
      run = e.nodeRuns.at(-1)!;
    }
    e.progressTicks++; e.elapsedMs += 1000; run.elapsedMs += 1000;
    if (e.progressTicks < e.nodeTicks) return;
    if (e.scenario === 'node_failure' && e.cursor === 1 && !e.scenarioApplied) {
      e.scenarioApplied = true; e.status = 'failed'; run.status = 'failed'; e.error = `节点 ${nodeId} 模拟请求失败；可回退或从检查点 Fork。`;
      this.event(s, 'node', e.error, e.id, 'error'); return;
    }
    run.status = 'completed';
    const messages = Array.isArray(e.state.messages) ? [...e.state.messages] : [];
    messages.push(nodeId);
    e.state = { ...e.state, count: (typeof e.state.count === 'number' ? e.state.count : 0) + 1, messages, last_node: nodeId };
    e.cursor++; e.progressTicks = 0;
    const prefix = e.cursor === e.graph.route.length ? 'final' : 'draft';
    e.files = [
      { path: 'report.txt', content: `${prefix}: ${String(e.state.query ?? '')}\n路径：${messages.join(' → ')}\n模型：${e.graph.config.model}\n计数：${e.state.count}\n（模拟输出，未写入磁盘）` },
      { path: 'state.json', content: JSON.stringify(e.state, null, 2) },
    ];
    const cp = this.checkpoint(e, `节点 ${nodeId} 完成`);
    this.event(s, 'node', `节点 ${nodeId} 完成`, e.id);
    this.event(s, 'checkpoint', `${cp.id} 关联文件提交 ${cp.fileCommitId}`, e.id);
    if (e.scenario === 'actor_reassigned' && e.cursor === 2 && !e.scenarioApplied && e.runtime.mode === 'ray') {
      e.scenarioApplied = true;
      e.runtime.actorId = `actor-reassigned-${e.id}`; e.runtime.rayActorId = `demo-ray-retry-${e.id}`; e.runtime.pid += 100;
      e.runtime.assignmentHistory.push({ actorId: e.runtime.actorId, rayActorId: e.runtime.rayActorId, reason: `在 ${cp.id} 后模拟重新分配` });
      this.event(s, 'runtime', '已模拟 Actor 重新分配，历史绑定保留', e.id);
    }
    if (e.cursor >= e.graph.route.length) { this.enter(s, e); return; }
    if (e.status === 'pausing') {
      e.status = 'paused'; this.event(s, 'control', `安全边界暂停，恢复位置 ${cp.id}`, e.id);
    } else this.enter(s, e);
  }
  private seed() {
    this.serial = 0;
    const s: DemoSnapshot = { revision: 0, projects: [{ id: 'workflow-a', name: 'Workflow A', description: '6 个组件 · 10 个变体 · 全功能演示' }], components: clone(components), variants: clone(variants), executions: [], batches: [], events: [],
      system: { cacheHits: 18, cacheMisses: 4, outboxPending: 0, outboxProcessed: 42, integrity: 'unchecked', findings: [] } };
    const completed = this.makeExecution({ ...defaultOptions, variantId: 'workflow1', mode: 'local', nodeTicks: 1 });
    completed.status = 'running'; this.enter(s, completed);
    while (busy(completed)) this.advance(s, completed);
    const previous = this.makeExecution({ ...defaultOptions, variantId: 'workflow2', environment: 'reused', nodeTicks: 1 });
    previous.status = 'running'; this.enter(s, previous); while (busy(previous)) this.advance(s, previous);
    const failed = this.makeExecution({ ...defaultOptions, variantId: 'workflow3', scenario: 'node_failure', nodeTicks: 1 });
    failed.status = 'running'; this.enter(s, failed); while (busy(failed)) this.advance(s, failed);
    const paused = this.makeExecution({ ...defaultOptions, variantId: 'workflow7', environment: 'reused', nodeTicks: 1 });
    paused.status = 'running'; this.enter(s, paused); this.advance(s, paused); paused.status = 'pausing'; this.advance(s, paused); paused.nodeTicks = 4;
    const running = this.makeExecution(defaultOptions); running.nodeTicks = 1; running.status = 'running'; this.enter(s, running); this.advance(s, running); running.nodeTicks = 6;
    s.executions = [running, paused, failed, previous, completed];
    const batchId = this.id('batch');
    [completed, previous, failed].forEach(e => { e.batchId = batchId; });
    s.batches = [{ id: batchId, name: '初始对照实验', variantIds: ['workflow1', 'workflow2', 'workflow3'], inputs: [defaultOptions.input], executionIds: [completed.id, previous.id, failed.id], createdAt: this.now() }];
    s.system.outboxPending = 3;
    this.snapshot = s;
  }
  private validate(o: StartOptions): string | undefined {
    if (!variants.some(v => v.id === o.variantId)) return '变体不存在';
    if (!o.input.trim()) return '请输入测试问题';
    if (!Number.isFinite(o.temperature) || o.temperature < 0 || o.temperature > 2) return '温度必须在 0–2 之间';
    if (!Number.isInteger(o.nodeTicks) || o.nodeTicks < 1 || o.nodeTicks > 15) return '节点步数必须在 1–15 之间';
    if (o.scenario === 'actor_reassigned' && o.mode !== 'ray') return 'Actor 重新分配场景需要 Ray 模式';
    if (o.scenario === 'environment_mismatch' && o.environment === 'current') return '环境不匹配场景请选择独立或复用环境';
    return undefined;
  }
  start(o: StartOptions): OperationResult {
    const error = this.validate(o); if (error) return { ok: false, message: error };
    let id = '';
    this.update(s => {
      const e = this.makeExecution(o); id = e.id;
      s.executions.unshift(e); this.event(s, 'execution', `创建 ${e.variantId} · ${o.mode === 'ray' ? 'Ray' : '本地'} 演示执行`, e.id);
      if (o.environment === 'reused') s.system.cacheHits++; else s.system.cacheMisses++;
      this.schedule(s);
    });
    return { ok: true, message: '已创建模拟执行', executionId: id };
  }
  startBatch(ids: string[], inputs: string[], o: StartOptions): OperationResult {
    const unique = [...new Set(ids)]; const questions = inputs.map(q => q.trim()).filter(Boolean);
    if (!unique.length || !questions.length) return { ok: false, message: '至少选择一个变体和一个测试输入' };
    if (unique.length * questions.length > 40) return { ok: false, message: '演示批次最多 40 个任务' };
    for (const id of unique) { const error = this.validate({ ...o, variantId: id, input: questions[0] }); if (error) return { ok: false, message: error }; }
    this.update(s => {
      const id = this.id('batch'); const executionIds: string[] = [];
      for (const variantId of unique) for (const input of questions) {
        const e = this.makeExecution({ ...o, variantId, input }); e.batchId = id; executionIds.push(e.id); s.executions.push(e);
        if (o.environment === 'reused') s.system.cacheHits++; else s.system.cacheMisses++;
        this.event(s, 'execution', `批量任务 ${variantId} 已排队`, e.id);
      }
      s.batches.unshift({ id, name: `对照实验 ${s.batches.length + 1}`, variantIds: unique, inputs: questions, executionIds, createdAt: this.now() });
      this.schedule(s);
    });
    return { ok: true, message: `已提交 ${unique.length * questions.length} 个演示任务（全局最多 2 个并发）` };
  }
  private schedule(s: DemoSnapshot) {
    let available = 2 - s.executions.filter(busy).length;
    for (const e of s.executions) {
      if (available <= 0) break;
      if (e.status !== 'queued') continue;
      available--; e.status = 'running'; this.event(s, 'execution', '获得执行槽位', e.id); this.enter(s, e);
      if (!busy(e)) available++;
    }
  }
  tick() {
    if (!this.snapshot.executions.some(e => busy(e) || e.status === 'queued')) return;
    this.update(s => {
      // Newly scheduled tasks start on the next tick, keeping progress observable.
      const active = s.executions.filter(busy); active.forEach(e => this.advance(s, e));
      this.schedule(s);
    });
  }
  command(id: string, c: Command): OperationResult {
    const original = this.snapshot.executions.find(e => e.id === id);
    if (!original) return { ok: false, message: '执行不存在，请重新选择' };
    const reason = commandReason(original, c.type); if (reason) return { ok: false, message: reason };
    if (c.type === 'edit' || c.type === 'fork') {
      const value = c.state;
      if (value !== undefined && (value === null || typeof value !== 'object' || Array.isArray(value))) return { ok: false, message: '状态必须为 JSON 对象' };
    }
    if (c.type === 'breakpoint' && !original.graph.nodes.some(n => n.id === c.nodeId)) return { ok: false, message: '节点不存在' };
    const cp = ('checkpointId' in c) ? original.checkpoints.find(p => p.id === c.checkpointId) : undefined;
    if ('checkpointId' in c && !cp) return { ok: false, message: '请先选择该执行的检查点' };
    let result: OperationResult = { ok: true, message: '操作已完成' };
    this.update(s => {
      const e = s.executions.find(x => x.id === id)!; e.operationError = undefined;
      if ((c.type === 'rollback' || c.type === 'fork') && e.scenario === 'restore_failure') {
        e.operationError = '模拟 CAS 文件恢复失败：未移动恢复位置，也未创建分支。';
        this.event(s, 'control', e.operationError, e.id, 'error'); result = { ok: false, message: e.operationError }; return;
      }
      switch (c.type) {
        case 'pause': e.status = 'pausing'; result.message = '暂停请求已提交，将在当前节点完成后暂停'; break;
        case 'resume':
          e.skipBreakpoint = e.graph.route[e.cursor]; e.status = 'queued'; e.error = undefined;
          result.message = '已提交继续请求'; break;
        case 'stop':
          e.status = 'cancelled';
          result.message = '模拟执行已停止，历史记录保留'; break;
        case 'checkpoint':
          this.checkpoint(e, '手动检查点'); result.message = '已创建手动检查点'; break;
        case 'edit':
          this.checkpoint(e, '修改状态前'); e.state = { ...e.state, ...clone(c.state) };
          this.checkpoint(e, '修改状态后'); result.message = '状态已更新，文件将在后续节点执行时生成新版本'; break;
        case 'breakpoint':
          e.breakpoints = e.breakpoints.includes(c.nodeId) ? e.breakpoints.filter(n => n !== c.nodeId) : [...e.breakpoints, c.nodeId];
          result.message = `已${e.breakpoints.includes(c.nodeId) ? '设置' : '取消'}节点 ${c.nodeId} 的执行前断点`; break;
        case 'rollback':
          e.cursor = cp!.cursor; e.state = clone(cp!.state); e.files = clone(cp!.files); e.progressTicks = 0;
          e.epoch++; e.status = 'paused'; e.error = undefined; e.score = undefined; e.skipBreakpoint = undefined; e.recoveryCheckpointId = cp!.id;
          result.message = `已回退到 ${cp!.id}，状态与文件已恢复，历史保留`; break;
        case 'fork': {
          const fork = this.makeExecution({ ...defaultOptions, variantId: e.variantId, mode: e.runtime.mode, environment: e.runtime.environment.kind,
            input: String(cp!.state.query ?? ''), model: e.graph.config.model, temperature: e.graph.config.temperature, nodeTicks: e.nodeTicks, scenario: e.scenario });
          fork.graph = clone(e.graph); fork.parentId = e.id; fork.fromCheckpointId = cp!.id;
          fork.cursor = cp!.cursor; fork.state = { ...clone(cp!.state), ...clone(c.state ?? {}) }; fork.files = clone(cp!.files);
          fork.status = 'paused'; fork.breakpoints = [...e.breakpoints]; fork.scenarioApplied = e.scenarioApplied;
          fork.checkpoints = []; this.checkpoint(fork, `继承自 ${cp!.id}`);
          s.executions.unshift(fork);
          this.event(s, 'execution', `从 ${e.id} / ${cp!.id} 创建分支`, fork.id);
          result = { ok: true, message: '已创建独立分支，点击继续运行', executionId: fork.id }; break;
        }
      }
      this.event(s, c.type === 'checkpoint' ? 'checkpoint' : 'control', result.message, e.id);
      this.schedule(s);
    });
    return result;
  }
  reset() {
    if (this.clock) clearInterval(this.clock);
    this.clock = undefined;
    this.seed(); this.listeners.forEach(l => l());
    if (this.clockRequested) this.installClock();
  }
  private installClock() { if (!this.clock) this.clock = setInterval(() => this.tick(), 1000); }
  startClock() {
    this.clockRequested = true; this.installClock();
    return () => { this.clockRequested = false; if (this.clock) clearInterval(this.clock); this.clock = undefined; };
  }
  systemAction(action: 'check' | 'outbox' | 'cache') {
    this.update(s => {
      if (action === 'check') {
        s.system.findings = s.executions.filter(e => e.operationError).map(e => `${e.id}：${e.operationError}`);
        s.system.integrity = s.system.findings.length ? 'issues' : 'healthy'; s.system.checkedAt = this.now();
        this.event(s, 'system', `完整性检查完成：${s.system.findings.length} 个演示问题`);
      }
      if (action === 'outbox') {
        const count = s.system.outboxPending; s.system.outboxProcessed += count;
        this.event(s, 'system', `已处理 ${count} 个模拟 Outbox 事件`); s.system.outboxPending = 0;
      }
      if (action === 'cache') { s.system.cacheHits = 0; s.system.cacheMisses = 0; this.event(s, 'system', '模拟缓存统计已重置（未操作磁盘）'); }
    });
  }
}

export const service: ConsoleService = new MockConsoleService();
