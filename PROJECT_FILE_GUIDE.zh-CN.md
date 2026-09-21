# WTB 项目逐文件中文导读

本导读基于本地目录、Python 模块说明与类/函数声明，以及重点执行代码、配置和脚本的静态阅读。文件作用不等于功能已在当前环境验证；本次没有安装依赖、启动服务或运行测试。

## 先看整体结构

```text
项目根目录
├── .github/workflows/     GitHub 自动检查和 PyPI 发布
├── examples/             简易示例、安装自检和 RAG + SQL 完整演示
├── scripts/              外部服务联调脚本
├── wtb/                  可安装的 Python 库
│   ├── sdk/              用户入口与项目配置
│   ├── application/      运行、回退、分支和批量实验流程
│   ├── domain/           业务对象、接口和事件定义
│   ├── infrastructure/   数据库、LangGraph、文件、环境等具体实现
│   ├── api/              HTTP、WebSocket 和 gRPC 入口
│   └── testing/          提供给库使用者的测试辅助对象
├── tests/                项目自己的测试
└── uv_venv_manager/      本地存在，但当前为空
```

## 几个名词

- SDK：你在 Python 里直接调用的入口。
- Domain Model：工作流、执行、检查点等业务对象，包含业务数据和规则。
- Interface：规定一个组件必须提供什么操作，不负责所有具体实现。
- Adapter：将 WTB 的操作转换成 LangGraph 等底层系统的操作。
- Repository：负责存取某类业务对象的数据访问组件。
- ORM：Python 对象与数据库表之间的映射。
- UnitOfWork / UoW：把多个仓库更新组织进一个事务。
- Factory：选择并组装实现，同时明确谁负责关闭资源。
- Event：表示一件已经发生的事，交给监听器记录、统计或处理。
- Outbox：先将待处理事件与业务数据一起存入数据库，再交后台处理；不意味着文件和所有数据库组成一个原子事务。
- CAS / Blob：用内容哈希标识文件内容，多个版本可以复用相同内容。
- Fixture：测试前准备对象/环境，测试后清理的公共逻辑。

## 主要调用关系

```text
你的代码
  → wtb.sdk.WTBTestBench
  → 项目服务 / ExecutionController / 批量执行器
  → 状态适配器 + 文件追踪服务 + UnitOfWork/仓库
  → LangGraph saver / SQLite 或 PostgreSQL / 本地文件

HTTP 或 gRPC 请求
  → wtb.api
  → application.services.api_services
  → 执行、项目、批量等应用逻辑
```

这是运行协作关系，不是严格的 imports 层级图。domain 提供各层使用的对象与接口。

## 容易看错的地方

1. 根目录 main.py 只是占位打印脚本。SDK 和示例才是主要阅读入口。
2. wtb/sdk/test_bench.py 虽然以 test_ 开头，却是正式实现；不能根据文件名认定它是测试。
3. wtb/testing 是随库提供的辅助能力，tests 是本仓库测试代码；打包配置排除了 tests 和 examples。
4. wtb/config.py、SDK 的 workflow_project.py 和 database/config.py 分别面向内部整体配置、用户项目配置、数据库配置。
5. domain/models 的对象表达业务，database/models.py 的对象表达数据库表；字段转换由仓库/mapper 完成。
6. domain/events 定义事件内容，infrastructure/events 负责发布、桥接、审计和统计。
7. adapters 管状态与执行适配，stores 侧重检查点存取；两者在当前代码中同时存在。
8. UV 客户端不是环境服务本身。本地 uv_venv_manager 目录为空，不能直接假设这里可启动 Docker 服务。
9. .gitignore 的 file_processing/ 规则会匹配同名子目录。wtb/domain/models/file_processing 的文件实际存在，但遵循忽略规则的搜索可能漏掉它们；这不单独证明文件未被 Git 跟踪。
10. tests 中多套目录按模块、集成场景和历史修复分别组织，名字和覆盖范围会重叠。integration/real 等名字不能代替检查实际依赖和断言；部分测试使用模拟对象。
11. __init__.py 不一定为空，有些负责公开导出、延迟导入或兼容入口，下面逐个列出。
12. 普通 rg 默认会漏掉被忽略的 examples/quick_start、examples/wtb_presentation 和 ray_file_cleanup_demo.py；这些文件当前确实在本地，已全部纳入本表。
13. 代码注释中仍有 AgentGit、旧版本号和历史文档引用；阅读时以当前实现和文件位置为准。

## 推荐阅读顺序

1. examples/modes_quick_demo.py：看用户怎么定义和运行图。
2. wtb/sdk/workflow_project.py：看注册时提交的配置。
3. wtb/sdk/test_bench.py：从 run 方法追调用。
4. wtb/domain/models/workflow.py、checkpoint.py：理解执行和检查点。
5. wtb/application/services/execution_controller.py：看运行、回退和分支。
6. wtb/infrastructure/adapters/langgraph_state_adapter.py：看 LangGraph 接入。
7. wtb/infrastructure/file_tracking/sqlite_service.py：看状态和文件版本如何关联。
8. tests/test_sdk 与 tests/integration：用测试场景检验自己对行为的理解。

下面按实际目录逐文件列出。链接指向本地文件；“定位线索”来自文件实际的类或函数声明，仅列少量帮助导航，不是完整 API 清单。生成的 gRPC 文件和二进制/锁文件按其格式与用途说明，不做业务级审计。.git 内部对象不逐个列出，它们是 Git 管理的数据。


## 根目录

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [.env.example](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/.env.example) | 环境变量配置示例，供本地模型服务和其他配置参考。 | — |
| [.gitignore](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/.gitignore) | Git 忽略规则：缓存、环境、数据、部分文档/示例、外部环境服务目录等。 | — |
| [.python-version](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/.python-version) | 本地工具使用的 Python 版本提示，目前为 3.12。 | — |
| [0001-wtb-pgsql.patch](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/0001-wtb-pgsql.patch) | 保存的一份 PostgreSQL 相关 Git 补丁，包含多文件修改记录；普通运行不会自动应用它。 | — |
| [LICENSE](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/LICENSE) | Apache 2.0 许可证文本。 | — |
| [MANIFEST.in](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/MANIFEST.in) | 源码分发包的文件包含/排除规则，包含 SQL、proto，排除 tests、examples 和外部服务等。 | — |
| [README.md](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/README.md) | 项目介绍、安装方法、SDK 示例、回退/继续/分支语义及已知边界。 | — |
| [_ver.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/_ver.py) | 导入 wtb 并打印包版本的小脚本。 | — |
| [agentgit-0.2.0a0-py3-none-any.whl](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/agentgit-0.2.0a0-py3-none-any.whl) | 旧 AgentGit 预发布版 wheel 安装包；是二进制归档，不是当前 wtb 源码入口。这里只按包名识别，未解包审计。 | — |
| [install_checker.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/install_checker.py) | 安装自检/冒烟测试：检查 SDK 执行、检查点、回退、分支、批量等；按条件进一步检查 Ray 和 gRPC。文件头 examples 路径对应另一份本地示例脚本，不是此文件自身。 | `record`、`check_import` |
| [main.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/main.py) | 占位脚本，仅打印问候语；不是 WTB 的实际服务或 SDK 入口。 | `main` |
| [pyproject.toml](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/pyproject.toml) | Python 包的正式配置：包名、版本、Python 版本范围、核心依赖、可选依赖、构建方式和 pytest/Ruff 配置。 | — |
| [requirements.txt](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/requirements.txt) | 固定版本的依赖清单，包含比核心 SDK 更广的库；理解安装需求时优先看 pyproject.toml 的依赖分组。 | — |
| [uv.lock](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/uv.lock) | uv 解析出的依赖锁文件，用于固定依赖解析结果；不能仅凭它存在就认定它与当前配置完全同步。 | — |

## .github/workflows

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [publish.yml](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/.github/workflows/publish.yml) | GitHub Release 发布时触发：校验标签来自 main、版本号匹配，构建包并发布到 PyPI。 | — |
| [ruff.yml](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/.github/workflows/ruff.yml) | 在 PR 和 main 推送时运行 Ruff 的指定严重错误规则；不是完整测试流水线。 | — |

## examples

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/examples/__init__.py) | 该目录的 Python 包标记/模块说明，没有独立业务执行逻辑。 | — |
| [modes_quick_demo.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/examples/modes_quick_demo.py) | 真实 LLM 的 A→B→C 示例，演示 single、batch、ray、venv 模式以及变体、文件、回退、继续和分支。 | `DemoState` |
| [ray_file_cleanup_demo.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/examples/ray_file_cleanup_demo.py) | 演示 Ray 批量执行、文件版本关联、回退与多余文件清理；属于本地被忽略的示例。 | `create_file_creating_workflow`、`create_demo_workflow` |

## examples/quick_start

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [INSTALL_AND_TEST.md](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/examples/quick_start/INSTALL_AND_TEST.md) | 从构建 wheel 到新环境安装和自检的操作说明；含旧版本号和外部服务目录假设，执行前应对照当前配置。 | — |
| [README.md](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/examples/quick_start/README.md) | 快速安装和冒烟测试说明；部分示例输出与“仅内存”描述较旧，应以自检脚本实际行为为准。 | — |
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/examples/quick_start/__init__.py) | 该目录的 Python 包标记/模块说明，没有独立业务执行逻辑。 | — |
| [install_checker.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/examples/quick_start/install_checker.py) | 快速开始目录内的安装自检版本，验证核心 SDK 并按条件检查 Ray/gRPC；与根目录脚本不是同一文件。 | `record`、`check_import` |

## examples/wtb_presentation

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [README.md](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/examples/wtb_presentation/README.md) | 完整 RAG + SQL 演示的结构、配置和使用说明。 | — |
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/examples/wtb_presentation/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |
| [env.local.example](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/examples/wtb_presentation/env.local.example) | 完整演示使用的本地环境变量模板。 | — |

## examples/wtb_presentation/config

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/examples/wtb_presentation/config/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |
| [llm_config.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/examples/wtb_presentation/config/llm_config.py) | 演示模型/嵌入服务配置、客户端获取、文本生成和文档相关性评分辅助函数。 | `ModelVariant` |
| [project_config.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/examples/wtb_presentation/config/project_config.py) | 把图、变体、文件追踪、Ray、环境和工作区选项组装成 WorkflowProject。 | `create_demo_project`、`create_rag_project` |
| [ray_resources.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/examples/wtb_presentation/config/ray_resources.py) | 为各节点定义 Ray CPU/GPU/内存等资源配置。 | `get_resources_for_node`、`get_resources_for_variant` |
| [variants.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/examples/wtb_presentation/config/variants.py) | 定义并注册可替换节点实现和批量实验变体组合。 | `register_all_variants`、`get_variant_implementation` |
| [venv_specs.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/examples/wtb_presentation/config/venv_specs.py) | 定义每个节点/变体的 Python 版本与依赖规格。 | `get_env_for_node`、`get_env_for_variant` |

## examples/wtb_presentation/graphs

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/examples/wtb_presentation/graphs/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |
| [rag_nodes.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/examples/wtb_presentation/graphs/rag_nodes.py) | RAG 各步骤：加载文档、切块、文档嵌入、问题嵌入、检索、相关性评分和答案生成，以及变体/缓存辅助逻辑。 | `rag_load_docs`、`rag_chunk_split` |
| [sql_agent_node.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/examples/wtb_presentation/graphs/sql_agent_node.py) | 把列出表、读取结构、生成/校验/执行 SQL 的整段流程封装为一个节点；含示例数据库创建辅助函数。 | `sql_agent_node` |
| [state_schemas.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/examples/wtb_presentation/graphs/state_schemas.py) | 定义 RAG、SQL 和统一工作流的 TypedDict 状态、数据结构及初始化函数。 | `DocumentChunk`、`EmbeddingVector` |
| [unified_graph.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/examples/wtb_presentation/graphs/unified_graph.py) | 将 RAG 和 SQL 节点组装为带路由的 LangGraph 图，提供独立 RAG/SQL 图、变体图和 Mermaid 输出。 | `create_unified_graph`、`create_rag_only_graph` |

## examples/wtb_presentation/scripts

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [05_full_presentation.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/examples/wtb_presentation/scripts/05_full_presentation.py) | 按演示步骤讲解项目配置、执行、检查点、回退、分支、批量与环境能力的完整脚本。 | `print_header`、`print_step` |
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/examples/wtb_presentation/scripts/__init__.py) | 该目录的 Python 包标记/模块说明，没有独立业务执行逻辑。 | — |
| [clean_demo_data.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/examples/wtb_presentation/scripts/clean_demo_data.py) | 清理演示数据库、输出、嵌入缓存等生成数据，带按类别清理和预览选项；本次未执行。 | `get_files_to_clean`、`clean_directory` |
| [run_demo.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/examples/wtb_presentation/scripts/run_demo.py) | 选择运行基础执行、回退、分支、批量、暂停/继续等演示。 | `print_header`、`print_step` |

## examples/wtb_presentation/workspace/documents

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [investment_memo_techflow.md](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/examples/wtb_presentation/workspace/documents/investment_memo_techflow.md) | RAG 示例语料：TechFlow 投资备忘录。属于演示输入，不能当作已经核实的投资资料。 | — |
| [market_analysis_2026.md](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/examples/wtb_presentation/workspace/documents/market_analysis_2026.md) | RAG 示例语料：企业 AI 平台市场分析，用来演示检索问答。 | — |
| [q4_2025_financial_report.md](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/examples/wtb_presentation/workspace/documents/q4_2025_financial_report.md) | RAG 示例语料：TechFlow 2025 年第四季度财报文本，用来演示检索问答。 | — |

## examples/wtb_presentation/workspace/sql

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [demo.db](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/examples/wtb_presentation/workspace/sql/demo.db) | SQL Agent 的 SQLite 示例数据库；只读确认包含 customers、products、orders、workflow_executions 表。 | — |

## scripts

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [verify_wtb_uv_e2e.ps1](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/scripts/verify_wtb_uv_e2e.ps1) | 启动/构建外部 uv_venv_manager 的 Docker Compose，等待 REST/gRPC 服务，执行严格集成测试和安装自检。 | — |

## tests

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/__init__.py) | 该目录的 Python 包标记/模块说明，没有独立业务执行逻辑。 | — |
| [run_integration_tests.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/run_integration_tests.py) | 准备测试服务/数据库并组织集成测试的脚本。 | `IntegrationTestEnvironment` |

## tests/helpers

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/helpers/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |
| [assertions.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/helpers/assertions.py) | 带业务上下文的断言辅助函数，让测试失败信息更清楚。 | `AssertionError` |
| [sync.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/helpers/sync.py) | 并发/异步测试的同步和轮询辅助函数。 | `TimeoutError` |

## tests/integration

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/integration/__init__.py) | 该目录的 Python 包标记/模块说明，没有独立业务执行逻辑。 | — |
| [test_acid_outbox.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/integration/test_acid_outbox.py) | 验证执行操作与 Outbox 事务一致性。 | `test_create_and_run_emits_created_started_completed`、`test_outbox_event_for_failed_run` |
| [test_cache_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/integration/test_cache_integration.py) | 验证缓存与执行存储接入。 | `test_real_ray_actors_get_isolated_cache_dbs` |
| [test_cas_file_tracking.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/integration/test_cas_file_tracking.py) | 验证SQLite/CAS 文件提交、查询和恢复。 | `test_track_files_creates_commit`、`test_track_files_stores_blobs` |
| [test_checkpoint_cas_linking_regression.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/integration/test_checkpoint_cas_linking_regression.py) | 验证各个检查点正确关联其文件版本。 | `test_langgraph_checkpoint_history_links_each_output_checkpoint_to_cas`、`test_rollback_resume_and_fork_resume_use_checkpoint_file_links` |
| [test_close_releases_sqlite.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/integration/test_close_releases_sqlite.py) | 验证关闭开发模式 bench 后释放 SQLite 文件。 | `test_close_releases_sqlite_after_run_fork_resume`、`test_closing_one_bench_keeps_a_shared_database_bench_usable` |
| [test_external_ray_client_file_flow.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/integration/test_external_ray_client_file_flow.py) | 验证独立 Ray Client 服务下的真实文件执行链路。 | `test_external_ray_client_grpc_real_file_control_flow` |
| [test_live_postgres_control_flow.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/integration/test_live_postgres_control_flow.py) | 验证显式启用的真实 PostgreSQL 执行与恢复链路。 | `test_live_postgres_factory_rollback_resume_fork`、`test_live_async_postgres_saver_lifecycle_and_fork` |
| [test_ray_batch.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/integration/test_ray_batch.py) | 验证Ray 批量运行集成。 | `test_ray_is_importable`、`test_runner_reports_available` |
| [test_ray_batch_rollback.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/integration/test_ray_batch_rollback.py) | 验证Ray 批量结果回退和分支协调。 | `test_batch_result_contains_file_commit_id`、`test_batch_result_handles_missing_fields` |
| [test_ray_graphless_durability.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/integration/test_ray_graphless_durability.py) | 验证真实 Ray 下非 LangGraph 节点路径的检查点持久化。 | `test_graphless_batch_history_and_rollback_survive_actor_return` |
| [test_real_file_control_flow_modes.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/integration/test_real_file_control_flow_modes.py) | 验证多种执行模式下的文件回退、分支和继续。 | `test_single_mode_real_file_rollback_resume_fork`、`test_batch_mode_real_file_rollback_resume_fork` |
| [test_sequential_execution.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/integration/test_sequential_execution.py) | 验证SDK 到控制器、LangGraph 适配器的顺序执行。 | `test_run_minimal_graph_completes`、`test_run_conditional_graph_route_b` |
| [test_sequential_langgraph.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/integration/test_sequential_langgraph.py) | 验证控制器到 LangGraph saver 的顺序执行。 | `test_run_completes`、`test_run_conditional_routing_b` |
| [test_sequential_node_executor.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/integration/test_sequential_node_executor.py) | 验证不提供 LangGraph 图时的普通节点执行。 | `test_run_simple_workflow`、`test_node_executor_with_inmemory_adapter` |
| [test_system_refactor_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/integration/test_system_refactor_integration.py) | 验证系统重构后的多条运行路径集成回归。 | `test_run_completes_successfully`、`test_run_get_checkpoints_rollback_fork` |
| [test_uv_venv_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/integration/test_uv_venv_integration.py) | 验证UV 服务、环境缓存和工作区组合。 | `test_same_spec_produces_same_hash`、`test_different_packages_produce_different_hash` |

## tests/mocks

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/mocks/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |
| [domain_objects.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/mocks/domain_objects.py) | 创建测试用真实领域对象的工厂函数；文件位于 mocks 目录但对象不一定是假实现。 | `create_test_outbox_event`、`create_test_checkpoint` |
| [repositories.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/mocks/repositories.py) | 实现领域接口的模拟仓库。 | `MockOutboxRepository`、`MockCheckpoint` |
| [services.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/mocks/services.py) | 模拟 Actor 池、外部服务等依赖。 | `MockActor`、`MockVenv` |

## tests/test_api

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_api/__init__.py) | 该目录的 Python 包标记/模块说明，没有独立业务执行逻辑。 | — |
| [test_api_services_unit.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_api/test_api_services_unit.py) | 验证执行、审计、批量和工作流 API 服务。 | `test_pause_execution_creates_checkpoint`、`test_pause_execution_on_error_no_commit` |
| [test_api_transaction_consistency.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_api/test_api_transaction_consistency.py) | 验证API 操作、事件顺序、失败回滚和并发。 | `test_client`、`test_pause_creates_outbox_event` |
| [test_external_control.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_api/test_external_control.py) | 验证外部执行控制、状态修改、检查点和分支。 | `test_pause_request_validation`、`test_pause_request_reason_max_length` |
| [test_external_control_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_api/test_external_control_integration.py) | 验证外部控制接口与执行服务的组合。 | `test_client`、`test_list_executions_empty` |
| [test_rest_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_api/test_rest_integration.py) | 验证HTTP 路由与服务实现集成。 | `test_client`、`test_health_check` |
| [test_rest_models.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_api/test_rest_models.py) | 验证REST 请求响应模型和校验。 | `test_execution_status_enum_values`、`test_audit_event_type_enum_values` |
| [test_workflow_submission_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_api/test_workflow_submission_integration.py) | 验证通过 API 提交工作流到执行的链路。 | `test_create_minimal_project`、`test_create_project_with_full_config` |

## tests/test_architecture

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_architecture/__init__.py) | 该目录的 Python 包标记/模块说明，没有独立业务执行逻辑。 | — |
| [test_acid_compliance.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_architecture/test_acid_compliance.py) | 验证事务与隔离等架构约束。 | `test_blob_save_creates_file_and_db_record`、`test_blob_delete_removes_file_and_db_record` |
| [test_dry_compliance.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_architecture/test_dry_compliance.py) | 验证共享实现和减少重复逻辑的架构约束。 | `test_compute_blob_id_sha256`、`test_compute_blob_id_idempotent` |
| [test_node_boundary_consistency.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_architecture/test_node_boundary_consistency.py) | 验证节点边界领域模型、ORM 和仓库映射一致性。 | `test_domain_model_has_expected_fields`、`test_domain_model_no_deprecated_fields` |

## tests/test_examples

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_examples/__init__.py) | 该目录的 Python 包标记/模块说明，没有独立业务执行逻辑。 | — |
| [test_transaction_consistency.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_examples/test_transaction_consistency.py) | 验证所属模块的事务、一致性和失败处理。 | `test_verify_uses_data_field`、`test_buggy_filter_returns_nothing` |

## tests/test_file_processing

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_file_processing/__init__.py) | 该目录的 Python 包标记/模块说明，没有独立业务执行逻辑。 | — |
| [conftest.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_file_processing/conftest.py) | 本测试目录及其子目录共享的 pytest fixtures：准备测试对象、依赖和清理；实际使用真实服务或模拟依赖以夹具为准。 | `InMemoryBlobRepository`、`InMemoryFileCommitRepository` |
| [test_transaction_scenarios.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_file_processing/test_transaction_scenarios.py) | 验证文件事务的具体成功/失败场景。 | `test_scenario_a_idempotency`、`test_scenario_b_partial_commit_orphan` |

## tests/test_file_processing/integration

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_file_processing/integration/__init__.py) | 该目录的 Python 包标记/模块说明，没有独立业务执行逻辑。 | — |
| [test_async_shared_uow_atomicity.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_file_processing/integration/test_async_shared_uow_atomicity.py) | 验证异步控制器/文件服务共享事务与并发边界。 | `test_concurrent_arun_calls_isolate_shared_adapter_session_state`、`test_controllers_sharing_one_adapter_serialize_session_mutation` |
| [test_async_transaction_consistency.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_file_processing/integration/test_async_transaction_consistency.py) | 验证异步文件追踪幂等、顺序、部分提交和读取一致性。 | `test_checkpoint_link_mapper_normalizes_legacy_numeric_identifier`、`test_sqlite_string_checkpoint_migration_preserves_legacy_rows` |
| [test_file_audit.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_file_processing/integration/test_file_audit.py) | 验证文件操作与审计。 | `test_node_execution_recorded`、`test_file_tracking_recorded` |
| [test_file_event.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_file_processing/integration/test_file_event.py) | 验证文件操作事件发布和处理。 | `test_track_files_publishes_start_event`、`test_track_files_publishes_blob_events` |
| [test_file_event_audit.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_file_processing/integration/test_file_event_audit.py) | 验证文件操作、事件与审计完整链路。 | `test_track_files_full_pipeline`、`test_restore_full_pipeline` |
| [test_file_ray.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_file_processing/integration/test_file_ray.py) | 验证文件操作与模拟 Actor/Ray 场景。 | `test_single_actor_track`、`test_pool_distributes_tasks` |
| [test_file_tracking_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_file_processing/integration/test_file_tracking_integration.py) | 验证追踪服务、关联对象和仓库的组合。 | `test_track_real_files`、`test_track_and_link_workflow` |
| [test_file_venv.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_file_processing/integration/test_file_venv.py) | 验证文件处理与环境管理的组合。 | `test_track_files_in_environment`、`test_environment_linked_to_commit` |
| [test_uow_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_file_processing/integration/test_uow_integration.py) | 验证文件处理与共享事务单元。 | `test_uow_interface_compliance`、`test_file_tracking_workflow_acid` |

## tests/test_file_processing/unit

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_file_processing/unit/__init__.py) | 该目录的 Python 包标记/模块说明，没有独立业务执行逻辑。 | — |
| [test_async_filetracker_preflight.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_file_processing/unit/test_async_filetracker_preflight.py) | 验证异步文件恢复前的预检查。 | `test_atrack_files_preflights_missing_path_before_any_storage_write`、`test_atrack_files_preflights_non_file_paths_before_storage_write` |
| [test_basic_operations.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_file_processing/unit/test_basic_operations.py) | 验证所在模块的基础操作：图执行或文件版本模型。 | `test_create_memento_from_existing_file`、`test_memento_immutability` |
| [test_execution_control.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_file_processing/unit/test_execution_control.py) | 验证所在模块的检查点、回退、分支和执行控制。 | `test_checkpoint_created_on_node_execution`、`test_checkpoint_contains_file_commit` |
| [test_file_processing_package.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_file_processing/unit/test_file_processing_package.py) | 验证拆分后的文件领域包、ID 校验和实体规则。 | `test_valid_blob_id`、`test_blob_id_from_content` |
| [test_file_tracking_link.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_file_processing/unit/test_file_tracking_link.py) | 验证接口层 FileTrackingLink 与领域关联实体的区分。 | `test_create_file_tracking_link`、`test_file_tracking_link_is_frozen` |

## tests/test_langgraph

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_langgraph/__init__.py) | 该目录的 Python 包标记/模块说明，没有独立业务执行逻辑。 | — |
| [conftest.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_langgraph/conftest.py) | 本测试目录及其子目录共享的 pytest fixtures：准备测试对象、依赖和清理；实际使用真实服务或模拟依赖以夹具为准。 | `simple_graph_def`、`branching_graph_def` |
| [helpers.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_langgraph/helpers.py) | 该测试套件共享的状态定义、图工厂和辅助逻辑。 | `SimpleState`、`AdvancedState` |

## tests/test_langgraph/integration

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_langgraph/integration/__init__.py) | 该目录的 Python 包标记/模块说明，没有独立业务执行逻辑。 | — |
| [test_audit_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_langgraph/integration/test_audit_integration.py) | 验证LangGraph 与审计记录集成。 | `test_create_audit_trail`、`test_record_audit_entry` |
| [test_checkpointer_persistence.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_langgraph/integration/test_checkpointer_persistence.py) | 验证检查点实际落盘及后续读取。 | `test_state_adapter_recompiles_with_checkpointer`、`test_checkpoints_persisted_after_execution` |
| [test_event_audit_combined.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_langgraph/integration/test_event_audit_combined.py) | 验证LangGraph、事件与审计组合。 | `test_create_full_integration_components`、`test_components_interconnected` |
| [test_event_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_langgraph/integration/test_event_integration.py) | 验证LangGraph 执行事件与总线集成。 | `test_event_bus_receives_published_events`、`test_multiple_event_types_subscription` |
| [test_execution_control.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_langgraph/integration/test_execution_control.py) | 验证所在模块的检查点、回退、分支和执行控制。 | `test_rollback_to_previous_checkpoint`、`test_rollback_and_resume` |
| [test_file_processing_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_langgraph/integration/test_file_processing_integration.py) | 验证文件追踪与工作流、事务、事件的集成。 | `test_create_blob_id`、`test_create_commit_id` |
| [test_langgraph_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_langgraph/integration/test_langgraph_integration.py) | 验证LangGraph 与 WTB 端到端执行。 | `test_adapter_initialization`、`test_extended_capabilities` |
| [test_ray_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_langgraph/integration/test_ray_integration.py) | 验证LangGraph 与 Ray 分布式执行。 | `test_batch_test_started_event`、`test_batch_test_completed_event` |
| [test_transaction_consistency.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_langgraph/integration/test_transaction_consistency.py) | 验证所属模块的事务、一致性和失败处理。 | `test_execution_persists_checkpoints`、`test_rollback_restores_consistent_state` |
| [test_venv_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_langgraph/integration/test_venv_integration.py) | 验证LangGraph 节点与环境准备集成。 | `test_create_environment`、`test_get_environment_status` |

## tests/test_langgraph/unit

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_langgraph/unit/__init__.py) | 该目录的 Python 包标记/模块说明，没有独立业务执行逻辑。 | — |
| [test_async_checkpointer_lifecycle.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_langgraph/unit/test_async_checkpointer_lifecycle.py) | 验证异步持久化 saver 的生命周期。 | `test_async_sqlite_saver_is_entered_used_and_closed`、`test_async_postgres_never_compiles_with_missing_saver` |
| [test_basic_operations.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_langgraph/unit/test_basic_operations.py) | 验证所在模块的基础操作：图执行或文件版本模型。 | `test_create_empty_graph`、`test_add_single_node` |
| [test_checkpoint_operations.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_langgraph/unit/test_checkpoint_operations.py) | 验证LangGraph 检查点的创建、查询和恢复。 | `test_create_checkpoint_id`、`test_checkpoint_id_equality` |
| [test_checkpointer_wiring.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_langgraph/unit/test_checkpointer_wiring.py) | 验证执行路径正确接入 checkpointer。 | `test_recompiles_compiled_graph_with_checkpointer`、`test_rejects_compiled_graph_without_builder_or_checkpointer` |
| [test_langgraph_node_replacer.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_langgraph/unit/test_langgraph_node_replacer.py) | 验证LangGraph 节点变体与图结构修改。 | `test_variant_creation`、`test_variant_serialization` |
| [test_state_management.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_langgraph/unit/test_state_management.py) | 验证LangGraph 状态更新、合并规则和校验。 | `test_simple_state_structure`、`test_advanced_state_structure` |
| [test_sync_checkpointer_lifecycle.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_langgraph/unit/test_sync_checkpointer_lifecycle.py) | 验证同步持久化 saver 的上下文和关闭责任。 | `test_sync_postgres_enters_context_and_closes_exactly_once`、`test_sync_postgres_setup_failure_exits_context` |

## tests/test_outbox_transaction_consistency

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_outbox_transaction_consistency/__init__.py) | 该目录的 Python 包标记/模块说明，没有独立业务执行逻辑。 | — |
| [conftest.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_outbox_transaction_consistency/conftest.py) | 本测试目录及其子目录共享的 pytest fixtures：准备测试对象、依赖和清理；实际使用真实服务或模拟依赖以夹具为准。 | `temp_data_dir`、`wtb_db_url` |
| [helpers.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_outbox_transaction_consistency/helpers.py) | 该测试套件共享的状态定义、图工厂和辅助逻辑。 | `SimpleState`、`TransactionState` |
| [test_batch_parallel.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_outbox_transaction_consistency/test_batch_parallel.py) | 验证并行批量执行中的 Outbox 事务。 | `test_batch_test_start_creates_outbox_event`、`test_batch_test_variant_execution_events` |
| [test_branching_rollback.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_outbox_transaction_consistency/test_branching_rollback.py) | 验证分支和回退中的 Outbox 事务。 | `test_branch_from_checkpoint_creates_outbox_event`、`test_branch_inherits_parent_state` |
| [test_cross_system_acid.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_outbox_transaction_consistency/test_cross_system_acid.py) | 验证多个存储/组件组合的一致性契约。 | `test_complete_execution_all_systems`、`test_execution_with_file_tracking` |
| [test_outbox_lifecycle.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_outbox_transaction_consistency/test_outbox_lifecycle.py) | 验证Outbox 后台处理器启动、处理和停止。 | `test_processor_starts_and_stops`、`test_processor_processes_pending_events` |
| [test_pause_resume.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_outbox_transaction_consistency/test_pause_resume.py) | 验证暂停与继续中的 Outbox 事务。 | `test_execution_creates_checkpoints`、`test_pause_creates_checkpoint_state` |
| [test_real_services_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_outbox_transaction_consistency/test_real_services_integration.py) | 验证实际 SQLite 等服务参与的组合场景。 | `test_real_outbox_event_persistence`、`test_real_blob_persistence` |
| [test_update_operations.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_outbox_transaction_consistency/test_update_operations.py) | 验证节点和状态更新中的 Outbox 事务。 | `test_node_update_creates_version_checkpoint`、`test_node_update_preserves_execution_state` |

## tests/test_sdk

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_sdk/__init__.py) | 该目录的 Python 包标记/模块说明，没有独立业务执行逻辑。 | — |
| [conftest.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_sdk/conftest.py) | 本测试目录及其子目录共享的 pytest fixtures：准备测试对象、依赖和清理；实际使用真实服务或模拟依赖以夹具为准。 | `SimpleState`、`MockVenvSpec` |
| [test_sdk_batch_rollback.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_sdk/test_sdk_batch_rollback.py) | 验证SDK 对批量结果回退与分支的封装。 | `test_success_result_has_required_fields`、`test_error_result_contains_error_message` |
| [test_sdk_control_operations.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_sdk/test_sdk_control_operations.py) | 验证SDK 暂停、继续、回退及状态/节点控制。 | `test_run_with_breakpoint`、`test_pause_execution` |
| [test_sdk_file_tracking.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_sdk/test_sdk_file_tracking.py) | 验证SDK 文件配置、关联和恢复。 | `test_config_defaults`、`test_config_enabled_with_paths` |
| [test_sdk_full_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_sdk/test_sdk_full_integration.py) | 验证SDK、Ray、环境和文件等完整组合。 | `test_update_workflow_preserves_env_config`、`test_update_workflow_preserves_file_tracking` |
| [test_sdk_graph_resolution.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_sdk/test_sdk_graph_resolution.py) | 验证持久化执行重新解析图，缺失配置时拒绝错误运行。 | `test_run_persists_graph_identity_in_metadata_without_polluting_user_state`、`test_resolver_prefers_persisted_metadata_graph_identity` |
| [test_sdk_langgraph_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_sdk/test_sdk_langgraph_integration.py) | 验证SDK 的 LangGraph 状态和检查点接入。 | `test_run_simple_workflow`、`test_run_with_initial_state` |
| [test_sdk_lifecycle.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_sdk/test_sdk_lifecycle.py) | 验证SDK 自建/借入资源的生命周期。 | `test_close_does_not_release_borrowed_dependencies`、`test_close_releases_owned_resources_once_and_deduplicates_aliases` |
| [test_sdk_ray_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_sdk/test_sdk_ray_integration.py) | 验证SDK 的 Ray 批量执行接入。 | `test_ray_config_defaults`、`test_ray_config_for_testing` |
| [test_sdk_venv_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_sdk/test_sdk_venv_integration.py) | 验证SDK 环境规格及隔离配置接入。 | `test_envspec_defaults`、`test_envspec_with_dependencies` |
| [test_transaction_consistency.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_sdk/test_transaction_consistency.py) | 验证所属模块的事务、一致性和失败处理。 | `test_successful_execution_is_atomic`、`test_failed_execution_rolls_back` |

## tests/test_v16_architecture

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_v16_architecture/__init__.py) | 该目录的 Python 包标记/模块说明，没有独立业务执行逻辑。 | — |
| [test_batch_runner_parity.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_v16_architecture/test_batch_runner_parity.py) | 验证批量执行器与 SDK 执行语义一致性。 | `test_runner_executes_via_execution_controller`、`test_runner_creates_isolated_controllers_per_variant` |
| [test_code_quality_fixes.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_v16_architecture/test_code_quality_fixes.py) | 验证导出、仓库接口等既有代码修复回归。 | `test_branchresult_not_in_sdk_exports`、`test_sdk_all_does_not_contain_branchresult` |
| [test_controller_factory.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_v16_architecture/test_controller_factory.py) | 验证隔离执行控制器与事务工厂。 | `test_managed_controller_context_manager_commits_on_success`、`test_managed_controller_context_manager_rollbacks_on_exception` |
| [test_execution_controller_fork.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_v16_architecture/test_execution_controller_fork.py) | 验证分支创建新执行/会话及保留历史。 | `test_fork_creates_new_execution`、`test_fork_preserves_workflow_id` |
| [test_langgraph_adapter.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_v16_architecture/test_langgraph_adapter.py) | 验证LangGraph 状态适配器与字符串 ID 契约。 | `test_initialize_session_returns_string`、`test_save_checkpoint_returns_string` |
| [test_outbox_lifecycle.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_v16_architecture/test_outbox_lifecycle.py) | 验证Outbox 后台处理器启动、处理和停止。 | `test_lifecycle_status_values`、`test_health_status_to_dict` |
| [test_string_ids.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_v16_architecture/test_string_ids.py) | 验证重构后的字符串/UUID 标识约束。 | `test_execution_has_string_session_id`、`test_execution_has_string_checkpoint_id` |

## tests/test_workspace

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_workspace/__init__.py) | 该目录的 Python 包标记/模块说明，没有独立业务执行逻辑。 | — |
| [conftest.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_workspace/conftest.py) | 本测试目录及其子目录共享的 pytest fixtures：准备测试对象、依赖和清理；实际使用真实服务或模拟依赖以夹具为准。 | `workspace_temp_dir`、`module_temp_dir` |
| [test_filesystem_workspace.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_workspace/test_filesystem_workspace.py) | 验证工作区中的文件版本与恢复。 | `test_hard_link_same_partition`、`test_copy_fallback` |
| [test_full_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_workspace/test_full_integration.py) | 验证工作区与 Ray、LangGraph、环境、文件的完整组合。 | `test_pause_preserves_workspace_and_state`、`test_resume_continues_in_same_workspace` |
| [test_langgraph_workspace.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_workspace/test_langgraph_workspace.py) | 验证LangGraph 状态与隔离工作区。 | `test_checkpoint_captures_workspace_context`、`test_multiple_checkpoints_same_workspace` |
| [test_parallel_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_workspace/test_parallel_integration.py) | 验证多个任务并行时的工作区隔离。 | `test_concurrent_creation_unique_ids`、`test_concurrent_creation_with_source_files` |
| [test_real_ray_workspace.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_workspace/test_real_ray_workspace.py) | 验证实际 Ray Actor 工作区隔离。 | `test_workspace_serializable_for_ray`、`test_workspace_config_serializable_for_ray` |
| [test_venv_workspace.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_workspace/test_venv_workspace.py) | 验证虚拟环境与工作区隔离。 | `test_hash_consistent`、`test_hash_changes_with_python_version` |

## tests/test_wtb

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/__init__.py) | 该目录的 Python 包标记/模块说明，没有独立业务执行逻辑。 | — |
| [conftest.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/conftest.py) | 本测试目录及其子目录共享的 pytest fixtures：准备测试对象、依赖和清理；实际使用真实服务或模拟依赖以夹具为准。 | `MockExecutionRepository`、`MockWorkflowRepository` |
| [test_actor_lifecycle.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_actor_lifecycle.py) | 验证Actor 生命周期、暂停策略和资源管理。 | `test_default_resources`、`test_gpu_memory_estimate` |
| [test_architecture_consolidation.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_architecture_consolidation.py) | 验证架构整合后的导出、接口与实现关系。 | `test_checkpoint_file_link_is_primary_model`、`test_checkpoint_file_link_uses_rich_value_object` |
| [test_audit_repository.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_audit_repository.py) | 验证审计数据库仓库。 | `test_add_and_get_log`、`test_append_logs` |
| [test_audit_trail.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_audit_trail.py) | 验证审计记录与查询。 | `test_create_entry`、`test_entry_to_dict` |
| [test_batch_runner.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_batch_runner.py) | 验证本地批量执行器。 | `test_default_config`、`test_for_local_development` |
| [test_checkpoint_models.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_checkpoint_models.py) | 验证检查点和执行历史模型。 | `test_creation`、`test_string_representation` |
| [test_checkpoint_session_ownership.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_checkpoint_session_ownership.py) | 验证检查点所属会话的隔离约束。 | `test_checkpoint_access_requires_an_active_session`、`test_checkpoint_access_rejects_foreign_session` |
| [test_checkpoint_stores.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_checkpoint_stores.py) | 验证检查点存储接口及实现。 | `test_save_and_load`、`test_load_nonexistent` |
| [test_cross_db_consistency.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_cross_db_consistency.py) | 验证Outbox 与完整性检查的跨存储配合。 | `test_atomic_boundary_and_outbox_creation`、`test_file_commit_link_with_outbox` |
| [test_domain_models.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_domain_models.py) | 验证节点、工作流、执行和批量等领域模型。 | `test_create_basic_node`、`test_create_node_with_tool` |
| [test_e2e_file_tracking_basic.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_e2e_file_tracking_basic.py) | 验证SDK 基础文件追踪链路。 | `test_file_tracking_basic` |
| [test_e2e_file_tracking_rollback_full.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_e2e_file_tracking_rollback_full.py) | 验证文件追踪到回退的完整场景。 | `test_file_tracking_rollback_full_scenario` |
| [test_e2e_file_tracking_rollback_restore.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_e2e_file_tracking_rollback_restore.py) | 验证回退时实际文件恢复。 | `test_file_tracking_rollback_restore_direct` |
| [test_e2e_file_tracking_rollback_state.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_e2e_file_tracking_rollback_state.py) | 验证回退后的状态与文件信息。 | `test_file_tracking_rollback_state_inspection` |
| [test_environment_events.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_environment_events.py) | 验证虚拟环境相关事件。 | `test_env_id_without_version`、`test_env_id_with_version` |
| [test_environment_provider_lifecycle_regression.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_environment_provider_lifecycle_regression.py) | 验证gRPC 环境创建异常、远端身份保留及删除重试等生命周期回归。 | `test_create_response_loss_retains_remote_identity_for_cleanup`、`test_invalid_create_response_is_rejected_but_remains_cleanup_safe` |
| [test_environment_providers.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_environment_providers.py) | 验证进程内、Ray 与 gRPC 环境提供者。 | `test_create_environment_returns_inprocess_type`、`test_create_environment_stores_reference` |
| [test_event_bus.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_event_bus.py) | 验证事件总线发布、历史和线程安全。 | `test_create_event_bus`、`test_create_event_bus_with_custom_history` |
| [test_execution_controller.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_execution_controller.py) | 验证执行控制器路由、状态转换、恢复、回退和异常路径。 | `test_create_execution`、`test_create_execution_with_initial_state` |
| [test_execution_controller_resume_regression.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_execution_controller_resume_regression.py) | 验证resume 修改状态能否进入后续执行。 | `test_resume_keeps_modified_state_in_node_executor_result`、`test_resume_applies_modified_state_to_graph_checkpoint` |
| [test_execution_controller_session_isolation.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_execution_controller_session_isolation.py) | 验证会话激活失败时拒绝混用其他执行的状态。 | `test_checkpoint_history_missing_session_does_not_return_previous_execution`、`test_sdk_batch_checkpoint_failure_is_not_hidden_by_fallback` |
| [test_execution_controller_sessionless_isolation.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_execution_controller_sessionless_isolation.py) | 验证缺少会话 ID 时拒绝借用残留会话。 | `test_run_without_session_id_fails_before_adapter_execution`、`test_pause_without_session_id_does_not_write_into_current_session` |
| [test_factories.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_factories.py) | 验证不同运行模式的配置、工厂及事务创建。 | `test_default_config`、`test_for_testing` |
| [test_file_processing_domain.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_file_processing_domain.py) | 验证文件 blob/提交 ID、快照和检查点关联模型。 | `test_create_valid_blob_id`、`test_blob_id_invalid_length` |
| [test_file_processing_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_file_processing_integration.py) | 验证文件追踪与工作流、事务、事件的集成。 | `test_atomic_commit_save`、`test_rollback_on_error` |
| [test_file_processing_repository.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_file_processing_repository.py) | 验证文件内容、提交和关联仓库。 | `test_save_and_get_blob`、`test_save_duplicate_content` |
| [test_file_tracking.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_file_tracking.py) | 验证文件追踪接口、结果对象及模拟服务。 | `test_create_tracked_file`、`test_tracked_file_immutable` |
| [test_grpc_environment_provider_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_grpc_environment_provider_integration.py) | 验证环境提供者与外部 UV gRPC 服务连接和环境准备。 | `test_init_creates_channel`、`test_close_cleans_up_channel` |
| [test_inmemory_uow.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_inmemory_uow.py) | 验证内存事务对象包含的仓库与行为。 | `test_has_blobs_repository`、`test_has_file_commits_repository` |
| [test_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_integration.py) | 验证多个执行组件组合运行。 | `test_complete_workflow_execution`、`test_breakpoint_and_resume` |
| [test_integrity.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_integrity.py) | 验证完整性问题、报告与检查器。 | `test_create_issue`、`test_dangling_reference_factory` |
| [test_langgraph_audit_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_langgraph_audit_integration.py) | 验证LangGraph 事件、审计及指标的组合。 | `test_langgraph_event_config_for_testing`、`test_langgraph_event_config_for_development` |
| [test_langgraph_event_bridge.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_langgraph_event_bridge.py) | 验证LangGraph 事件转换与传递。 | `test_for_testing_minimal_modes`、`test_for_development_detailed_modes` |
| [test_langgraph_time_travel.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_langgraph_time_travel.py) | 验证历史恢复和分支时不混入未来执行状态。 | `test_time_travel_no_future_leak_with_run_id`、`test_branching_no_leak` |
| [test_migration_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_migration_integration.py) | 验证旧检查点文件表到统一关联表的迁移。 | `test_add_and_get_by_checkpoint`、`test_get_by_commit` |
| [test_node_replacer.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_node_replacer.py) | 验证节点变体管理和替换。 | `test_register_variant`、`test_register_variant_without_node_id_fails` |
| [test_outbox.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_outbox.py) | 验证Outbox 模型、仓库及处理器。 | `test_create_default_event`、`test_create_checkpoint_verify_event` |
| [test_outbox_decorator.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_outbox_decorator.py) | 验证执行更新和 Outbox 事件写入的协调提交。 | `test_sets_deferred_commit_on_inner`、`test_atomic_mode_rejects_incomplete_callbacks_without_mutating_inner` |
| [test_outbox_filetracker.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_outbox_filetracker.py) | 验证Outbox 对文件提交、blob 和引用的核验。 | `test_verify_existing_commit_succeeds`、`test_verify_nonexistent_commit_raises` |
| [test_parity_checker.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_parity_checker.py) | 验证本地执行与 Ray 结果比较逻辑。 | `test_default_config`、`test_strict_config` |
| [test_ray_batch_runner.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_ray_batch_runner.py) | 验证Ray 配置、Actor 结果与批量执行行为。 | `test_default_config`、`test_for_local_development` |
| [test_ray_e2e_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_ray_e2e_integration.py) | 验证Ray、工作区和生命周期的端到端组合。 | `test_ray_is_initialized`、`test_ray_resources_available` |
| [test_ray_event_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_ray_event_integration.py) | 验证Ray 事件序列化、桥接和审计。 | `test_ray_batch_test_started_event_creation`、`test_ray_variant_execution_completed_event` |
| [test_ray_filetracker_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_ray_filetracker_integration.py) | 验证Ray Actor 内文件追踪服务集成。 | `test_disabled_service_returns_disabled_result`、`test_disabled_service_is_not_available` |
| [test_ray_transaction_consistency.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_ray_transaction_consistency.py) | 验证Ray 事件和状态更新的一致性。 | `test_outbox_event_created_in_same_transaction`、`test_batch_events_committed_atomically` |
| [test_ray_workspace_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_ray_workspace_integration.py) | 验证Ray 批量任务的工作目录隔离。 | `test_workspace_config_to_dict`、`test_workspace_data_round_trip` |
| [test_real_integration_consistency.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_real_integration_consistency.py) | 验证SQLite 元数据、LangGraph 检查点和恢复链路。 | `test_data_dir`、`test_checkpoint_tables_created` |
| [test_rollback_with_llm.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_rollback_with_llm.py) | 验证真实模型调用下的回退和对话状态。 | `test_database_setup`、`test_conversation_memory_after_rollback` |
| [test_safe_eval.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_safe_eval.py) | 验证受限条件表达式求值，替代直接 eval。 | `test_simple_true`、`test_simple_false` |
| [test_sqlite_foreign_keys.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_sqlite_foreign_keys.py) | 验证SQLite 新连接的外键约束及迁移处理。 | `test_sync_sqlite_engines_enable_foreign_keys_on_every_new_connection`、`test_async_sqlite_engine_enables_foreign_keys_on_new_connections` |
| [test_sqlite_recovery_reconciliation.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_sqlite_recovery_reconciliation.py) | 验证普通节点执行崩溃恢复、节点认领及防止重复执行。 | `test_completed_nodes_are_not_replayed_after_execution_persist_failure`、`test_persisted_running_continues_from_completed_head` |
| [test_state_adapter.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_state_adapter.py) | 验证内存/SQLite 状态适配及节点边界更新约束。 | `test_sqlite_completed_boundary_cannot_be_overwritten_by_stale_failure`、`test_sqlite_failed_boundary_cannot_be_overwritten_by_stale_completion` |
| [test_state_transitions.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_state_transitions.py) | 验证执行、批次和节点状态转换限制。 | `test_complete_from_running`、`test_complete_from_pending_raises` |
| [test_venv_cache.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_venv_cache.py) | 验证环境缓存、哈希、LRU 和过期淘汰。 | `test_compute_hash_deterministic`、`test_compute_hash_different_packages` |
| [test_workspace_integration.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_workspace_integration.py) | 验证工作区并发、事件及分支组合。 | `test_parallel_variants_isolated_file_writes`、`test_parallel_variants_share_input_efficiently` |
| [test_workspace_manager.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/test_wtb/test_workspace_manager.py) | 验证工作区创建、链接、复制和清理。 | `test_workspace_creation`、`test_derived_paths` |

## tests/unit

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/unit/__init__.py) | 该目录的 Python 包标记/模块说明，没有独立业务执行逻辑。 | — |
| [test_execution_controller.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/unit/test_execution_controller.py) | 验证执行控制器路由、状态转换、恢复、回退和异常路径。 | `test_create_execution_writes_explicit_checkpoint_backend`、`test_create_execution_uses_requested_id_for_session_and_persistence` |
| [test_fail_closed_contracts.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/unit/test_fail_closed_contracts.py) | 验证资源关闭、分支与历史读取失败时拒绝继续的契约。 | `test_bench_close_propagates_all_owned_failures_and_remains_retryable`、`test_fork_setup_failure_restores_source_without_cleanup_commit` |
| [test_fork_variant_metadata.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/unit/test_fork_variant_metadata.py) | 验证分支显式变体配置覆盖和元数据复制。 | `test_fork_variant_overlay_replaces_graph_metadata_with_deep_copies`、`test_fork_without_variant_overlay_inherits_source_graph_metadata` |
| [test_outbox_decorator.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/unit/test_outbox_decorator.py) | 验证执行更新和 Outbox 事件写入的协调提交。 | `test_inner_mutation_error_rolls_back_once`、`test_inner_error_is_preserved_when_rollback_also_fails` |
| [test_sqlite_lifecycle_contract.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/unit/test_sqlite_lifecycle_contract.py) | 验证数据库工厂、驱动选择和 SQLite 文件生命周期。 | `test_plain_postgres_url_selects_declared_psycopg3_driver`、`test_legacy_postgres_url_selects_declared_psycopg3_driver` |
| [test_system_refactor_unit.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/unit/test_system_refactor_unit.py) | 验证会话激活、分支初始化、线程安全、环境哈希和 CAS 等重构回归。 | `test_run_with_langgraph_sets_session_before_execute`、`test_run_with_langgraph_no_session_fails_closed` |

## tests/unit/application

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/unit/application/__init__.py) | 该目录的 Python 包标记/模块说明，没有独立业务执行逻辑。 | — |
| [test_batch_coordinator_adapter_resolution.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/unit/application/test_batch_coordinator_adapter_resolution.py) | 验证按执行选择适配器，缺失/未知后端时拒绝错误降级。 | `test_missing_or_unknown_explicit_backend_never_uses_shared_adapter`、`test_node_backend_constructor_failure_never_uses_shared_adapter` |
| [test_batch_coordinator_lifecycle.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/unit/application/test_batch_coordinator_lifecycle.py) | 验证批量协调器对借入/自建资源的关闭责任。 | `test_coordinator_borrows_injected_resources_by_default`、`test_coordinator_closes_owned_resources_once_and_deduplicates_aliases` |
| [test_batch_execution_coordinator.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/unit/application/test_batch_execution_coordinator.py) | 验证批量回退、分支、继续的协调行为。 | `test_rollback_restores_state_and_emits_events`、`test_rollback_emits_file_restore_outbox_event` |
| [test_external_storage.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/unit/application/test_external_storage.py) | 验证Actor 隔离存储路径、元数据恢复和运行环境传递。 | `test_resolve_actor_local_storage_paths_is_actor_scoped`、`test_resolve_execution_storage_paths_rehydrates_from_metadata` |
| [test_workflow_conversion_service.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/tests/unit/application/test_workflow_conversion_service.py) | 验证SDK 项目转换后保留项目版本。 | `test_workflow_conversion_persists_sdk_project_version` |

## wtb

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/__init__.py) | 顶层包入口：版本信息，以及 SDK/API 的延迟访问辅助逻辑。 | — |
| [config.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/config.py) | WTB 内部全局配置，包括测试/开发/生产模式、Ray、事件和文件追踪；与 SDK 的项目配置层次不同。 | `LangGraphEventConfig`、`RayConfig` |
| [py.typed](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/py.typed) | 标记此包提供类型信息，供类型检查器识别。 | — |

## wtb/api

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/api/__init__.py) | 包入口和 create_app/get_app 延迟导出，减少导入时加载可选 API 依赖。 | `create_app`、`get_app` |

## wtb/api/grpc

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/api/grpc/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |
| [servicer.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/api/grpc/servicer.py) | 把 gRPC 请求转换为应用服务调用并转换响应，提供服务实现/启动辅助逻辑。 | `WTBServicer` |

## wtb/api/grpc/protos

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [wtb_service.proto](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/api/grpc/protos/wtb_service.proto) | WTB 的 gRPC 服务与消息协议定义；不要与 UV 环境服务的协议混淆。 | — |

## wtb/api/rest

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/api/rest/__init__.py) | 包入口和 create_app/get_app 延迟导出，减少导入时加载可选 API 依赖。 | `create_app`、`get_app` |
| [app.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/api/rest/app.py) | 创建 FastAPI 应用，注册路由、WebSocket、中间件、异常处理和可选监控。 | `create_app`、`get_app` |
| [dependencies.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/api/rest/dependencies.py) | API 依赖注入与应用状态，给路由提供应用服务，并保留部分兼容适配。 | `AppState`、`LegacyExecutionService` |
| [models.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/api/rest/models.py) | HTTP 请求/响应的 Pydantic 数据结构及校验。 | `ExecutionStatusEnum`、`AuditEventTypeEnum` |

## wtb/api/rest/routes

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/api/rest/routes/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |
| [audit.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/api/rest/routes/audit.py) | 审计查询、统计与执行时间线路由。 | `list_audit_events`、`get_audit_summary` |
| [batch_tests.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/api/rest/routes/batch_tests.py) | 批量实验创建、查询、停止、进度和结果路由。 | `list_batch_tests`、`create_batch_test` |
| [executions.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/api/rest/routes/executions.py) | 执行查询、暂停/继续/停止、检查点、回退/分支、状态和节点控制路由。 | `list_executions`、`get_execution` |
| [health.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/api/rest/routes/health.py) | 健康、就绪和存活检查路由。 | `health_check`、`readiness_check` |
| [workflows.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/api/rest/routes/workflows.py) | 工作流定义、节点、变体等 HTTP 路由。 | `list_workflows`、`create_workflow` |

## wtb/api/websocket

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/api/websocket/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |
| [handlers.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/api/websocket/handlers.py) | WebSocket 连接、主题订阅、广播、心跳及事件总线接入。 | `ConnectionManager` |

## wtb/application

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/application/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |
| [factories.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/application/factories.py) | 组装项目服务、执行控制器、状态适配器、批量执行器及其资源；按模式选择实现，并管理隔离控制器的生命周期。 | `ManagedController`、`ExecutionControllerFactory` |
| [validators.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/application/validators.py) | 校验 ID、节点名、分页、状态修改等输入的函数。 | `ValidationError` |

## wtb/application/services

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/application/services/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |
| [actor_lifecycle.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/application/services/actor_lifecycle.py) | 管理 Ray Actor 创建、资源、暂停策略、恢复、回退和分支相关生命周期。 | `PauseStrategy`、`RollbackStrategy` |
| [api_services.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/application/services/api_services.py) | REST/gRPC 背后的应用服务实现：执行、审计、批量实验和工作流管理。 | `ExecutionAPIService`、`AuditAPIService` |
| [async_execution_controller.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/application/services/async_execution_controller.py) | 异步执行控制器及异步流输出。能力不与同步版本完全等价，应以具体方法和 README 的异步契约为准。 | `AsyncExecutionResult`、`AsyncStreamEvent` |
| [batch_execution_coordinator.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/application/services/batch_execution_coordinator.py) | 协调批量结果的回退、分支和恢复操作，解析该次执行对应的适配器和存储。 | `StateAdapterResolutionError`、`DefaultExecutionControllerFactory` |
| [batch_test_runner.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/application/services/batch_test_runner.py) | 本地 ThreadPool 批量执行器，为并发任务建立隔离的控制器和事务上下文，收集结果。 | `ThreadPoolBatchTestRunner` |
| [execution_controller.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/application/services/execution_controller.py) | 同步执行核心：创建执行、运行、暂停、继续、终止、断点、检查点回退、文件恢复和分支；含 LangGraph 与普通节点执行路径。 | `NodeBoundaryClaimConflict`、`DefaultNodeExecutor` |
| [external_storage.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/application/services/external_storage.py) | 解析 Actor 本地缓存/检查点数据库路径，以及从执行元数据重建这些路径。 | `ActorLocalStoragePaths` |
| [graph_loader.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/application/services/graph_loader.py) | 根据模块路径动态加载图工厂函数。 | `load_graph_factory` |
| [langgraph_node_replacer.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/application/services/langgraph_node_replacer.py) | 针对 LangGraph 图捕获节点/边结构、管理节点变体并构造替换后的图。 | `LangGraphNodeVariant`、`VariantSet` |
| [node_replacer.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/application/services/node_replacer.py) | 管理领域工作流的节点变体注册与替换。 | `NodeReplacer` |
| [outbox_controller_decorator.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/application/services/outbox_controller_decorator.py) | 包装执行控制器，将生命周期事件与业务更新放进共享事务的 Outbox，使用延迟提交协调提交时机。 | `OutboxExecutionControllerDecorator` |
| [parity_checker.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/application/services/parity_checker.py) | 比较线程池与 Ray 执行结果，记录两种执行方式的差异。 | `ParityDiscrepancyType`、`ParityDiscrepancy` |
| [project_service.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/application/services/project_service.py) | 项目注册、查询和变体管理，并把 SDK 项目对象转换为领域工作流。 | `ProjectService`、`VariantService` |
| [ray_batch_runner.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/application/services/ray_batch_runner.py) | Ray 批量执行器与 Actor 实现：分配变体任务、执行图、处理结果、存储引用及失败/重试等。 | `VariantExecutionResult`、`RayBatchTestRunner` |

## wtb/domain

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |

## wtb/domain/events

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/events/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |
| [checkpoint_events.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/events/checkpoint_events.py) | 检查点创建、回退请求/结果、节点边界、历史加载和分支等事件。 | `CheckpointEvent`、`CheckpointCreated` |
| [environment_events.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/events/environment_events.py) | 环境创建/删除、依赖修改、同步及失败等事件。 | `EnvironmentEvent`、`EnvironmentCreationStartedEvent` |
| [execution_events.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/events/execution_events.py) | 执行开始、暂停、恢复、完成、失败、取消等事件定义。 | `WTBEvent`、`ExecutionStartedEvent` |
| [file_processing_events.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/events/file_processing_events.py) | 文件提交、blob、检查点关联、恢复和清理等事件。 | `FileCommitCreatedEvent`、`FileCommitDeletedEvent` |
| [langgraph_events.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/events/langgraph_events.py) | 将 LangGraph 流式执行信息表示为 WTB 审计事件的类型和转换辅助函数。 | `LangGraphAuditEventType`、`LangGraphAuditEvent` |
| [node_events.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/events/node_events.py) | 节点开始、完成、失败、跳过等事件定义。 | `NodeEvent`、`NodeStartedEvent` |
| [ray_events.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/events/ray_events.py) | Ray 批次、Actor、变体执行、进度和失败相关事件定义。 | `RayEventType`、`RayEvent` |
| [workspace_events.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/events/workspace_events.py) | 工作区创建/激活/清理、文件快照/恢复、分支和环境复用等事件。 | `WorkspaceCreatedEvent`、`WorkspaceActivatedEvent` |

## wtb/domain/interfaces

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/interfaces/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |
| [_deprecated.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/interfaces/_deprecated.py) | 旧接口的兼容入口和弃用提示；当前调用关系应以实际 imports 为准。 | `get_deprecated_state_adapter` |
| [api_services.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/interfaces/api_services.py) | 规定 API 应用服务接口，以及请求处理需要的数据传输对象 DTO。 | `ExecutionDTO`、`ControlResultDTO` |
| [async_file_tracking.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/interfaces/async_file_tracking.py) | 异步文件追踪服务和结果接口。 | `FileTrackingResult`、`IAsyncFileTrackingService` |
| [async_repositories.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/interfaces/async_repositories.py) | 异步读取/写入及文件、Outbox 等数据仓库接口。 | `IAsyncReadRepository`、`IAsyncWriteRepository` |
| [async_state_adapter.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/interfaces/async_state_adapter.py) | 异步状态适配接口。 | `IAsyncStateAdapter` |
| [async_unit_of_work.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/interfaces/async_unit_of_work.py) | 异步事务边界接口。 | `IAsyncUnitOfWork` |
| [batch_coordinator.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/interfaces/batch_coordinator.py) | 规定批量回退/分支等操作请求、结果和协调器接口。 | `OperationType`、`BatchOperationRequest` |
| [batch_runner.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/interfaces/batch_runner.py) | 规定批量执行器、进度、错误类型和环境提供者接口。 | `BatchRunnerStatus`、`BatchRunnerProgress` |
| [checkpoint_store.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/interfaces/checkpoint_store.py) | 规定检查点保存、读取、列举等存储接口。 | `ICheckpointStore`、`ICheckpointStoreFactory` |
| [evaluator.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/interfaces/evaluator.py) | 规定评估器、评估器注册表和评估引擎接口；接口文件本身不代表已提供所有具体评估算法。 | `EvaluationMetric`、`EvaluationScore` |
| [execution_controller.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/interfaces/execution_controller.py) | 规定执行控制器应提供的操作；实际同步实现位于 application/services。 | `IExecutionController` |
| [file_processing_repository.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/interfaces/file_processing_repository.py) | 规定文件 blob、提交、检查点关联的存储接口及文件事务接口。 | `IBlobRepository`、`IFileCommitRepository` |
| [file_tracking.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/interfaces/file_tracking.py) | 规定文件追踪、恢复、清理服务及结果对象；FileTrackingLink 是接口层传输对象。 | `FileTrackingError`、`FileNotFoundError` |
| [node_executor.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/interfaces/node_executor.py) | 规定单个节点的执行接口、执行结果和执行器注册接口。 | `NodeExecutionResult`、`INodeExecutor` |
| [node_replacer.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/interfaces/node_replacer.py) | 规定节点变体注册及节点替换接口。 | `IVariantRegistry`、`INodeSwapper` |
| [repositories.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/interfaces/repositories.py) | 规定工作流、执行、变体、批次、评估、审计、节点边界及 Outbox 的数据访问接口。 | `IReadRepository`、`IWriteRepository` |
| [state_adapter.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/interfaces/state_adapter.py) | 规定执行会话、状态、检查点、节点边界等适配接口；会话对应 LangGraph thread_id。 | `CheckpointTrigger`、`CheckpointInfo` |
| [unit_of_work.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/interfaces/unit_of_work.py) | 规定跨多个数据仓库共享的事务边界：开始、提交、回滚和资源管理。 | `IUnitOfWork` |

## wtb/domain/models

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/models/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |
| [audit.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/models/audit.py) | 持久化审计条目的领域模型。 | `AuditEntry` |
| [batch_test.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/models/batch_test.py) | 批量实验、变体组合、单项结果、批量状态，以及分数/指标规范化。 | `BatchTestStatus`、`VariantCombination` |
| [checkpoint.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/models/checkpoint.py) | 检查点 ID、状态快照和 ExecutionHistory；定义历史查找、回退目标判断等领域规则。 | `CheckpointId`、`Checkpoint` |
| [evaluation.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/models/evaluation.py) | 评估指标值、评估结果和比较结果的数据模型。 | `MetricValue`、`EvaluationResult` |
| [integrity.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/models/integrity.py) | 完整性问题类别、严重程度、修复动作和检查报告。 | `IntegrityIssueType`、`IntegritySeverity` |
| [node_boundary.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/models/node_boundary.py) | 节点进入/退出时的检查点引用和节点状态；节点边界是检查点标记，不是另一份快照。 | `NodeStatus`、`NodeBoundary` |
| [outbox.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/models/outbox.py) | Outbox 待处理事件、事件类型、处理状态及相关行为。 | `OutboxEventType`、`OutboxStatus` |
| [workflow.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/models/workflow.py) | 最重要的业务模型：节点、边、工作流、Execution、ExecutionState、执行状态转换和 NodeVariant。 | `ExecutionStatus`、`WorkflowNode` |
| [workspace.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/models/workspace.py) | 工作区、隔离策略、文件链接方式、清理报告和环境规格哈希等模型。 | `WorkspaceStrategy`、`LinkMethod` |

## wtb/domain/models/file_processing

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/models/file_processing/__init__.py) | 集中导出文件版本领域模型，保持包级导入入口。 | — |
| [checkpoint_link.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/models/file_processing/checkpoint_link.py) | 将工作流检查点关联到文件提交的领域实体 CheckpointFileLink。 | `CheckpointFileLink` |
| [entities.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/models/file_processing/entities.py) | FileCommit、FileMemento、提交状态等文件版本实体和规则。 | `CommitStatus`、`FileMemento` |
| [exceptions.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/models/file_processing/exceptions.py) | 文件版本领域的异常类型，如非法 ID、重复文件、已完成提交。 | `FileProcessingError`、`DuplicateFileError` |
| [value_objects.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/domain/models/file_processing/value_objects.py) | BlobId、CommitId 等不可变 ID 类型和格式校验。 | `BlobId`、`CommitId` |

## wtb/infrastructure

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |

## wtb/infrastructure/adapters

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/adapters/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |
| [async_langgraph_state_adapter.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/adapters/async_langgraph_state_adapter.py) | 异步 LangGraph 适配器，管理异步 saver 和异步状态/执行操作。 | `AsyncLangGraphStateAdapter`、`AsyncLangGraphStateAdapterFactory` |
| [inmemory_state_adapter.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/adapters/inmemory_state_adapter.py) | 内存状态适配器，适合轻量测试；数据不跨进程重启保留。 | `InMemoryCheckpoint`、`InMemoryNodeBoundary` |
| [langgraph_state_adapter.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/adapters/langgraph_state_adapter.py) | 主要 LangGraph 适配器：编译/执行图，对接检查点存储、会话、状态、历史和分支，并管理 saver 资源。 | `CheckpointerType`、`LangGraphConfig` |
| [sqlite_state_adapter.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/adapters/sqlite_state_adapter.py) | 不依赖 LangGraph 图的普通节点执行路径所用持久化适配器，保存会话、检查点和节点边界。 | `SqliteStateAdapter` |

## wtb/infrastructure/database

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |
| [async_unit_of_work.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/async_unit_of_work.py) | SQLAlchemy 异步事务实现。 | `AsyncSQLAlchemyUnitOfWork` |
| [config.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/config.py) | 数据库 URL、路径、引擎/会话工厂等配置辅助逻辑，保留部分 AgentGit 兼容函数。 | `DatabaseConfig` |
| [engine_cache.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/engine_cache.py) | 按数据库地址缓存 SQLAlchemy Engine，并配置 SQLite 外键等连接行为。 | `normalize_db_url`、`configure_sqlite_foreign_keys` |
| [factory.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/factory.py) | 按配置创建相应 UnitOfWork 实现。 | `UnitOfWorkFactory` |
| [file_processing_orm.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/file_processing_orm.py) | 文件 blob、提交、文件快照和检查点关联的 SQLAlchemy 表映射。 | `FileBlobORM`、`FileCommitORM` |
| [inmemory_unit_of_work.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/inmemory_unit_of_work.py) | 内存事务和内存仓库实现，供测试及轻量使用。 | `InMemoryWorkflowRepository`、`InMemoryExecutionRepository` |
| [models.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/models.py) | 工作流、执行、变体、批量实验、评估、节点边界、Outbox 和审计的 SQLAlchemy 表映射。 | `JSONEncodedDict`、`WorkflowORM` |
| [setup.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/setup.py) | 数据库表结构初始化和会话获取辅助函数。 | `setup_wtb_database`、`setup_agentgit_database` |
| [unit_of_work.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/unit_of_work.py) | SQLAlchemy 同步事务实现，统一管理各仓库、提交、回滚和会话。 | `SQLAlchemyUnitOfWork` |

## wtb/infrastructure/database/async_repositories

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/async_repositories/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |
| [async_core_repositories.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/async_repositories/async_core_repositories.py) | 工作流、执行、变体、批次、评估、节点边界和审计的异步仓库。 | `AsyncWorkflowRepository`、`AsyncExecutionRepository` |
| [async_file_processing_repository.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/async_repositories/async_file_processing_repository.py) | blob、文件提交和检查点关联的异步仓库。 | `AsyncSQLAlchemyBlobRepository`、`AsyncSQLAlchemyFileCommitRepository` |
| [async_outbox_repository.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/async_repositories/async_outbox_repository.py) | Outbox 事件的异步仓库。 | `AsyncOutboxRepository` |
| [base.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/async_repositories/base.py) | 异步仓库的通用数据访问实现。 | `BaseAsyncRepository` |

## wtb/infrastructure/database/mappers

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/mappers/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |
| [blob_storage_core.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/mappers/blob_storage_core.py) | 同步/异步 blob 仓库共用的文件内容存储逻辑及文件提交/检查点关联映射。 | `BlobStorageCore`、`FileCommitMapper` |
| [outbox_mapper.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/mappers/outbox_mapper.py) | Outbox 领域对象与 ORM 对象之间的转换，供同步和异步仓库复用。 | `OutboxMapper` |

## wtb/infrastructure/database/migrations

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [002_batch_tests.sql](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/migrations/002_batch_tests.sql) | 批量实验结果表及索引相关迁移。 | — |
| [003_postgresql_production.sql](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/migrations/003_postgresql_production.sql) | PostgreSQL 专用表/索引等部署迁移。 | — |
| [004_consolidate_checkpoint_files.sql](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/migrations/004_consolidate_checkpoint_files.sql) | 把旧检查点文件关联表迁移并统一到 checkpoint_file_links。 | — |
| [005_node_boundary_cleanup.sql](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/migrations/005_node_boundary_cleanup.sql) | 调整节点边界表以匹配领域模型，处理旧字段及检查点 ID。 | — |
| [006_checkpoint_file_links_string_ids.sql](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/migrations/006_checkpoint_file_links_string_ids.sql) | SQLite 检查点文件关联 ID 转为文本，通过重建表保留字符串/UUID。 | — |
| [006_checkpoint_file_links_string_ids_postgresql.sql](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/migrations/006_checkpoint_file_links_string_ids_postgresql.sql) | 上述检查点 ID 字符串迁移的 PostgreSQL 版本。 | — |
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/migrations/__init__.py) | 该目录的 Python 包标记/模块说明，没有独立业务执行逻辑。 | — |

## wtb/infrastructure/database/repositories

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/repositories/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |
| [audit_repository.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/repositories/audit_repository.py) | 审计日志的数据访问实现。 | `SQLAlchemyAuditLogRepository` |
| [base.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/repositories/base.py) | 同步仓库的通用数据访问实现。 | `BaseRepository` |
| [batch_test_repository.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/repositories/batch_test_repository.py) | 批量实验的数据访问实现。 | `BatchTestRepository` |
| [evaluation_result_repository.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/repositories/evaluation_result_repository.py) | 评估结果的数据访问实现。 | `EvaluationResultRepository` |
| [execution_repository.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/repositories/execution_repository.py) | 执行记录的数据访问实现。 | `ExecutionRepository` |
| [file_processing_repository.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/repositories/file_processing_repository.py) | blob、文件提交、检查点文件关联的数据库/内存仓库实现。 | `SQLAlchemyBlobRepository`、`SQLAlchemyFileCommitRepository` |
| [node_boundary_repository.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/repositories/node_boundary_repository.py) | 节点边界的数据访问与映射。 | `NodeBoundaryRepository` |
| [node_variant_repository.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/repositories/node_variant_repository.py) | 节点变体的数据访问实现。 | `NodeVariantRepository` |
| [outbox_repository.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/repositories/outbox_repository.py) | Outbox 待处理事件的数据访问实现。 | `SQLAlchemyOutboxRepository` |
| [workflow_repository.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/database/repositories/workflow_repository.py) | 工作流定义的数据访问实现。 | `WorkflowRepository` |

## wtb/infrastructure/environment

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/environment/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |
| [providers.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/environment/providers.py) | 当前进程、Ray runtime_env 和 gRPC 环境提供者；UV 服务负责准备环境，Ray 负责在相应环境中执行。 | `InProcessEnvironmentProvider`、`RayRuntimeEnvConfig` |
| [venv_cache.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/environment/venv_cache.py) | 按环境规格哈希缓存虚拟环境，管理复用、LRU/过期淘汰和统计。 | `VenvSpec`、`VenvCacheEntry` |

## wtb/infrastructure/environment/uv_manager

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [README.md](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/environment/uv_manager/README.md) | 解释这里仅包含 UV Venv Manager 客户端，实际服务属于外部项目。 | — |
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/environment/uv_manager/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |

## wtb/infrastructure/environment/uv_manager/grpc_generated

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/environment/uv_manager/grpc_generated/__init__.py) | 该目录的 Python 包标记/模块说明，没有独立业务执行逻辑。 | — |
| [env_manager_pb2.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/environment/uv_manager/grpc_generated/env_manager_pb2.py) | Protobuf 自动生成的环境服务消息定义。 | — |
| [env_manager_pb2.pyi](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/environment/uv_manager/grpc_generated/env_manager_pb2.pyi) | 生成消息的 Python 类型声明，供 IDE 和类型检查器使用。 | — |
| [env_manager_pb2_grpc.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/environment/uv_manager/grpc_generated/env_manager_pb2_grpc.py) | 自动生成的环境服务 gRPC stub 与服务注册辅助代码；不是实际环境管理业务实现。 | — |

## wtb/infrastructure/events

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/events/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |
| [langgraph_event_bridge.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/events/langgraph_event_bridge.py) | 将 LangGraph 流式事件转换并发送到 WTB 事件系统。 | `NodeExecutionTracker`、`LangGraphEventBridge` |
| [metrics_event_listener.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/events/metrics_event_listener.py) | 监听执行事件收集指标，供 Prometheus 或内存统计使用。 | `MetricsEventListener`、`InMemoryMetricsCollector` |
| [ray_event_bridge.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/events/ray_event_bridge.py) | 将 Ray worker/批量执行事件序列化、转换并接入 WTB 事件和 Outbox 流程。 | `RayEventBridge` |
| [stream_mode_config.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/events/stream_mode_config.py) | 配置需要处理的 LangGraph 流模式和事件相关选项。 | `StreamMode`、`StreamModeConfig` |
| [wtb_audit_trail.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/events/wtb_audit_trail.py) | 收集、查询并记录执行相关审计信息，对接事件总线。 | `WTBAuditEventType`、`WTBAuditSeverity` |
| [wtb_event_bus.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/events/wtb_event_bus.py) | 发布/订阅事件总线，管理监听器和有界事件历史等。 | `WTBEventBus` |

## wtb/infrastructure/file_tracking

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/file_tracking/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |
| [async_filetracker_service.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/file_tracking/async_filetracker_service.py) | 异步文件追踪实现，对接异步仓库/事务进行提交、关联和恢复。 | `AsyncFileTrackerService` |
| [async_orphan_cleaner.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/file_tracking/async_orphan_cleaner.py) | 异步清理失去有效引用的文件数据。 | `AsyncBlobOrphanCleaner` |
| [cleanup_service.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/file_tracking/cleanup_service.py) | 识别并清理回退后的多余文件，提供备份和清理限制等处理。 | `FileCleanupService` |
| [filetracker_service.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/file_tracking/filetracker_service.py) | 外部 FileTracker 系统的适配实现；具体可用性取决于外部包和数据库配置。 | `FileTrackerService` |
| [mock_service.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/file_tracking/mock_service.py) | 内存模拟文件追踪服务，供测试记录和验证操作。 | `MockFileTrackingService` |
| [ray_filetracker_service.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/file_tracking/ray_filetracker_service.py) | Ray 可用的文件追踪包装层，传递可序列化配置并在 worker 内延迟创建服务。 | `RayFileTrackerService` |
| [sqlite_service.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/file_tracking/sqlite_service.py) | SQLite 保存文件元数据、本地目录保存 SHA-256 blob 的轻量文件追踪实现；支持检查点关联和恢复。 | `SqliteFileTrackingService` |

## wtb/infrastructure/integrity

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/integrity/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |
| [checker.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/integrity/checker.py) | 检查跨存储悬空引用、孤立数据和状态不一致等完整性问题。 | `ICheckpointRepository`、`IntegrityChecker` |

## wtb/infrastructure/llm

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/llm/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |
| [openai_langchain.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/llm/openai_langchain.py) | 复用模型客户端、LangChain 聊天/嵌入封装和 SQLite 文本生成缓存；延迟导入可选模型依赖。 | `LangChainOpenAIConfig`、`TextGenerationResult` |

## wtb/infrastructure/outbox

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/outbox/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |
| [lifecycle.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/outbox/lifecycle.py) | 启动、停止和监控 Outbox 后台处理器，管理应用退出时的资源。 | `LifecycleStatus`、`HealthStatus` |
| [processor.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/outbox/processor.py) | 后台处理 Outbox 事件，包括检查点/文件关联等核验、失败和重试处理；不是跨数据库的单一原子事务。 | `ICheckpointRepository`、`ICommitRepository` |

## wtb/infrastructure/stores

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/stores/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |
| [inmemory_checkpoint_store.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/stores/inmemory_checkpoint_store.py) | 内存检查点存储，提供保存/读取/列举等操作。 | `InMemoryCheckpointStore`、`InMemoryCheckpointStoreFactory` |
| [langgraph_checkpoint_store.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/stores/langgraph_checkpoint_store.py) | 在领域 Checkpoint 和 LangGraph StateSnapshot 之间转换，对接不同 saver 后端。 | `LangGraphCheckpointConfig`、`LangGraphCheckpointStore` |

## wtb/infrastructure/workspace

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/workspace/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |
| [manager.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/infrastructure/workspace/manager.py) | 创建、激活、分支和清理隔离工作目录，处理文件链接/复制及恢复。 | `WorkspaceManagerError`、`WorkspaceNotFoundError` |

## wtb/sdk

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/sdk/__init__.py) | 集中导出公开 SDK 类型，使使用者可以统一从 wtb.sdk 导入。 | — |
| [_example_graphs.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/sdk/_example_graphs.py) | 供安装检查和测试使用、可按模块路径导入的小型图工厂；便于 Ray worker 导入，避免依赖 __main__。 | `create_linear_graph` |
| [test_bench.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/sdk/test_bench.py) | 核心 SDK 门面 WTBTestBench：注册项目、run、pause、resume、rollback、fork、批量实验和资源关闭；把请求转交应用服务。 | `RollbackResult`、`ForkResult` |
| [workflow_project.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/sdk/workflow_project.py) | 用户提交的项目配置：WorkflowProject、ExecutionConfig、RayConfig、EnvironmentConfig、EnvSpec、FileTrackingConfig，以及图和变体构建。 | `EnvSpec`、`EnvironmentConfig` |

## wtb/testing

| 文件 | 作用 | 定位线索 |
|---|---|---|
| [__init__.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/testing/__init__.py) | 该目录的 Python 包入口，组织导出/兼容导入；具体导出见文件。 | — |
| [fixtures.py](E:/HKU_WORK/WTB-AgenticWorkflowTestBench/wtb/testing/fixtures.py) | 对使用者开放的测试夹具/辅助对象，用于创建测试适配器和服务环境；属于可安装包。 | `MinimalState`、`StateAdapterTestMixin` |

## 目录与覆盖核对

- 本次列出 401 个现有文件（不含本导读自身、.git 内部数据和 Python 缓存）。
- wtb 下 165 个文件；tests 下 190 个文件。
- .git/：本地 Git 对象、分支引用、索引、配置与钩子等版本控制元数据；不属于 WTB 业务源码。
- uv_venv_manager/：当前为空，没有可逐个解释的文件。
- PROJECT_FILE_GUIDE.zh-CN.md：本次生成的中文逐文件导读。
- 未改动业务源码，未执行数据库迁移或发布操作。
