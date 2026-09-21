# WTB 本地控制台

页面连接真实 WTB 服务，执行、检查点、文件版本、分叉、批量实验和审计都来自 SQLite 与 CAS。支持空数据库直接启动，执行图与状态来自注册项目。

## 安装与启动

仓库根目录创建 Python 3.11–3.13 环境并安装 API：

```sh
python -m venv .venv
# 激活环境后：
python -m pip install -e ".[api,langgraph-sqlite]"
python -m wtb.api.console --data-dir data/console --port 8000
```

在 `web` 目录安装并启动前端：

```sh
npm ci
npm run dev
```

默认打开 `http://127.0.0.1:5173`，开发与生产预览都代理 `/api` 和 `/ws` 到 `http://127.0.0.1:8000`。需要其他后端地址时设置 `WTB_API_URL`，例如 PowerShell：

```powershell
$env:WTB_API_URL = 'http://127.0.0.1:8001'
npm run dev
# 或先 npm run build，再 npm run preview
```

## 原生与 LangGraph 兼容模式

| 后端 | 默认项目 | Python 依赖 | 图协议 |
| --- | --- | --- | --- |
| 支持 `WorkflowProject.runtime_backend` 的 WTB | 原生 `file-workflow` | `.[api]`，不需要 LangGraph | 可调用 `nodes` 字典、`edges` 二元/三元组与 `entry_point` |
| 旧版 WTB | LangGraph `file-workflow` | `.[api,langgraph-sqlite]` | `StateGraph` 或带 builder 的编译图 |
| 显式 LangGraph 配置 | `langgraph-file-workflow` | `.[api,langgraph-sqlite]` | 保留 LangGraph 循环、并行汇合与 reducer 语义 |

原生执行由 WTB 的节点执行器、SQLite 状态适配器和恢复声明控制。控制台只增加事件观察与节点边界控制，不绕过检查点所有权、CAS 校验或重复恢复保护。可选 LangGraph 项目使用仓库支持的版本：

```sh
python -m pip install -e ".[api,langgraph-sqlite]"
python -m wtb.api.console --data-dir data/console-lg --config console-langgraph.json
```

默认项目 `prepare → transform → finish` 写入 `report.txt`，`transform` 可循环并替换为 `uppercase`；状态可用 `text`、`repeat`、`delay`、`fail`。LangGraph 配置另外提供两个并行节点汇合的 `parallel` 变体。原生顺序执行器不会假装支持并行 reducer。

## 使用

“新建执行”选择变体、初始 JSON、节点实现与节点前断点。暂停后可以编辑状态、断点或创建检查点，再继续运行。执行中可请求暂停或停止，操作在节点边界生效。

“执行与文件”提供检查点分页、实时/历史状态、执行分支、CAS 文件预览、版本对比与审计。失败执行先选择有效检查点回退，再修改状态继续。分叉拥有独立输出目录，保留父执行、来源检查点和历史尝试；回退也保留旧记录。

其他入口提供批量实验、节点实现库、变体对照、审计分页和文件完整性检查。四种结构视图使用真实节点记录、检查点顺序和跟踪文件数，支持鼠标与键盘（1–4 切换，Space 暂停/继续，F 分叉，R 回退）。离线时禁用执行控制，重新连接后刷新。

## 注册工作流

配置只加载服务端明确列出的可信工厂：

```json
{
  "projects": [
    {"factory": "my_package.workflows:create_project", "initial_state": {"query": "hello"}}
  ]
}
```

工厂返回 `wtb.sdk.WorkflowProject`，图遵循上表相应协议。`--config` 指定配置；不接收浏览器任意导入路径。每次执行保存图快照，恢复时使用该执行的快照。

运行数据位于 `data/console/console.db` 和 `workspaces/<execution-id>/`。本机单用户模式最多两个后台任务，使用服务进程 Python。Ray、独立 venv、外部工具副作用恢复、未配置的评估器与缓存/Outbox 指标不在这个本地控制台的执行范围内，界面明确展示能力边界。

## 可重复验收

```sh
python -m pip install pytest httpx
python -m pytest tests/test_api/test_console_real.py -q
cd web
npm test
npm run typecheck
npm run build
npx playwright install chromium
npm run test:e2e
```

`test:e2e` 自动启动新的本地后端和前端，创建独立数据目录，使用真实浏览器操作 UI 并读取真实 API/CAS，最后关闭自己创建的进程。结果 JSON、服务日志、导出状态和 1440/900/390 像素截图保存在 `output/playwright/e2e-<timestamp>/`。不修改已有控制台数据库、不 mock 执行响应。

可选环境变量：`WTB_E2E_PYTHON` 指定解释器，`WTB_E2E_CONFIG` 指定项目配置，`WTB_E2E_CHANNEL=chrome` 使用本机 Chrome，`WTB_E2E_PREVIEW=1` 验收已构建产物，`WTB_E2E_OUTPUT` 指定证据目录。解释器/配置相对路径从 `web` 目录解析。

后端回归覆盖恢复/分叉、文件损坏不移动状态、状态编辑、手动检查点、循环、并行、失败隔离、任务并发限制和服务重启；前端回归覆盖空状态、变体归属、真实节点状态和历史尝试投影。本仓库 CI 分别运行默认项目及显式 LangGraph 项目。上表的原生模式由支持该协议的后端提供。
