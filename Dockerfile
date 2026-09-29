# ---------------------------------------------------------------------------
# 应用镜像（FastAPI + 后续 worker / scheduler / retention 共用同一镜像）
#
# 定位：DESIGN §10.1 的**可选隔离路径**。默认安装方式是在宿主机上
# `uv tool install`（或单文件包），Docker 不是前提。
#
# 镜像里**只有一个进程，没有数据库容器**：SQLite 文件在挂载的数据卷里。
#
# 目标运行时：DESIGN §8.1 选型的 Python 3.12（自带 SQLite ≥ 3.40，启动时会校验）。
# 依赖安装使用 uv；本仓库的 uv.lock 提交后，可把下面的 `uv sync` 换成
# `uv sync --frozen` 以保证可复现。
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH=/app/.venv/bin:$PATH \
    PC_DATA_ROOT=/data

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# 先只复制依赖声明，让依赖层可被 Docker 缓存复用
COPY pyproject.toml README.md ./
RUN uv sync --no-dev --no-install-project

COPY src ./src
# 迁移脚本随镜像分发：容器内也要能 `pc db apply`（migrations 属于可部署产物，
# 不是只在开发机上存在的东西）。注意 SQLite 版在 migrations/sqlite/ 下。
COPY migrations ./migrations
RUN uv sync --no-dev

# 数据根目录：库文件、媒体、缩略图、冷层、隔离区、备份都在它下面（DESIGN §10.2）
RUN mkdir -p /data/media /data/thumbs /data/cold /data/quarantine /data/backup /app/config
VOLUME ["/data"]

# 只监听容器内；对外暴露由 compose 限定在 127.0.0.1
EXPOSE 8080

# 存活检查走 /healthz（它刻意不校验数据库——数据库是本机文件，不是外部依赖）
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status == 200 else 1)"

# 骨架阶段唯一可启动的进程是 API；worker/sync/retention 的入口随对应模块加入
CMD ["uvicorn", "pc.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
