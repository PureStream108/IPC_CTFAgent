FROM python:3.12-slim
RUN sed -i 's|http://deb.debian.org/debian|https://mirrors.tuna.tsinghua.edu.cn/debian|g; s|http://deb.debian.org/debian-security|https://mirrors.tuna.tsinghua.edu.cn/debian-security|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends chromium \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir playwright==1.50.0
ENV IPC_PLAYWRIGHT_EXECUTABLE=/usr/bin/chromium
CMD ["python", "/opt/playwright_e2e.py"]
