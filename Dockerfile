FROM python:3.12-slim

WORKDIR /app

RUN sed -i 's|http://deb.debian.org/debian|https://mirrors.tuna.tsinghua.edu.cn/debian|g; s|http://deb.debian.org/debian-security|https://mirrors.tuna.tsinghua.edu.cn/debian-security|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git docker.io \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /app/pyproject.toml
COPY backend /app/backend
COPY scripts /app/scripts

RUN pip install --no-cache-dir -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple -e ".[docker]"

# Runtime data directories are mounted as named volumes by docker-compose.yml.
RUN mkdir -p /app/data /app/memory /app/wp /app/logs /app/projects /app/exports/logs /app/exports/Wp

ENV IPC_ROOT=/app
EXPOSE 8000

CMD ["uvicorn", "backend.server.app:app", "--host", "0.0.0.0", "--port", "8000"]
