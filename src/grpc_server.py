# src/grpc_server.py
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from grpc import aio

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger("grpc-server")


async def start_grpc_server(
        app: FastAPI,
        port: int = 50051,
        max_message_length: int = 100 * 1024 * 1024,
) -> aio.Server:
    """
    启动 gRPC 服务器。

    Args:
        app: FastAPI 应用实例（用于共享状态）
        port: gRPC 服务端口
        max_message_length: 最大消息大小 (默认 100MB)

    Returns:
        gRPC 服务器实例
    """
    from src.grpc_generated import env_manager_pb2_grpc as pb2_grpc
    from src.grpc_servicer import EnvManagerServicer

    # 服务器配置
    options = [
        ("grpc.max_send_message_length", max_message_length),
        ("grpc.max_receive_message_length", max_message_length),
        ("grpc.keepalive_time_ms", 30000),
        ("grpc.keepalive_timeout_ms", 10000),
    ]

    server = aio.server(options=options)

    # 注册服务 - 使用共享的 app 实例
    servicer = EnvManagerServicer(app)
    pb2_grpc.add_EnvManagerServiceServicer_to_server(servicer, server)

    # 可选：启用反射服务（用于调试）
    if os.getenv("GRPC_REFLECTION", "false").lower() == "true":
        try:
            from grpc_reflection.v1alpha import reflection

            SERVICE_NAMES = (
                pb2_grpc.DESCRIPTOR.services_by_name["EnvManagerService"].full_name,
                reflection.SERVICE_NAME,
            )
            reflection.enable_server_reflection(SERVICE_NAMES, server)
            logger.info("gRPC reflection enabled")
        except ImportError:
            logger.warning("grpc-reflection not installed, reflection disabled")

    # 绑定端口并启动
    listen_addr = f"[::]:{port}"
    server.add_insecure_port(listen_addr)
    await server.start()

    logger.info("gRPC server listening on %s", listen_addr)
    return server