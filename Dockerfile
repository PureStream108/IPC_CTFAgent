FROM python:3.12-slim

WORKDIR /app

RUN sed -i 's|http://deb.debian.org/debian|https://mirrors.tuna.tsinghua.edu.cn/debian|g; s|http://deb.debian.org/debian-security|https://mirrors.tuna.tsinghua.edu.cn/debian-security|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git docker.io \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /app/pyproject.toml
COPY backend /app/backend

RUN pip install --no-cache-dir -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple -e ".[docker]"

# Runtime data dirs. Compose persists only wp/ and logs/ on the host; project
# state, memory DB, and workspaces stay in the app container layer.
RUN mkdir -p /app/data /app/memory /app/wp /app/logs /app/projects

ENV IPC_ROOT=/app
EXPOSE 8000

CMD ["uvicorn", "backend.server.app:app", "--host", "0.0.0.0", "--port", "8000"]
