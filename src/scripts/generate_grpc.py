#!/usr/bin/env python
# scripts/generate_grpc.py
import os
import sys
import subprocess
from pathlib import Path


def main():
    project_root = Path(__file__).parent.parent
    proto_dir = project_root  / "protos"
    out_dir = project_root / "grpc_generated"
    proto_file = proto_dir / "env_manager.proto"

    out_dir.mkdir(parents=True, exist_ok=True)

    if not proto_file.exists():
        print(f"Error: Proto file not found: {proto_file}")
        sys.exit(1)

    try:
        import grpc_tools
        grpc_proto_path = Path(grpc_tools.__path__[0]) / "_proto"
    except ImportError:
        print("Installing grpcio-tools...")
        subprocess.run([sys.executable, "-m", "pip", "install", "grpcio-tools"], check=True)
        import grpc_tools
        grpc_proto_path = Path(grpc_tools.__path__[0]) / "_proto"

    cmd = [
        sys.executable, "-m", "grpc_tools.protoc",
        f"-I{proto_dir}",
        f"-I{grpc_proto_path}",
        f"--python_out={out_dir}",
        f"--grpc_python_out={out_dir}",
        f"--pyi_out={out_dir}",
        str(proto_file),
    ]

    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"Error: {result.stderr}")
        sys.exit(1)

    (out_dir / "__init__.py").touch()

    grpc_file = out_dir / "env_manager_pb2_grpc.py"
    if grpc_file.exists():
        content = grpc_file.read_text(encoding="utf-8")
        content = content.replace(
            "import env_manager_pb2 as env__manager__pb2",
            "from . import env_manager_pb2 as env__manager__pb2"
        )
        grpc_file.write_text(content, encoding="utf-8")

    print(f"✅ gRPC code generated in {out_dir}")


if __name__ == "__main__":
    main()