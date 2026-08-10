FROM python:3.12-slim

WORKDIR /app

RUN sed -i 's|http://deb.debian.org/debian|https://mirrors.tuna.tsinghua.edu.cn/debian|g; s|http://deb.debian.org/debian-security|https://mirrors.tuna.tsinghua.edu.cn/debian-security|g' /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' \
        'Acquire::Retries "5";' \
        'Acquire::http::Timeout "30";' \
        'Acquire::https::Timeout "30";' \
        'Acquire::http::Pipeline-Depth "0";' \
        'Acquire::http::No-Cache "true";' \
        'Acquire::BrokenProxy "true";' \
        > /etc/apt/apt.conf.d/80-ipc-retries \
    && apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates docker.io \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /app/pyproject.toml
COPY alembic.ini /app/alembic.ini
COPY backend /app/backend
COPY frontend /app/frontend
COPY scripts /app/scripts

RUN pip install --no-cache-dir -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple -e ".[docker]"

# /app/data is bind-mounted by docker-compose.yml. PostgreSQL owns runtime
# facts; filesystem workspaces and generated files share one Artifact tree.
RUN mkdir -p /app/data/artifacts/projects \
    /app/data/artifacts/writeups \
    /app/data/artifacts/logs \
    /app/data/artifacts/exports/logs \
    /app/data/artifacts/exports/writeups \
    /app/data/artifacts/exports/memory

ENV IPC_ROOT=/app
ENV IPC_ARTIFACT_ROOT=/app/data/artifacts
EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && exec uvicorn backend.server.app:app --host 0.0.0.0 --port 8000"]
