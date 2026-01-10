"""
Ray Environment Executor

封装 Ray 与 Environment Fabric 的集成，提供：
- 工作流环境预热 (AOT)
- Ray Task/Actor 执行封装
- 批量环境管理

使用示例:
    >>> from src.integrations import RayEnvExecutor, EnvConfig
    >>> 
    >>> executor = RayEnvExecutor()
    >>> 
    >>> # 预热工作流所有节点的环境
    >>> configs = [
    ...     EnvConfig("wf-001", "node-A", packages=["numpy"]),
    ...     EnvConfig("wf-001", "node-B", packages=["pandas"]),
    ... ]
    >>> python_paths = executor.prepare_workflow(configs)
    >>> 
    >>> # 执行代码
    >>> result = executor.execute("wf-001", "node-A", "import numpy; print(numpy.__version__)")
    >>> print(result["stdout"])

与 Ray 原生集成:
    >>> import ray
    >>> 
    >>> executor = RayEnvExecutor()
    >>> python_path = executor.get_python_path("wf-001", "node-A")
    >>> 
    >>> @ray.remote(runtime_env={"py_executable": python_path})
    ... def my_task():
    ...     import numpy
    ...     return numpy.__version__
    >>> 
    >>> ray.init()
    >>> print(ray.get(my_task.remote()))
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from src.integrations.client import EnvFabricClient, EnvConfig


@dataclass
class ExecutionResult:
    """代码执行结果"""
    env_id: str
    stdout: str
    stderr: str
    returncode: int
    python_path: str


class RayEnvExecutor:
    """
    Ray + Environment Fabric 执行器
    
    提供工作流环境预热和代码执行的高级封装。
    可以与 Ray 原生 API 结合使用，也可以独立使用 subprocess 执行。
    
    Attributes:
        client: Environment Fabric API 客户端
        envs_base_path: 环境存储根目录（用于本地路径计算）
    
    Example:
        >>> executor = RayEnvExecutor()
        >>> 
        >>> # 方式一：使用内置执行（subprocess）
        >>> result = executor.execute("wf-001", "node-A", "print('hello')")
        >>> 
        >>> # 方式二：获取路径后使用 Ray
        >>> python_path = executor.get_python_path("wf-001", "node-A")
        >>> @ray.remote(runtime_env={"py_executable": python_path})
        >>> def task(): ...
    """
    
    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        envs_base_path: str | Path | None = None,
        client: EnvFabricClient | None = None,
    ) -> None:
        """
        初始化执行器
        
        Args:
            base_url: Environment Fabric API 地址
            envs_base_path: 环境存储根目录，默认从API状态获取
            client: 可选的预配置客户端
        """
        self.client = client or EnvFabricClient(base_url)
        self._envs_base_path = Path(envs_base_path) if envs_base_path else None
        self._python_cache: dict[str, str] = {}
    
    def __enter__(self) -> "RayEnvExecutor":
        return self
    
    def __exit__(self, *args: Any) -> None:
        self.close()
    
    def close(self) -> None:
        """关闭客户端连接"""
        self.client.close()
    
    # ========== 环境管理 ==========
    
    def ensure_env(
        self,
        workflow_id: str,
        node_id: str,
        *,
        version_id: str | None = None,
        python_version: str | None = None,
        packages: list[str] | None = None,
    ) -> str:
        """
        确保环境存在，返回 Python 路径
        
        这是与 Ray py_executable 集成的主要方法。
        
        Returns:
            Python 解释器的绝对路径
        """
        cache_key = EnvFabricClient.get_env_id(workflow_id, node_id, version_id)
        
        if cache_key in self._python_cache:
            return self._python_cache[cache_key]
        
        python_path = self.client.ensure_env(
            workflow_id,
            node_id,
            version_id=version_id,
            python_version=python_version,
            packages=packages,
        )
        
        self._python_cache[cache_key] = python_path
        return python_path
    
    def get_python_path(
        self,
        workflow_id: str,
        node_id: str,
        version_id: str | None = None,
    ) -> str:
        """
        获取已存在环境的 Python 路径
        
        注意：此方法假定环境已存在。如果环境可能不存在，请使用 ensure_env。
        
        Returns:
            Python 解释器的绝对路径
        
        Raises:
            httpx.HTTPStatusError: 如果环境不存在
        """
        cache_key = EnvFabricClient.get_env_id(workflow_id, node_id, version_id)
        
        if cache_key in self._python_cache:
            return self._python_cache[cache_key]
        
        status = self.client.get_status(workflow_id, node_id, version_id)
        python_path = EnvFabricClient.get_python_path(status["env_path"])
        
        self._python_cache[cache_key] = python_path
        return python_path
    
    def prepare_workflow(
        self,
        configs: list[EnvConfig],
    ) -> dict[str, str]:
        """
        AOT 预热：批量构建工作流所有节点的环境
        
        在工作流运行之前调用此方法，可以避免冷启动延迟。
        
        Args:
            configs: 环境配置列表
        
        Returns:
            {env_id: python_path} 映射字典
        
        Example:
            >>> configs = [
            ...     EnvConfig("ml-pipeline", "data-loader", packages=["pandas"]),
            ...     EnvConfig("ml-pipeline", "model-trainer", packages=["scikit-learn"]),
            ... ]
            >>> paths = executor.prepare_workflow(configs)
            >>> # paths = {
            >>> #     "ml-pipeline_data-loader": "/data/envs/.../python",
            >>> #     "ml-pipeline_model-trainer": "/data/envs/.../python",
            >>> # }
        """
        result: dict[str, str] = {}
        
        for config in configs:
            env_id = EnvFabricClient.get_env_id(
                config.workflow_id, 
                config.node_id, 
                config.version_id,
            )
            python_path = self.client.ensure_env_from_config(config)
            result[env_id] = python_path
            self._python_cache[env_id] = python_path
        
        return result
    
    # ========== 代码执行 ==========
    
    def execute(
        self,
        workflow_id: str,
        node_id: str,
        code: str,
        *,
        version_id: str | None = None,
        timeout: int = 30,
        use_api: bool = True,
    ) -> ExecutionResult:
        """
        在指定环境中执行 Python 代码
        
        Args:
            workflow_id: 工作流 ID
            node_id: 节点 ID
            code: 要执行的 Python 代码
            version_id: 可选的版本 ID
            timeout: 执行超时时间（秒）
            use_api: 是否通过 API 执行（否则使用本地 subprocess）
        
        Returns:
            ExecutionResult 包含 stdout、stderr、returncode
        """
        env_id = EnvFabricClient.get_env_id(workflow_id, node_id, version_id)
        
        if use_api:
            # 通过 API 执行
            result = self.client.run_code(
                workflow_id, node_id, code, 
                timeout=timeout, 
                version_id=version_id,
            )
            python_path = self._python_cache.get(env_id, "")
            return ExecutionResult(
                env_id=env_id,
                stdout=result["stdout"],
                stderr=result["stderr"],
                returncode=result["exit_code"],
                python_path=python_path,
            )
        else:
            # 本地 subprocess 执行
            python_path = self.get_python_path(workflow_id, node_id, version_id)
            proc = subprocess.run(
                [python_path, "-c", code],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return ExecutionResult(
                env_id=env_id,
                stdout=proc.stdout,
                stderr=proc.stderr,
                returncode=proc.returncode,
                python_path=python_path,
            )
    
    def execute_script(
        self,
        workflow_id: str,
        node_id: str,
        script_path: str | Path,
        args: list[str] | None = None,
        *,
        version_id: str | None = None,
        timeout: int = 300,
    ) -> ExecutionResult:
        """
        在指定环境中执行 Python 脚本文件
        
        Args:
            workflow_id: 工作流 ID
            node_id: 节点 ID
            script_path: 脚本文件路径
            args: 命令行参数
            version_id: 可选的版本 ID
            timeout: 执行超时时间（秒）
        
        Returns:
            ExecutionResult
        """
        env_id = EnvFabricClient.get_env_id(workflow_id, node_id, version_id)
        python_path = self.get_python_path(workflow_id, node_id, version_id)
        
        cmd = [python_path, str(script_path)] + (args or [])
        
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        
        return ExecutionResult(
            env_id=env_id,
            stdout=proc.stdout,
            stderr=proc.stderr,
            returncode=proc.returncode,
            python_path=python_path,
        )
    
    # ========== Ray 集成辅助方法 ==========
    
    def get_ray_runtime_env(
        self,
        workflow_id: str,
        node_id: str,
        version_id: str | None = None,
        working_dir: str | None = None,
        **extra_runtime_env: Any,
    ) -> dict[str, Any]:
        """
        获取 Ray runtime_env 配置字典
        
        Args:
            workflow_id: 工作流 ID
            node_id: 节点 ID
            version_id: 可选的版本 ID
            working_dir: 工作目录
            **extra_runtime_env: 其他 runtime_env 参数
        
        Returns:
            可直接用于 @ray.remote(runtime_env=...) 的字典
        
        Example:
            >>> runtime_env = executor.get_ray_runtime_env("wf-001", "node-A")
            >>> @ray.remote(runtime_env=runtime_env)
            ... def my_task():
            ...     ...
        """
        python_path = self.get_python_path(workflow_id, node_id, version_id)
        
        runtime_env: dict[str, Any] = {
            "py_executable": python_path,
            **extra_runtime_env,
        }
        
        if working_dir:
            runtime_env["working_dir"] = working_dir
        
        return runtime_env
    
    def create_ray_task(
        self,
        workflow_id: str,
        node_id: str,
        version_id: str | None = None,
    ) -> Callable[..., Any]:
        """
        创建一个绑定到特定环境的 Ray remote 函数装饰器
        
        注意：需要先安装 ray 包。
        
        Args:
            workflow_id: 工作流 ID
            node_id: 节点 ID
            version_id: 可选的版本 ID
        
        Returns:
            ray.remote 装饰器（已配置 py_executable）
        
        Example:
            >>> decorator = executor.create_ray_task("wf-001", "node-A")
            >>> @decorator
            ... def my_task():
            ...     import numpy
            ...     return numpy.__version__
            >>> ray.get(my_task.remote())
        """
        try:
            import ray
        except ImportError as e:
            raise ImportError(
                "Ray is required for this method. "
                "Install it with: pip install ray"
            ) from e
        
        runtime_env = self.get_ray_runtime_env(workflow_id, node_id, version_id)
        return ray.remote(runtime_env=runtime_env)


# ========== 便捷函数 ==========

def ensure_env(
    workflow_id: str,
    node_id: str,
    *,
    version_id: str | None = None,
    packages: list[str] | None = None,
    base_url: str = "http://localhost:8000",
) -> str:
    """
    便捷函数：确保环境存在并返回 Python 路径
    
    Example:
        >>> python_path = ensure_env("wf-001", "node-A", packages=["numpy"])
        >>> @ray.remote(runtime_env={"py_executable": python_path})
        ... def task(): ...
    """
    with EnvFabricClient(base_url) as client:
        return client.ensure_env(
            workflow_id, 
            node_id, 
            version_id=version_id, 
            packages=packages,
        )


def get_runtime_env(
    workflow_id: str,
    node_id: str,
    *,
    version_id: str | None = None,
    packages: list[str] | None = None,
    working_dir: str | None = None,
    base_url: str = "http://localhost:8000",
) -> dict[str, Any]:
    """
    便捷函数：获取 Ray runtime_env 配置
    
    Example:
        >>> runtime_env = get_runtime_env("wf-001", "node-A", packages=["numpy"])
        >>> @ray.remote(runtime_env=runtime_env)
        ... def task(): ...
    """
    with EnvFabricClient(base_url) as client:
        python_path = client.ensure_env(
            workflow_id, 
            node_id, 
            version_id=version_id, 
            packages=packages,
        )
    
    runtime_env: dict[str, Any] = {"py_executable": python_path}
    if working_dir:
        runtime_env["working_dir"] = working_dir
    
    return runtime_env
