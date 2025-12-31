# db_service 模块说明

本模块提供环境操作的数据库审计功能，用于记录、追踪和分析对 node-level 环境（env）的所有操作。

## 模块结构

```
db_service/
├── __init__.py          # 模块初始化
├── connection.py        # 数据库连接和会话管理
├── models.py            # SQLAlchemy ORM 模型定义
├── env_repository.py    # 数据访问层（Repository 模式）
├── env_audit.py         # 高级审计辅助类
└── README.md           # 本文档
```

---

## 1. 核心组件

### 1.1 connection.py - 数据库连接管理

负责数据库连接的创建、会话管理和表初始化。

**主要功能：**
- 从环境变量 `DATABASE_URL` 读取数据库连接字符串（默认：PostgreSQL）
- 创建 SQLAlchemy engine 和 SessionLocal
- 提供 `get_db()` 依赖注入函数
- 提供 `init_db()` 函数用于初始化数据库表结构

**关键配置：**
- 连接池大小：`pool_size=5`
- 最大溢出连接：`max_overflow=10`
- 连接健康检查：`pool_pre_ping=True`

**使用示例：**
```python
from src.db_service.connection import SessionLocal, init_db

# 初始化数据库表
init_db()

# 获取数据库会话
db = SessionLocal()
try:
    # 使用 db 进行操作
    pass
finally:
    db.close()
```

---

### 1.2 models.py - 数据模型

定义 `env_operations` 表的 SQLAlchemy ORM 模型。

**表名：** `env_operations`

**字段说明：**

| 字段名 | 类型 | 说明 |
|--------|------|------|
| `id` | `Integer` | 主键，自增 |
| `workflow_id` | `String(255)` | 工作流标识，索引 |
| `node_id` | `String(255)` | 节点/环境标识，索引 |
| `operation` | `String(50)` | 操作类型（如 `create_env`, `add_deps` 等），索引 |
| `status` | `String(20)` | 操作状态（`success` / `failed`），索引 |
| `stdout` | `Text` | 标准输出内容 |
| `stderr` | `Text` | 标准错误输出 |
| `exit_code` | `Integer` | 命令退出码 |
| `duration_ms` | `Float` | 操作耗时（毫秒） |
| `operation_metadata` | `JSONB` | 扩展元数据（存储为 `metadata` 列） |
| `created_at` | `DateTime(timezone=True)` | 记录创建时间（UTC），索引 |

**索引：**
- 单列索引：`workflow_id`, `node_id`, `operation`, `status`, `created_at`
- 复合索引：
  - `idx_env_ops_workflow_node`：`(workflow_id, node_id)`
  - `idx_env_ops_node_operation`：`(node_id, operation)`
- 特殊索引（通过 DDL 事件创建）：
  - `idx_env_ops_metadata`：GIN 索引，用于 JSONB 查询
  - `idx_env_ops_stderr_fts`：全文搜索索引，用于错误信息搜索

**使用示例：**
```python
from src.db_service.models import EnvOperation

# 创建记录
record = EnvOperation(
    workflow_id="workflow_123",
    node_id="node_456",
    operation="add_deps",
    status="success",
    stdout="...",
    metadata={"packages": ["numpy", "pandas"]}
)
```

---

### 1.3 env_repository.py - 数据访问层

实现 Repository 模式，提供对 `env_operations` 表的 CRUD 操作。

**类：** `EnvOperationRepository`

**主要方法：**

| 方法 | 说明 |
|------|------|
| `create(...)` | 创建一条操作记录 |
| `get_by_id(record_id)` | 根据 ID 查询记录 |
| `get_by_workflow_id(workflow_id)` | 查询指定工作流的所有操作（按时间倒序） |
| `get_by_node_id(node_id)` | 查询指定节点的所有操作（按时间倒序） |
| `get_by_workflow_and_node(workflow_id, node_id)` | 查询指定工作流和节点的操作（按时间倒序） |
| `get_failed_operations(workflow_id=None)` | 查询失败的操作（可选按工作流过滤） |
| `delete_by_workflow_id(workflow_id)` | 删除指定工作流的所有记录 |

**使用示例：**
```python
from src.db_service.connection import SessionLocal
from src.db_service.env_repository import EnvOperationRepository

db = SessionLocal()
repo = EnvOperationRepository(db)

# 创建记录
record = repo.create(
    workflow_id="wf_1",
    node_id="node_1",
    operation="add_deps",
    status="success",
    stdout="...",
    metadata={"packages": ["numpy"]}
)

# 查询记录
records = repo.get_by_workflow_id("wf_1")
```

---

### 1.4 env_audit.py - 高级审计辅助类

提供便捷的审计接口，简化操作记录的创建流程。

**类：** `EnvAudit`

**设计理念：**
- 一个操作 = 一条记录
- 自动计算执行耗时
- 统一成功/失败记录格式

**主要方法：**

| 方法 | 说明 |
|------|------|
| `start()` | 静态方法，返回开始时间戳 |
| `success(...)` | 记录成功操作 |
| `failed(...)` | 记录失败操作 |

**典型使用模式：**
```python
from src.db_service.connection import SessionLocal
from src.db_service.env_audit import EnvAudit

db = SessionLocal()
audit = EnvAudit(
    db=db,
    workflow_id="workflow_123",
    node_id="node_456"
)

start_time = audit.start()

try:
    # 执行操作
    result = perform_operation()
    audit.success(
        operation="add_deps",
        start_time=start_time,
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        metadata={"packages": ["numpy"]}
    )
except Exception as e:
    audit.failed(
        operation="add_deps",
        start_time=start_time,
        error=str(e),
        metadata={"packages": ["numpy"]}
    )
    raise
finally:
    db.close()
```

---

## 2. 操作类型（operation）约定

建议使用以下标准操作类型，保持一致性便于统计和筛选：

| 操作类型 | 说明 |
|----------|------|
| `create_env` | 创建环境（uv init + 可选安装依赖） |
| `delete_env` | 删除环境目录 |
| `add_deps` | 添加依赖（uv add） |
| `update_deps` | 升级依赖（uv add --upgrade） |
| `remove_deps` | 移除依赖（uv remove） |
| `sync` | 同步环境（uv sync） |
| `run` | 在环境中执行代码（uv run） |
| `cleanup` | 清理闲置环境（批量删除） |

---

## 3. metadata 字段约定

`metadata` 字段用于存储与操作相关的结构化扩展信息，常见示例：

### create_env
```json
{
  "python_version": "3.11",
  "packages": ["numpy", "pandas"],
  "requirements_file": "/path/to/requirements.txt"
}
```

### add_deps / update_deps / remove_deps
```json
{
  "packages": ["numpy", "pandas"],
  "requirements_file": "/path/to/requirements.txt"
}
```

### run
```json
{
  "timeout": 30,
  "code": "print('hello')"
}
```

### cleanup
```json
{
  "idle_hours": 24,
  "deleted": ["env1", "env2"]
}
```

### failed 操作
失败时，错误信息会自动添加到 metadata：
```json
{
  "packages": ["numpy"],
  "error": "Package not found"
}
```

---

## 4. 数据库初始化

**重要：** 本模块是主服务的强制审计依赖。服务启动时会自动调用 `init_db()` 创建表并校验数据库连接。如果初始化失败，会抛出 `DBAuditError` 异常。

**初始化流程：**
1. 导入所有模型（触发模型注册）
2. 调用 `Base.metadata.create_all()` 创建表
3. 触发 `after_create` 事件，创建 GIN 和全文搜索索引

**环境变量：**
- `DATABASE_URL`：数据库连接字符串（必需）
  - 默认值：`postgresql://agentgit:agentgit_dev_password@localhost:54320/agentgit_testing`
  - 格式：`postgresql://user:password@host:port/database`

---

## 5. 查询示例

### 查询工作流的所有操作
```python
repo = EnvOperationRepository(db)
operations = repo.get_by_workflow_id("workflow_123")
```

### 查询节点的所有操作
```python
operations = repo.get_by_node_id("node_456")
```

### 查询失败的操作
```python
failed_ops = repo.get_failed_operations()
# 或按工作流过滤
failed_ops = repo.get_failed_operations(workflow_id="workflow_123")
```

### 使用 JSONB 查询（PostgreSQL）
```python
from sqlalchemy import text

# 查询包含特定包的安装操作
result = db.execute(text("""
    SELECT * FROM env_operations
    WHERE metadata @> '{"packages": ["numpy"]}'
    AND operation = 'add_deps'
"""))
```

### 全文搜索错误信息
```python
result = db.execute(text("""
    SELECT * FROM env_operations
    WHERE to_tsvector('english', stderr) @@ to_tsquery('error & package')
"""))
```

---

## 6. 注意事项

1. **数据库连接**：确保 `DATABASE_URL` 环境变量正确配置
2. **事务管理**：Repository 的 `create()` 方法会自动提交，如需事务控制请使用 `db.begin()`
3. **性能考虑**：大量写入时考虑批量插入或异步写入
4. **数据清理**：定期清理旧记录，避免表过大影响性能
5. **错误处理**：审计失败不应影响主业务流程，但会抛出 `DBAuditError` 异常

---

## 7. 依赖关系

```
connection.py
  └── models.py (导入模型以注册到 Base)
      └── env_repository.py (使用 EnvOperation 模型)
          └── env_audit.py (使用 EnvOperationRepository)
```

**外部依赖：**
- SQLAlchemy
- PostgreSQL（使用 JSONB 和全文搜索功能）
