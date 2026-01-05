from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ErrorResponse(BaseModel):
    code: str
    message: str


class CreateEnvRequest(BaseModel):
    workflow_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    version_id: str | None = Field(default=None)
    python_version: str | None = None
    packages: list[str] = Field(default_factory=list)

    @field_validator("packages")
    @classmethod
    def validate_packages(cls, v: list[str]) -> list[str]:
        if any(not p.strip() for p in v):
            raise ValueError("Package names cannot be empty or whitespace")
        return v


class CreateEnvResponse(BaseModel):
    workflow_id: str
    node_id: str
    version_id: str | None = None
    env_path: str
    python_version: str
    status: str
    pyproject_toml: str


class EnvStatusResponse(BaseModel):
    workflow_id: str
    node_id: str
    version_id: str | None = None
    status: str
    env_path: str
    has_pyproject: bool
    has_uv_lock: bool
    has_venv: bool
    metadata: dict[str, Any] | None = None


class PackagesRequest(BaseModel):
    packages: list[str] = Field(default_factory=list)

    @field_validator("packages")
    @classmethod
    def validate_packages(cls, v: list[str]) -> list[str]:
        if any(not p.strip() for p in v):
            raise ValueError("Package names cannot be empty or whitespace")
        return v


class DepsResponse(BaseModel):
    workflow_id: str
    node_id: str
    version_id: str | None = None
    dependencies: list[str]
    locked_versions: dict[str, str]


class ExportResponse(BaseModel):
    workflow_id: str
    node_id: str
    version_id: str | None = None
    pyproject_toml: str
    uv_lock: str | None


class SyncResponse(BaseModel):
    workflow_id: str
    node_id: str
    version_id: str | None = None
    status: str
    packages_installed: int | None = None


class RunRequest(BaseModel):
    code: str = Field(min_length=1)
    timeout: int | None = Field(default=None, ge=1)
    version_id: str | None = Field(default=None)


class RunResponse(BaseModel):
    workflow_id: str
    node_id: str
    version_id: str | None = None
    stdout: str
    stderr: str
    exit_code: int


class CleanupRequest(BaseModel):
    idle_hours: int | None = Field(default=None, ge=1)


class CleanupResponse(BaseModel):
    deleted: list[str]
    checked_at: datetime

class EnvOperationResponse(BaseModel):
    """单条环境操作记录响应"""
    id: int
    workflow_id: str
    node_id: str
    operation: str
    status: str
    stdout: str | None = None
    stderr: str | None = None
    exit_code: int | None = None
    duration_ms: float | None = None
    metadata: dict[str, Any] | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class EnvOperationListResponse(BaseModel):
    """环境操作列表响应"""
    total: int
    items: list[EnvOperationResponse]
