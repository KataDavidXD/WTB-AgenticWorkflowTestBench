# 核心服务模块文档 (Services)

本目录包含系统的核心业务逻辑实现，负责环境管理、依赖操作、并发控制及底层命令执行。

## 模块列表

### 1. `env_manager.py` (环境管理器)
**职责**: 管理虚拟环境的完整生命周期。
- **核心类**: `EnvManager`
- **主要功能**:
  - `create_env(workflow_id, node_id, packages)`: 初始化新环境。
    - **特性**: 环境标识符由 `workflow_id` 和 `node_id` 组合而成，确保多租户/多工作流下的环境隔离。创建时会自动通过 `ProjectInfo` 获取主项目依赖版本进行约束。
  - `get_status(env_id)`: 检查环境状态（是否存在、是否包含 `pyproject.toml` 和 `uv.lock`）。
  - `cleanup()`: 自动清理长期（默认 72 小时）未使用的闲置环境，释放磁盘空间。
  - `run(env_id, code)`: 在指定环境中执行 Python 代码，支持超时控制。
  - `touch(env_id)`: 更新环境的“最后使用时间”元数据。

### 2. `dep_manager.py` (依赖管理器)
**职责**: 处理依赖包的增删改查操作。
- **核心类**: `DependencyManager`
- **主要功能**:
  - `add(env_id, packages)`: 添加依赖。
    - **特性**: 调用 `ProjectInfo` 自动应用主项目版本映射。
  - `remove(env_id, packages)`: 移除依赖。
  - `list_dependencies(env_id)`: 解析 `pyproject.toml` 和 `uv.lock`，返回当前声明的依赖及实际锁定的版本。

### 3. `uv_executor.py` (UV 执行器)
**职责**: 封装底层的 `uv` CLI 命令调用，屏蔽操作系统差异。
- **核心类**: `UVCommandExecutor`
- **主要功能**:
  - **跨平台支持**: 自动处理 Windows/Linux 下的路径分隔符和命令调用差异。
  - **环境隔离**:
    - 使用 `UV_CACHE_DIR` 环境变量指定独立的缓存目录。
    - 使用 `--no-workspace` 参数确保创建的节点环境是独立的，不会被识别为工作区成员。
  - **Windows 优化**: 在执行 `uv remove` 时使用 `--no-sync` 参数，避免在 Windows 下因文件锁导致的操作失败。

### 4. `project_info.py` (项目信息/依赖映射)
**职责**: 读取主项目配置，实现依赖版本继承。
- **核心类**: `ProjectInfo` (单例模式)
- **主要功能**:
  - **单例加载**: 系统启动时自动读取并缓存主项目的 `pyproject.toml` 依赖配置。
  - `get_dependency_mapping(packages)`: 核心映射逻辑。
    - 输入: `["fastapi", "numpy"]`
    - 输出: `["fastapi>=0.128.0", "numpy"]` (假设主项目约束了 fastapi)
    - 作用: 确保子环境安装的依赖版本与主项目兼容，防止版本冲突。

### 5. `lock_manager.py` (并发锁)
**职责**: 提供细粒度的并发控制。
- **核心类**: `LockManager`
- **主要功能**:
  - **节点级锁**: 为每个 `node_id` 维护一个独立的 `asyncio.Lock`。
  - **竞态防护**: 防止对同一个节点环境同时进行互斥操作（例如：不能在安装依赖的同时删除环境）。
