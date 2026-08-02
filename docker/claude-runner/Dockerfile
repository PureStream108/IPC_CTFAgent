FROM node:22-bookworm-slim

RUN sed -i \
        -e 's|https://deb.debian.org/debian-security|http://mirrors.tuna.tsinghua.edu.cn/debian-security|g' \
        -e 's|https://deb.debian.org/debian|http://mirrors.tuna.tsinghua.edu.cn/debian|g' \
        -e 's|http://deb.debian.org/debian-security|http://mirrors.tuna.tsinghua.edu.cn/debian-security|g' \
        -e 's|http://deb.debian.org/debian|http://mirrors.tuna.tsinghua.edu.cn/debian|g' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl docker.io git ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && npm config set registry https://registry.npmmirror.com \
    && npm install --global @anthropic-ai/claude-code \
    && npm cache clean --force

COPY runner/server.js /opt/ipc-runner/server.js
COPY runner/host_exec /usr/local/bin/ipc-host-exec
RUN chmod 0755 /usr/local/bin/ipc-host-exec \
    && mkdir -p /home/node/.claude \
    && chown -R node:node /home/node/.claude

WORKDIR /data/ctf/IPC_CTFAgent
ENV HOME=/home/node
ENV IPC_WORKSPACE=/data/ctf/IPC_CTFAgent
EXPOSE 8600

USER node
CMD ["node", "/opt/ipc-runner/server.js"]
