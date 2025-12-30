# 源代码文档 (src/)

本目录包含 LangGraph 节点环境管理系统的核心代码实现。

## 目录结构

```text
src/
├── api.py                  # FastAPI 应用入口与路由定义
├── config.py               # 配置加载与校验 (.env 处理)
├── exceptions.py           # 自定义异常类与 HTTP 异常映射
├── models.py               # Pydantic 数据模型 (请求/响应/校验)
├── services/               # 核心业务逻辑服务 (详见 services/README.md)
│   ├── dep_manager.py      # 依赖管理逻辑
│   ├── env_manager.py      # 环境生命周期逻辑
│   ├── lock_manager.py     # 节点并发控制锁
│   ├── project_info.py     # 主项目依赖分析映射
│   └── uv_executor.py      # UV CLI 指令底层执行
└── README.md               # 本文档
```

## 基础模块详述

### 1. `api.py` (接口层)
- **核心功能**: 系统的唯一对外部入口，负责路由请求。
- **关键逻辑**:
  - **应用工厂**: 使用 `create_app()` 动态创建 FastAPI 实例。
  - **依赖注入**: 使用 `Depends` 注入 `EnvManager` 等服务。
  - **路由组**:
    - `POST /envs`: 环境创建。
    - `GET /envs/{workflow_id}/{node_id}`: 状态查询。
    - `POST /envs/{workflow_id}/{node_id}/run`: **代码执行核心**。
    - `POST /envs/{workflow_id}/{node_id}/deps`: 动态扩展依赖。
    - **注意**: 采用 `/envs/{workflow_id}/{node_id}` 的层次结构，使 API 符合 RESTful 规范，并显式区分不同工作流下的节点环境。
  - **异常捕获**: 全局集成 `ServiceError` 捕获器，将业务异常自动转换为标准的 HTTP 响应。

### 2. `config.py` (配置层)
- **核心功能**: 统一管理系统环境变量和运行时参数。
- **关键特性**:
  - **Dotenv 集成**: 自动寻找并加载项目根目录下的 `.env` 文件。
  - **智能路径解析**: 
    - 针对 `DATA_ROOT` 等关键路径，会自动判断是相对路径还是绝对路径。
    - 若为相对路径，则相对于当前项目的根目录进行解析，解决了 CWD (当前工作目录) 变化导致的路径找不到问题。
  - **单例模式**: 通过 `get_settings()` 缓存配置实例，避免重复解析。

### 3. `models.py` (模型层)
- **核心功能**: 定义数据契约与自动化验证。
- **主要模型**:
  - `CreateEnvRequest`: 校验 `node_id` 格式，过滤空包名，支持可选的 `python_version`。
  - `RunRequest`: 校验执行代码 `code` 不能为空。
  - `EnvStatusResponse`: 结构化输出环境状态，包含已安装依赖和锁定版本。
  - `UVResult`: 封装底层命令执行结果，包含 `stdout`, `stderr` 和 `returncode`。

### 4. `exceptions.py` (异常层)
- **核心功能**: 实现业务逻辑与 HTTP 状态码的解耦。
- **异常设计**:
  - `ServiceError`: 所有自定义异常的基类。
  - `EnvNotFound` (404): 尝试操作不存在的节点环境时抛出。
  - `EnvLocked` (423): 当同一个节点环境正在执行安装或删除操作时，拦截冲突请求。
  - `UVExecutionError` (500): 底层 `uv` 命令执行非零返回时的包装。

---

## 核心服务 (Services)

系统的核心业务逻辑（如并发控制、UV 命令构建、依赖映射逻辑）位于 `services/` 目录下。

详细实现文档请参阅：**[services/README.md](services/README.md)**。
