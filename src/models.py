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
    python_version: str | None = None
    packages: list[str] = Field(default_factory=list)
    requirements_file: str | None = None

    @field_validator("packages")
    @classmethod
    def validate_packages(cls, v: list[str]) -> list[str]:
        if any(not p.strip() for p in v):
            raise ValueError("Package names cannot be empty or whitespace")
        return v


class CreateEnvResponse(BaseModel):
    workflow_id: str
    node_id: str
    env_path: str
    python_version: str
    status: str
    pyproject_toml: str


class EnvStatusResponse(BaseModel):
    workflow_id: str
    node_id: str
    status: str
    env_path: str
    has_pyproject: bool
    has_uv_lock: bool
    has_venv: bool
    metadata: dict[str, Any] | None = None


class PackagesRequest(BaseModel):
    packages: list[str] = Field(default_factory=list)
    requirements_file: str | None = None

    @field_validator("packages")
    @classmethod
    def validate_packages(cls, v: list[str]) -> list[str]:
        if any(not p.strip() for p in v):
            raise ValueError("Package names cannot be empty or whitespace")
        return v


class DepsResponse(BaseModel):
    workflow_id: str
    node_id: str
    dependencies: list[str]
    locked_versions: dict[str, str]


class ExportResponse(BaseModel):
    workflow_id: str
    node_id: str
    pyproject_toml: str
    uv_lock: str | None


class SyncResponse(BaseModel):
    workflow_id: str
    node_id: str
    status: str
    packages_installed: int | None = None


class RunRequest(BaseModel):
    code: str = Field(min_length=1)
    timeout: int | None = Field(default=None, ge=1)


class RunResponse(BaseModel):
    workflow_id: str
    node_id: str
    stdout: str
    stderr: str
    exit_code: int


class CleanupRequest(BaseModel):
    idle_hours: int | None = Field(default=None, ge=1)


class CleanupResponse(BaseModel):
    deleted: list[str]
    checked_at: datetime

