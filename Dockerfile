# ---------------------------------------------------------------------------
# 应用镜像（FastAPI + 后续 worker / scheduler / retention 共用同一镜像）
#
# 目标运行时：DESIGN §8.1 选型的 Python 3.12。
# 依赖安装使用 uv；本仓库的 uv.lock 会在实现阶段首次 `uv sync` 后提交，
# 提交后请把下面的 `uv sync` 换成 `uv sync --frozen` 以保证可复现。
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH=/app/.venv/bin:$PATH

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# 先只复制依赖声明，让依赖层可被 Docker 缓存复用
COPY pyproject.toml README.md ./
RUN uv sync --no-dev --no-install-project

COPY src ./src
# 迁移脚本随镜像分发：这样容器内没有 psql 客户端也能 `pc db apply`
# （migrations 属于可部署产物，不是只在开发机上存在的东西）
COPY migrations ./migrations
RUN uv sync --no-dev

# 运行时目录（compose 会以卷覆盖挂载，见 docker-compose.yml）
RUN mkdir -p /data/media /data/cold /data/quarantine /data/backup /app/config

# 只监听容器内；对外暴露由 compose 限定在 127.0.0.1
EXPOSE 8080

# 骨架阶段唯一可启动的进程是 API；worker/sync/retention 的入口随对应模块加入
CMD ["uvicorn", "pc.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
