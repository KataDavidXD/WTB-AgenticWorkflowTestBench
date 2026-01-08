FROM python:3.11.9-slim-bookworm

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_CACHE_DIR=/data/uv_cache \
    UV_SYSTEM_PYTHON=1 \
    ENVS_BASE_PATH=/data/envs

# 安装 uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# 创建非 root 用户
RUN groupadd -r appuser && useradd -r -g appuser -d /app appuser

# 预创建目录并授权
RUN mkdir -p /app /data/envs /data/uv_cache && \
    chown -R appuser:appuser /app /data

WORKDIR /app

# 切换到非 root 用户
USER appuser

# 1. 复制依赖文件
COPY --chown=appuser:appuser pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-cache --link-mode copy

# 2. 复制源码
COPY --chown=appuser:appuser src ./src

# 暴露端口
EXPOSE 8435 50051

# 生产环境启动命令
CMD ["uv", "run", "uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8435"]
