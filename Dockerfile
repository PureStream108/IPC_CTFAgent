FROM python:3.12-slim

WORKDIR /app

ARG TEMURIN_JRE_URL=https://api.adoptium.net/v3/binary/latest/21/ga/linux/x64/jre/hotspot/normal/eclipse
ARG GHIDRA_VERSION=12.1.2
ARG GHIDRA_DATE=20260605
ARG GHIDRA_TAG=Ghidra_12.1.2_build

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
        ca-certificates curl wget git docker.io unzip binutils chromium \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/java \
    && wget -q -O /tmp/jre21.tar.gz "$TEMURIN_JRE_URL" \
    && tar -xzf /tmp/jre21.tar.gz -C /opt/java --strip-components=1 \
    && rm -f /tmp/jre21.tar.gz

RUN wget -q -O /tmp/ghidra.zip "https://github.com/NationalSecurityAgency/ghidra/releases/download/${GHIDRA_TAG}/ghidra_${GHIDRA_VERSION}_PUBLIC_${GHIDRA_DATE}.zip" \
    && unzip -q /tmp/ghidra.zip -d /opt \
    && mv "/opt/ghidra_${GHIDRA_VERSION}_PUBLIC" /opt/ghidra \
    && rm -f /tmp/ghidra.zip

ENV JAVA_HOME=/opt/java
ENV PATH="${JAVA_HOME}/bin:${PATH}"
ENV IPC_CHROME_BIN=/usr/bin/chromium
ENV IPC_GHIDRA_HEADLESS=/opt/ghidra/support/analyzeHeadless

COPY pyproject.toml /app/pyproject.toml
COPY backend /app/backend
COPY scripts /app/scripts

RUN pip install --no-cache-dir -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple -e ".[docker]"

# Runtime data directories are mounted as named volumes by docker-compose.yml.
RUN mkdir -p /app/data /app/memory /app/wp /app/logs /app/projects /app/exports/logs /app/exports/Wp

ENV IPC_ROOT=/app
EXPOSE 8000

CMD ["uvicorn", "backend.server.app:app", "--host", "0.0.0.0", "--port", "8000"]
