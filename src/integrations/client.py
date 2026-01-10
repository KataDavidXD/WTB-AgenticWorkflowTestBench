"""
Environment Fabric API Client

通过 HTTP 调用 Environment Fabric FastAPI 服务的客户端封装。
支持环境的创建、查询、同步和依赖管理。

使用示例:
    >>> client = EnvFabricClient("http://localhost:8000")
    >>> python_path = client.ensure_env("wf-001", "node-A", packages=["numpy"])
    >>> print(python_path)
    /path/to/data/envs/wf-001_node-A/.venv/bin/python
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx


@dataclass
class EnvConfig:
    """环境配置"""
    workflow_id: str
    node_id: str
    version_id: str | None = None
    python_version: str | None = None
    packages: list[str] = field(default_factory=list)


class EnvFabricClient:
    """
    Environment Fabric API 客户端
    
    通过 HTTP 调用 FastAPI 服务，管理虚拟环境的生命周期。
    
    Attributes:
        base_url: FastAPI 服务地址
        timeout: 请求超时时间（秒），默认 300 秒以支持大依赖安装
    
    Example:
        >>> client = EnvFabricClient("http://localhost:8000")
        >>> 
        >>> # 确保环境存在
        >>> python_path = client.ensure_env("wf-001", "node-A", packages=["numpy>=1.24"])
        >>> 
        >>> # 获取环境状态
        >>> status = client.get_status("wf-001", "node-A")
        >>> print(status["status"])  # "ACTIVE"
    """
    
    def __init__(
        self, 
        base_url: str = "http://localhost:8000",
        timeout: float = 300.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout)
    
    def __enter__(self) -> "EnvFabricClient":
        return self
    
    def __exit__(self, *args: Any) -> None:
        self.close()
    
    def close(self) -> None:
        """关闭 HTTP 客户端"""
        self._client.close()
    
    def _build_env_url(
        self, 
        workflow_id: str, 
        node_id: str, 
        suffix: str = "",
    ) -> str:
        """构建环境相关的 API URL"""
        url = f"{self.base_url}/envs/{workflow_id}/{node_id}"
        if suffix:
            url = f"{url}/{suffix}"
        return url
    
    def _get_version_params(self, version_id: str | None) -> dict[str, str]:
        """构建版本查询参数"""
        if version_id:
            return {"version_id": version_id}
        return {}
    
    # ========== 环境管理 ==========
    
    def create_env(
        self,
        workflow_id: str,
        node_id: str,
        *,
        version_id: str | None = None,
        python_version: str | None = None,
        packages: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        创建新环境
        
        Args:
            workflow_id: 工作流 ID
            node_id: 节点 ID
            version_id: 可选的版本 ID
            python_version: Python 版本，默认使用服务配置
            packages: 初始安装的依赖包列表
        
        Returns:
            包含 env_path、status 等信息的字典
        
        Raises:
            httpx.HTTPStatusError: API 调用失败
        """
        data: dict[str, Any] = {
            "workflow_id": workflow_id,
            "node_id": node_id,
        }
        if version_id:
            data["version_id"] = version_id
        if python_version:
            data["python_version"] = python_version
        if packages:
            data["packages"] = packages
        
        resp = self._client.post(f"{self.base_url}/envs", data=data)
        resp.raise_for_status()
        return resp.json()
    
    def get_status(
        self,
        workflow_id: str,
        node_id: str,
        version_id: str | None = None,
    ) -> dict[str, Any]:
        """
        获取环境状态
        
        Returns:
            包含 status、has_venv、has_pyproject 等信息的字典
        """
        url = self._build_env_url(workflow_id, node_id)
        params = self._get_version_params(version_id)
        resp = self._client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()
    
    def delete_env(
        self,
        workflow_id: str,
        node_id: str,
        version_id: str | None = None,
    ) -> dict[str, Any]:
        """删除环境"""
        url = self._build_env_url(workflow_id, node_id)
        params = self._get_version_params(version_id)
        resp = self._client.delete(url, params=params)
        resp.raise_for_status()
        return resp.json()
    
    def sync_env(
        self,
        workflow_id: str,
        node_id: str,
        version_id: str | None = None,
    ) -> dict[str, Any]:
        """
        从 uv.lock 同步环境
        
        用于从锁文件重建 .venv，实现环境复现。
        """
        url = self._build_env_url(workflow_id, node_id, "sync")
        params = self._get_version_params(version_id)
        resp = self._client.post(url, params=params)
        resp.raise_for_status()
        return resp.json()
    
    def export_env(
        self,
        workflow_id: str,
        node_id: str,
        version_id: str | None = None,
    ) -> dict[str, Any]:
        """
        导出环境配置
        
        Returns:
            包含 pyproject_toml 和 uv_lock 内容的字典
        """
        url = self._build_env_url(workflow_id, node_id, "export")
        params = self._get_version_params(version_id)
        resp = self._client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()
    
    # ========== 依赖管理 ==========
    
    def add_packages(
        self,
        workflow_id: str,
        node_id: str,
        packages: list[str],
        version_id: str | None = None,
    ) -> dict[str, Any]:
        """添加依赖包"""
        url = self._build_env_url(workflow_id, node_id, "deps")
        params = self._get_version_params(version_id)
        resp = self._client.post(url, data={"packages": packages}, params=params)
        resp.raise_for_status()
        return resp.json()
    
    def remove_packages(
        self,
        workflow_id: str,
        node_id: str,
        packages: list[str],
        version_id: str | None = None,
    ) -> dict[str, Any]:
        """移除依赖包"""
        url = self._build_env_url(workflow_id, node_id, "deps")
        params = self._get_version_params(version_id)
        resp = self._client.request("DELETE", url, data={"packages": packages}, params=params)
        resp.raise_for_status()
        return resp.json()
    
    def list_packages(
        self,
        workflow_id: str,
        node_id: str,
        version_id: str | None = None,
    ) -> dict[str, Any]:
        """列出已安装的依赖包"""
        url = self._build_env_url(workflow_id, node_id, "deps")
        params = self._get_version_params(version_id)
        resp = self._client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()
    
    # ========== 代码执行 ==========
    
    def run_code(
        self,
        workflow_id: str,
        node_id: str,
        code: str,
        timeout: int | None = None,
        version_id: str | None = None,
    ) -> dict[str, Any]:
        """
        在环境中执行 Python 代码
        
        Args:
            workflow_id: 工作流 ID
            node_id: 节点 ID
            code: 要执行的 Python 代码
            timeout: 执行超时时间（秒）
            version_id: 可选的版本 ID
        
        Returns:
            包含 stdout、stderr、exit_code 的字典
        """
        url = self._build_env_url(workflow_id, node_id, "run")
        json_data: dict[str, Any] = {"code": code}
        if timeout:
            json_data["timeout"] = timeout
        if version_id:
            json_data["version_id"] = version_id
        
        resp = self._client.post(url, json=json_data)
        resp.raise_for_status()
        return resp.json()
    
    # ========== 高级方法 ==========
    
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
        确保环境存在，返回 Python 解释器路径
        
        如果环境不存在则创建。这是与 Ray 集成的主要入口方法。
        
        Args:
            workflow_id: 工作流 ID
            node_id: 节点 ID
            version_id: 可选的版本 ID
            python_version: Python 版本
            packages: 依赖包列表
        
        Returns:
            .venv/bin/python (Linux) 或 .venv/Scripts/python.exe (Windows) 的绝对路径
        
        Example:
            >>> client = EnvFabricClient()
            >>> python_path = client.ensure_env("wf-001", "node-A", packages=["numpy"])
            >>> # 可用于 Ray 的 py_executable 参数
        """
        try:
            status = self.get_status(workflow_id, node_id, version_id)
            env_path = status["env_path"]
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                # 环境不存在，创建它
                result = self.create_env(
                    workflow_id,
                    node_id,
                    version_id=version_id,
                    python_version=python_version,
                    packages=packages,
                )
                env_path = result["env_path"]
            else:
                raise
        
        return self.get_python_path(env_path)
    
    def ensure_env_from_config(self, config: EnvConfig) -> str:
        """
        从 EnvConfig 确保环境存在
        
        Args:
            config: 环境配置对象
        
        Returns:
            Python 解释器路径
        """
        return self.ensure_env(
            config.workflow_id,
            config.node_id,
            version_id=config.version_id,
            python_version=config.python_version,
            packages=config.packages,
        )
    
    @staticmethod
    def get_python_path(env_path: str) -> str:
        """
        根据环境路径获取 Python 解释器路径
        
        Args:
            env_path: 环境根目录路径（如 /data/envs/wf-001_node-A）
        
        Returns:
            .venv/bin/python 或 .venv/Scripts/python.exe 的路径
        """
        venv_path = Path(env_path) / ".venv"
        if sys.platform == "win32":
            return str(venv_path / "Scripts" / "python.exe")
        else:
            return str(venv_path / "bin" / "python")
    
    @staticmethod
    def get_env_id(
        workflow_id: str, 
        node_id: str, 
        version_id: str | None = None,
    ) -> str:
        """
        生成环境目录名
        
        Args:
            workflow_id: 工作流 ID
            node_id: 节点 ID
            version_id: 可选的版本 ID
        
        Returns:
            环境目录名（如 wf-001_node-A 或 wf-001_node-A_v1）
        """
        base = f"{workflow_id}_{node_id}"
        if version_id:
            return f"{base}_{version_id}"
        return base
