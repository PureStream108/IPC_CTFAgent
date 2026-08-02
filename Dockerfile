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
COPY backend /app/backend
COPY frontend /app/frontend
COPY scripts /app/scripts

RUN pip install --no-cache-dir -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple -e ".[docker]"

# /app/data is bind-mounted by docker-compose.yml and holds authentication,
# Operations Agent state and exports; the rest is ephemeral working state.
RUN mkdir -p /app/data/logs /app/data/Wp /app/data/memory \
    && mkdir -p /app/memory /app/wp /app/logs /app/projects

ENV IPC_ROOT=/app
EXPOSE 8000

CMD ["uvicorn", "backend.server.app:app", "--host", "0.0.0.0", "--port", "8000"]
