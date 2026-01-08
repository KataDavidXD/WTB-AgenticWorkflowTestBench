# examples/grpc_client.py
from __future__ import annotations

import asyncio
import grpc

from src.grpc_generated import env_manager_pb2 as pb2
from src.grpc_generated import env_manager_pb2_grpc as pb2_grpc


async def main():
    """gRPC 客户端使用示例"""

    # 连接 gRPC 服务器
    async with grpc.aio.insecure_channel("localhost:50051") as channel:
        stub = pb2_grpc.EnvManagerServiceStub(channel)

        # 1. 创建环境
        print("Creating environment...")
        create_response = await stub.CreateEnv(
            pb2.CreateEnvRequest(
                workflow_id="wf-001",
                node_id="node-001",
                python_version="3.11",
                packages=["requests", "pandas>=2.0"],
            )
        )
        print(f"Created: {create_response.env_path}")
        print(f"Python: {create_response.python_version}")

        # 2. 查看状态
        print("\nGetting status...")
        status_response = await stub.GetEnvStatus(
            pb2.GetEnvStatusRequest(
                workflow_id="wf-001",
                node_id="node-001",
            )
        )
        print(f"Status: {status_response.status}")
        print(f"Has venv: {status_response.has_venv}")

        # 3. 添加依赖
        print("\nAdding dependencies...")
        add_response = await stub.AddDeps(
            pb2.DepsRequest(
                workflow_id="wf-001",
                node_id="node-001",
                packages=["numpy"],
            )
        )
        print(f"Add status: {add_response.status}")

        # 4. 列出依赖
        print("\nListing dependencies...")
        list_response = await stub.ListDeps(
            pb2.ListDepsRequest(
                workflow_id="wf-001",
                node_id="node-001",
            )
        )
        print(f"Dependencies: {list_response.dependencies}")
        print(f"Locked versions: {dict(list_response.locked_versions)}")

        # 5. 运行代码
        print("\nRunning code...")
        run_response = await stub.RunCode(
            pb2.RunCodeRequest(
                workflow_id="wf-001",
                node_id="node-001",
                code="import numpy; print(numpy.sqrt(10000))",
                timeout=30,
            )
        )
        print(f"stdout: {run_response.stdout}")
        print(f"exit_code: {run_response.exit_code}")

        # 6. 导出环境
        print("\nExporting environment...")
        export_response = await stub.ExportEnv(
            pb2.ExportEnvRequest(
                workflow_id="wf-001",
                node_id="node-001",
            )
        )
        print(f"pyproject.toml:\n{export_response.pyproject_toml[:200]}...")

        # 7. 删除环境
        print("\nDeleting environment...")
        delete_response = await stub.DeleteEnv(
            pb2.DeleteEnvRequest(
                workflow_id="wf-001",
                node_id="node-001",
            )
        )
        print(f"Delete status: {delete_response.status}")

if __name__ == "__main__":
    asyncio.run(main())