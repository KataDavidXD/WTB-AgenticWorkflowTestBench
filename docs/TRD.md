# 技术需求文档 (TRD)

> 本文档描述 Environment Fabric 的 API 接口规范、项目结构和配置说明。

---

## 1. API 接口规范

### 1.1 环境管理

#### POST /envs — 创建环境

**请求体** (Form 数据):
```
workflow_id: "workflow123"
node_id: "node123"
version_id: "v1" (可选)
python_version: "3.11" (可选)
packages: ["numpy>=1.24.0", "pandas>=2.0.0"] (可选)
requirements_file: <file> (可选)
```

**响应**:
```json
{
    "workflow_id": "workflow123",
    "node_id": "node123",
    "version_id": "v1",
    "env_path": "/mnt/nas/envs/workflow123_node123_v1",
    "python_version": "3.11",
    "status": "created",
    "pyproject_toml": "[project]\nname = \"workflow123-node123\"..."
}
```

---

#### GET /envs/{workflow_id}/{node_id}/export — 导出环境配置

**查询参数**:
- `version_id`: "v1" (可选)

**响应**:
```json
{
    "workflow_id": "workflow123",
    "node_id": "node123",
    "version_id": "v1",
    "pyproject_toml": "...",
    "uv_lock": "..."
}
```

---

#### POST /envs/{workflow_id}/{node_id}/sync — 从 lock 文件同步环境

用于从 `uv.lock` 重建 `.venv`，实现环境复现。

**响应**:
```json
{
    "workflow_id": "workflow123",
    "node_id": "node123",
    "status": "synced",
    "packages_installed": 15
}
```

---

### 1.2 依赖管理 (CRUD)

#### POST /envs/{workflow_id}/{node_id}/deps — 添加依赖 (uv add)

**请求体**:
```json
{
    "packages": ["scikit-learn>=1.0", "torch==2.0.0"]
}
```

---

#### DELETE /envs/{workflow_id}/{node_id}/deps — 删除依赖 (uv remove)

**请求体**:
```json
{
    "packages": ["torch", "scikit-learn"]
}
```

---

#### GET /envs/{workflow_id}/{node_id}/deps — 列出依赖

从 `pyproject.toml` 读取依赖列表。

**响应**:
```json
{
    "workflow_id": "workflow123",
    "node_id": "node123",
    "dependencies": [
        "numpy>=1.24.0",
        "pandas>=2.0.0"
    ],
    "locked_versions": {
        "numpy": "1.24.3",
        "pandas": "2.1.0"
    }
}
```

---

### 1.3 代码执行

#### POST /envs/{workflow_id}/{node_id}/run — 执行代码 (uv run)

```json
{
    "code": "import numpy as np; print(np.__version__)",
    "timeout": 30
}
```

---

## 2. 项目结构

```
env_manager/
├── src/
│   ├── __init__.py
│   ├── api.py                 # FastAPI 路由定义
│   ├── config.py              # 配置管理
│   ├── models.py              # Pydantic 数据模型
│   ├── services/
│   │   ├── __init__.py
│   │   ├── lock_manager.py    # 并发锁管理器
│   │   ├── env_manager.py     # 环境生命周期管理
│   │   ├── dep_manager.py     # 依赖 CRUD 管理
│   │   ├── project_info.py    # 主项目依赖映射服务
│   │   └── uv_executor.py     # UV CLI 命令执行器
│   └── exceptions.py          # 自定义异常
├── docs/
│   ├── PRD.md                 # 产品需求文档
│   ├── ARD.md                 # 架构需求文档
│   └── TRD.md                 # 技术需求文档
├── envs/                      # 虚拟环境存储目录 (或挂载 NAS)
├── uv_cache/                  # UV 缓存目录 (同一分区!)
└── pyproject.toml
```

---

## 3. 跨平台 UV 执行器设计

```python
import sys
import asyncio
from pathlib import Path

class UVCommandExecutor:
    """跨平台 UV CLI 封装器 - 使用 uv add 而非 uv pip install"""
    
    def __init__(self, envs_base_path: Path):
        self.envs_base_path = envs_base_path
        self.is_windows = sys.platform == "win32"
    
    def _get_project_path(self, node_id: str) -> Path:
        return self.envs_base_path / node_id
    
    async def init_project(self, node_id: str, python_version: str) -> bool:
        """初始化 UV 项目"""
        project_path = self._get_project_path(node_id)
        project_path.mkdir(parents=True, exist_ok=True)
        cmd = ["uv", "init", str(project_path), "--python", python_version]
        # ... subprocess 执行
    
    async def add_packages(self, node_id: str, packages: list[str]) -> bool:
        """添加依赖 (uv add)"""
        project_path = self._get_project_path(node_id)
        cmd = ["uv", "add", "--project", str(project_path)] + packages
        # ... subprocess 执行
    
    async def remove_packages(self, node_id: str, packages: list[str]) -> bool:
        """删除依赖 (uv remove)"""
        project_path = self._get_project_path(node_id)
        cmd = ["uv", "remove", "--project", str(project_path)] + packages
        # ... subprocess 执行
    
    async def sync_env(self, node_id: str) -> bool:
        """从 uv.lock 同步环境"""
        project_path = self._get_project_path(node_id)
        cmd = ["uv", "sync", "--project", str(project_path)]
        # ... subprocess 执行
    
    async def run_code(self, node_id: str, code: str) -> tuple[str, str, int]:
        """在项目环境中执行代码"""
        project_path = self._get_project_path(node_id)
        cmd = ["uv", "run", "--project", str(project_path), "python", "-c", code]
        # ... subprocess 执行
```

---

## 4. 环境变量配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DATA_ROOT` | `/mnt/nas` | NAS/共享存储挂载点 |
| `ENVS_BASE_PATH` | `${DATA_ROOT}/envs` | 虚拟环境存储目录 |
| `UV_CACHE_DIR` | `${DATA_ROOT}/uv_cache` | **必须与 ENVS_BASE_PATH 同一分区且平级** |
| `DATABASE_URL` | `postgresql://...` | 审计数据库连接串 |
| `DEFAULT_PYTHON` | `3.11` | 默认 Python 版本 |
| `EXECUTION_TIMEOUT` | `30` | 代码执行默认超时（秒） |
| `CLEANUP_IDLE_HOURS` | `72` | 自动清理闲置环境阈值（小时） |

> [!WARNING]
> **硬性物理条件**
> 
> `UV_CACHE_DIR` 必须配置在与 `ENVS_BASE_PATH` **相同的物理分区**上，且为平级目录，否则无法使用 Hardlink 节省磁盘空间。

> [!IMPORTANT]
> **审计数据库为强制依赖**
>
> - ✅ 所有会对 env 产生副作用的操作（create/delete/add/update/remove/sync/run/cleanup）都会写入 `env_operations` 审计表
> - ✅ 服务启动时会执行 `init_db()` 自动建表并做一次 `SELECT 1` 连接校验
> - ❌ 若数据库不可连接或审计写入失败，相关请求会返回 `DB_AUDIT_ERROR` (500)

---

## 5. 错误码定义

| HTTP | 错误码 | 说明 |
|------|--------|------|
| 400 | `INVALID_PACKAGES` | packages 格式无效 |
| 404 | `ENV_NOT_FOUND` | 环境不存在 |
| 409 | `ENV_ALREADY_EXISTS` | 环境已存在 |
| 422 | `PACKAGE_RESOLUTION_FAILED` | 依赖解析失败 |
| 423 | `ENV_LOCKED` | 环境正在被其他操作使用 |
| 500 | `DB_AUDIT_ERROR` | 审计数据库初始化/连接失败或审计写入失败 |
| 500 | `UV_EXECUTION_ERROR` | UV 命令执行失败 |
| 504 | `EXECUTION_TIMEOUT` | 代码执行超时 |
