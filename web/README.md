# WTB 本地控制台

这套页面连接真实 WTB 本地服务：工作流、执行、检查点、文件版本、Fork、批量任务和审计记录都来自 SQLite 与 CAS，不使用浏览器模拟数据。

## 启动

在仓库根目录启动后端：

```powershell
.venv\Scripts\python.exe -m wtb.api.console --data-dir data/console
```

另开一个终端启动前端：

```powershell
cd web
npm install
npm run dev
```

打开 Vite 输出的地址，默认是 `http://127.0.0.1:5173`。开发代理将 `/api` 和 `/ws` 转发给本机 `127.0.0.1:8000`。

运行数据位于 `data/console/`：`console.db` 保存控制台读模型、`workspaces/<execution-id>/` 保存该执行的图快照、输出和 `.filetrack` CAS。删除这个目录会删除本地历史和文件版本。

## 默认验收项目

`file-workflow` 在 [console_project.py](../wtb/testing/console_project.py) 中注册，完全不依赖 LLM：

- `prepare → transform → finish` 写入 `report.txt`；`transform` 可循环。
- `transform:uppercase` 替换节点实现。
- `parallel` 是两个并行节点汇合的工作流变体。
- 初始状态可使用 `text`、`repeat`、`delay`、`fail`。`fail: true` 用于验证失败隔离。

启动后可在底部“断点”输入框设置 `transform`，执行会在该节点前进入暂停。选择检查点后可回退或 Fork；Fork 会分配新的 Workspace。四个 Studio 视图分别展示真实工作流路径、检查点/文件/分支、运行环境和 SDK 配置，以及变体矩阵；不保留浏览器演示状态。

## 注册自己的工作流

服务只加载明确列出的 Python 工厂，不接收浏览器上传或任意导入路径。创建 JSON 配置：

```json
{
  "projects": [
    {
      "factory": "my_package.workflows:create_project",
      "initial_state": {"query": "hello"}
    }
  ]
}
```

工厂必须返回 `wtb.sdk.WorkflowProject`，其 `graph_factory` 返回 LangGraph `StateGraph` 或可重编译的图。然后启动：

```powershell
.venv\Scripts\python.exe -m wtb.api.console --data-dir data/my-console --config .\console-projects.json
```

后端进程运行时，项目图会被冻结到每个 Execution 的 Workspace。之后即使修改项目代码，历史执行仍使用自己的图快照。

## 当前能力边界

当前是本机单用户模式：后台线程最多同时运行两个任务，使用服务进程 Python 与 SQLite/CAS。页面会明确显示 Ray、独立 venv、评估器、Outbox 和缓存指标未接入；不会用假 Actor、路径或分数替代它们。

## 验证

```powershell
.venv\Scripts\python.exe -m pytest tests/test_api/test_console_real.py -q
cd web
npm test
npm run typecheck
npm run build
```
