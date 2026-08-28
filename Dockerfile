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

# wsrx (WebSocket Reflector X CLI from XDSEC, static musl build) tunnels the
# ret2shell platform's ws://-proxied challenge ports to local TCP ports so
# Members can reach dynamic instances over the docker network. The ret2shell
# backend itself is only installed in this image because the tunnel
# subprocesses are owned by the backend process (Members just connect to
# ipc-app:<port>).
ARG WSRX_VERSION=0.6.1
RUN python -c "import urllib.request; urllib.request.urlretrieve('https://github.com/XDSEC/WebSocketReflectorX/releases/download/${WSRX_VERSION}/wsrx-cli-${WSRX_VERSION}-linux-musl-x86_64.tar.gz', '/tmp/wsrx.tar.gz')" \
    && tar -xzf /tmp/wsrx.tar.gz -C /tmp \
    && install -m 0755 /tmp/wsrx /usr/local/bin/wsrx \
    && rm -f /tmp/wsrx /tmp/wsrx.tar.gz \
    && wsrx --version

# /app/data is bind-mounted by docker-compose.yml and holds authentication,
# Operations Agent state and exports; the rest is ephemeral working state.
RUN mkdir -p /app/data/logs /app/data/Wp /app/data/memory \
    && mkdir -p /app/memory /app/wp /app/logs /app/projects

ENV IPC_ROOT=/app
EXPOSE 8000

CMD ["uvicorn", "backend.server.app:app", "--host", "0.0.0.0", "--port", "8000"]
