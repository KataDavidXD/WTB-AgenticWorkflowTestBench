<h1 align="center">WTB</h1>

<p align="center"><strong>面向 Agent 工作流的治理、恢复与实验控制层。</strong></p>

<p align="center"><a href="README.md">English</a> · <strong>简体中文</strong></p>

WTB（Workflow Test Bench）把工作流运行在一个持久化治理层之下。它记录
execution identity、checkpoint、文件版本、variant、生命周期转换和审计证据，
让一次运行可以被检查、恢复、回滚、分叉和比较，而不需要把治理真相交给某一个
具体的工作流框架。

WTB 自带原生 Python runtime。**LangGraph 是可选集成，不是核心依赖。**
DeepSeek Harness 和其他执行引擎通过明确的 runtime port 接入。

## 为什么需要 WTB

假设一个 agent 写入了 `report.md`，在第二次工具调用后暂停，随后又使用不同的
prompt 或模型重启。普通测试框架通常只能告诉你最终调用是否返回，却无法回答：

- 这个文件由哪个 workflow、input、runtime 和 attempt 产生？
- 从哪个 checkpoint 恢复才不会静默重放已经发生的副作用？
- 能否从同一状态分出两个独立分支并比较不同 variant？
- rollback 是否同时恢复了 workflow state 和 checkpoint 绑定的文件？
- recovery target 是精确的持久 attempt，还是根据 session/path 猜出来的？

WTB 把这些问题变成显式协议。它面向的是状态和副作用都重要的 Agent 系统，
而不仅是无状态的 input/output assertion。

### WTB 负责什么

| WTB 治理 | Workflow runtime 负责 |
|---|---|
| Execution、attempt、project 和 variant identity | 模型调用与工具语义 |
| Pause、resume、rollback、fork、cancel 和 reconciliation policy | Node/agent-loop 的实际执行 |
| Checkpoint reference 与 durable state adapter | Runtime-native state 的生成 |
| Workspace snapshot、file manifest 与 content-addressable storage | 工作区的实际修改 |
| Batch orchestration、evaluation、audit event 与 outbox receipt | 框架特有的 streaming、interrupt 等语义 |

WTB 不是新的 Agent 框架，也不替代 LangGraph、DeepSeek Harness 或自定义 runtime。
它通过统一治理契约包装这些执行引擎。

## 快速开始

安装当前开发 checkout。下列示例对应 `0.4.0.dev0`，包注册表中的旧版可能还没有
这些接口：

```bash
python -m pip install -e "."
```

`WTBTestBench.create(mode="development")` 只需要基础包。下面的示例执行真实
Python node，并把 metadata 和 checkpoint 保存到 SQLite；整个过程不会导入
LangGraph。

### 原生 Python workflow

```python
from dataclasses import dataclass

from wtb.sdk import WorkflowProject, WTBTestBench


@dataclass(frozen=True)
class Edge:
    source: str
    target: str


def prepare(state):
    return {"value": 20}


def compute(state):
    return {"answer": state["value"] * 2 + 2}


class NativeGraph:
    nodes = {"prepare": prepare, "compute": compute}
    edges = [
        Edge("__start__", "prepare"),
        Edge("prepare", "compute"),
        Edge("compute", "__end__"),
    ]
    entry_point = "prepare"


project = WorkflowProject(name="answer-42", graph_factory=NativeGraph)

with WTBTestBench.create(mode="development", data_dir=".wtb-data") as bench:
    bench.register_project(project)
    execution = bench.run(project.name, {})
    assert execution.state.workflow_variables["answer"] == 42
```

原生 graph contract 很小：`nodes` 把 ID 映射到同步 callable，`edges` 提供
`source` 和 `target`，每个 node 返回一组 state update。

### Pause、重开与 resume

可执行 Python 代码不会被序列化到 state database。进程重启后，必须重新绑定
经过审查的 project definition：

```python
first = WTBTestBench.create(mode="development", data_dir=".wtb-data")
first.register_project(project)
paused = first.run(project.name, {}, breakpoints=["compute"])
execution_id = paused.id
first.close()

second = WTBTestBench.create(mode="development", data_dir=".wtb-data")
second.attach_project(project)
completed = second.resume(execution_id)
second.close()
```

这是安全边界：持久 state 本身不会被当成可信的 graph code。缺失 executable
binding 时，WTB 会 fail closed。

## 选择 runtime

| 需求 | Runtime/state 选择 | 安装方式 |
|---|---|---|
| 本地 Python workflow、持久生命周期、无框架依赖 | Native runtime + native SQLite | `pip install -e "."` |
| 分布式 variant batch | Native runtime + Ray | `pip install -e ".[ray]"` |
| LangGraph reducer/superstep/interrupt 语义 | LangGraph + SQLite | `pip install -e ".[langgraph-sqlite]"` |
| LangGraph 共享 PostgreSQL checkpoint | LangGraph + PostgreSQL | `pip install -e ".[langgraph-postgres]"` |
| Ray batch + LangGraph | 显式组合 | `pip install -e ".[ray,langgraph-sqlite]"` |
| 其他执行引擎 | 实现 `WorkflowRuntime` | 基础包 + 对应引擎 |

默认 native SQLite adapter 是 single-host state backend。集群级 workflow state
需要显式注入 `IStateAdapter`，或使用可选的 LangGraph/PostgreSQL composition；
仅仅配置 PostgreSQL metadata URL 不会自动改变 workflow-state backend。

### 可选 LangGraph 示例

```python
from langgraph.graph import END, StateGraph
from wtb.sdk import WorkflowProject, WTBTestBench


def create_graph():
    graph = StateGraph(dict)
    graph.add_node("answer", lambda state: {"answer": state["value"] * 2})
    graph.set_entry_point("answer")
    graph.add_edge("answer", END)
    return graph


project = WorkflowProject(
    name="langgraph-example",
    graph_factory=create_graph,
    runtime_backend="langgraph",
)

with WTBTestBench.create(
    mode="development",
    data_dir=".wtb-data",
    state_adapter_mode="langgraph",
    checkpointer="sqlite",
) as bench:
    bench.register_project(project)
    execution = bench.run(project.name, {"value": 21})
```

`runtime_backend="langgraph"` 必须由调用方显式选择；基础安装不会尝试导入
LangGraph。

## 架构

```text
User / experiment runner
          |
          v
WTBTestBench SDK
          |
          v
Governance application layer
  lifecycle | variants | audit/outbox | batch | CAS/file policy
          |
          v
Framework-neutral ports
  WorkflowRuntime | IStateAdapter | repositories | node executor
          |
          +----------------------+----------------------+------------------+
          |                      |                      |
     Native defaults       Optional adapters      Execution substrate
     Python runtime        LangGraph              thread pool / Ray
     SQLite state          DeepSeek Harness       UV environments
     SQLAlchemy metadata   custom runtimes        external services
```

依赖方向向内：domain/application 依赖接口，runtime 和 persistence integration
实现接口。因此，即使没有安装 LangGraph，governance policy 仍可独立运行。

### 生命周期语义

| 操作 | 结果 | Execution ID | 是否立即执行？ |
|---|---|---|---|
| `resume(id, modified_state=...)` | 从选定 recovery head 继续 | 不变 | 是 |
| `rollback(id, checkpoint_id)` | 把 execution 移到历史状态 | 不变 | 否 |
| `fork(id, checkpoint_id, new_initial_state=...)` | 创建隔离分支 | 新 ID | 否 |

Rollback 和 fork 只选择状态；后续 node 需要调用 `resume` 才会执行。启用 file
tracking 后，同步 rollback 会在提交 rewound state 前校验并恢复 checkpoint 绑定的
文件快照。缺失必要 CAS 内容时 fail closed。Workflow state 与外部文件写入不是一个
分布式事务，物理恢复后的失败仍需要 operator 明确处理。

## DeepSeek Harness 集成

WTB 把 DeepSeek Harness 作为**外部 workflow runtime**接入，而不是替代其 agent
loop：

```text
WTB governance
  -> WorkflowRuntime / DeepSeekHarnessRuntime
    -> DeepSeek Harness Python SDK
      -> JSON-RPC runtime
        -> Cordis agent composition
```

DeepSeek Harness 负责 model session、agent loop、tool execution 和 Cordis
lifecycle。WTB 负责 execution/attempt identity、durable admission、recovery
policy、checkpoint reference、workspace evidence、audit receipt 与实验比较。

当前 `0.4.0.dev0` adapter 包含 durable attempt 控制和可选 S0/M0 引用记忆 receipt。
它仍是 source-only 且受 release gate 限制，因为已发布 SDK
尚未提供 WTB 所需的完整 durable-attempt contract。缺失 durable identity、binding
或 process-generation evidence 时，adapter 会在 model/tool dispatch 前 fail closed。
目前还没有受支持的 `wtb[deepseek-harness]` distribution extra。

Source setup、协议映射、durable attempt identity、恢复规则、平台限制和发布顺序见
[DeepSeek Harness integration and operator safety](DEEPSEEK_HARNESS.md)。

## 执行证据与发布消费

可选的 `EvidenceCollector` / `SQLiteEvidenceStore` 统一维护精确 attempt 绑定，
处理重复、延迟和相互矛盾的 runtime 记录。`SQLitePublicationSink` 在同一事务内
核对当前授权、消费批准并存储实际产物 bytes；`ExternalDelivery` 持久保存请求并
查询未知结果，外部 provider 仍须自行实现原子授权检查和幂等 effect。
这些 store 与 sink 由应用显式组合。
详见[模块与运行时指南](AUTHORITY_RUNTIME.md)、[架构说明](docs/RUNTIME_EVIDENCE_ARCHITECTURE_ZH.md)
及 [material generation 迁移指南](docs/e1-material-version-upgrade.md)。

PostgreSQL 证据存储、批准消费和外部投递账本可通过 `pip install -e ".[postgres-evidence]"`
启用；使用 `PostgresEvidenceStore` / `PostgresPublicationSink`，见
[接入方式与事务保证](docs/POSTGRES_EVIDENCE_ZH.md)。它独立于 LangGraph PostgreSQL checkpointer。

## 核心操作

```python
execution = bench.run(project="my-workflow", initial_state={"query": "hello"})
checkpoints = bench.get_checkpoints(execution.id)

rollback = bench.rollback(execution.id, checkpoint_id=str(checkpoints[0].id))
if rollback.success:
    resumed = bench.resume(execution.id, modified_state={"query": "revised"})

fork = bench.fork(
    execution.id,
    checkpoint_id=str(checkpoints[0].id),
    new_initial_state={"query": "alternative"},
)
forked = bench.resume(fork.fork_execution_id)
```

### Batch 实验

```python
batch = bench.run_batch_test(
    project="my-workflow",
    variant_matrix=[
        {"retriever": "bm25", "generator": "large"},
        {"retriever": "dense", "generator": "small"},
    ],
    test_cases=[{"query": "What changed?"}],
)

for result in batch.results:
    print(result.combination_name, result.success, result.overall_score)
```

通过 `enable_ray=True` 和 `ray` extra 使用 Ray。相同 output root 仍是受治理资源；
不要让多个独立 matrix orchestrator 竞争写入同一个 result journal。

## 安装

```bash
# 基础包：native runtime 与 native SQLite state
python -m pip install -e "."

# 可选 execution/state integration
python -m pip install -e ".[ray]"
python -m pip install -e ".[langgraph-sqlite]"
python -m pip install -e ".[langgraph-postgres]"
python -m pip install -e ".[ray,langgraph-sqlite]"

# 当前源码中组合的可选功能
python -m pip install -e ".[all]"
```

开发时先检出经过审查的源码版本，再安装：

```bash
git clone https://github.com/KataDavidXD/WTB-AgenticWorkflowTestBench.git
cd WTB-AgenticWorkflowTestBench
python -m pip install -e ".[dev,api]"
python -m pytest tests/unit tests/test_api/test_console_real.py -q
```

WTB 支持 Python 3.11–3.13。可选 runtime 可能有更窄的平台限制。

## 保证与限制

- Native development/standalone composition 不需要 LangGraph。
- WTB 分离持久数据与 executable binding，重绑定必须显式发生。
- Rollback、fork、reconcile 和 cancel 校验 durable identity；无法证明目标时
  fail closed。
- Content-addressable file tracking 会检测缺失或不匹配的 snapshot。
- Native SQLite 是本地持久状态，不是集群共识机制。
- WTB 不复刻 LangGraph reducer、superstep、streaming 或 distributed checkpoint。
- WTB 无法把自己的数据库、外部 runtime 与任意 tool side effect 变成一个原子
  事务。Unknown outcome 保持 recovery-required，不会被盲目重放。

## 项目状态

当前 package version 为 `0.4.0.dev0`。Native execution、可选 LangGraph、Ray
batch、lifecycle control、SQLite persistence、file/CAS tracking 和 framework-
neutral runtime boundary 均由测试覆盖。DeepSeek Harness 仍是 source-only 开发
集成，直到所需 durable-attempt SDK contract 被正式发布并固定。

真实 provider smoke test 位于 `examples/modes_quick_demo.py`；它需要
OpenAI-compatible endpoint，且不会退化为 mock。Live PostgreSQL、external Ray
Client 和 Docker UV environment 检查是 `tests/integration/` 与 `scripts/` 中的
opt-in integration tests。

## 贡献

欢迎贡献 runtime/state adapter、lifecycle correctness、file governance、evaluation、
文档与平台验证。提交 PR 前请运行相关 focused tests。Runtime、PostgreSQL、Ray
和真实 provider 测试需要相应的可选依赖与环境。

WTB 延续了 [Agent Git](https://github.com/HKU-MAS-Infra-Layer/Agent-Git)
探索的核心思想。合作方：HKU CAMO Lab。

## License

Apache License 2.0，详见 [LICENSE](LICENSE)。
