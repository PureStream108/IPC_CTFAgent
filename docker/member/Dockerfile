# Member sandbox image (Seed.md 独立 Docker 沙箱).
# One per Member: languages + core CTF tooling. Extra tools are fetched during
# image build, matching ctf-agent's Dockerfile style; the local tools/ tree is
# not copied into the Docker build context or image.
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Languages/runtimes (Seed.md: Java/Python/Go/Rust/PHP/Nodejs/Maven).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl wget git build-essential \
        python3 python3-pip python3-venv \
        openjdk-17-jdk maven \
        golang-go rustc cargo \
        php-cli \
        nodejs npm ruby ruby-dev \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y --no-install-recommends \
        nmap netcat-openbsd binutils file binwalk steghide exiftool \
        gdb sqlmap unzip zip jq openssl \
        xxd bsdmainutils pngcheck imagemagick ffmpeg sox \
        tesseract-ocr tesseract-ocr-eng \
        libgmp-dev libmpfr-dev libfplll-dev libeigen3-dev libz3-dev \
        libmhash-dev libmcrypt-dev libjpeg-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# radare2 is not consistently available from Ubuntu 22.04 apt repos.
RUN git clone --depth=1 https://github.com/radareorg/radare2 /tmp/r2 \
    && cd /tmp/r2 && bash sys/install.sh --install \
    && ldconfig \
    && rm -rf /tmp/r2

# Python CTF libraries.
RUN pip3 install --no-cache-dir \
        TyphonBreaker pwntools pycryptodome sympy gmpy2 requests flask pyjwt z3-solver \
        Pillow pytesseract scipy numpy angr capstone unicorn ropgadget tqdm \
        fpylll volatility3 || true

# Extra CTF tools. These are intentionally cloned here instead of copied from
# IPC_CTFAgent/tools, so Docker builds do not upload the local tools directory.
RUN mkdir -p /opt/ipc-tools/tools \
    && git clone --depth=1 https://github.com/sqlmapproject/sqlmap.git /opt/ipc-tools/tools/sqlmap/sqlmapproject-sqlmap-03be590 \
    && git clone --depth=1 https://github.com/maurosoria/dirsearch.git /opt/ipc-tools/tools/dirsearch/dirsearch \
    && git clone --depth=1 https://github.com/commixproject/commix.git /opt/ipc-tools/tools/commix \
    && git clone --depth=1 https://github.com/Ekultek/WhatWaf.git /opt/ipc-tools/tools/WhatWaf \
    && git clone --depth=1 https://github.com/Marven11/Fenjing.git /opt/ipc-tools/tools/Fenjing \
    && git clone --depth=1 https://github.com/noraj/flask-session-cookie-manager.git /opt/ipc-tools/tools/flask-session-cookie/flask-session-cookie-manager-master \
    && git clone --depth=1 https://github.com/synacktiv/php_filter_chain_generator.git /opt/ipc-tools/tools/Php_chain/php_filter_chain_generator \
    && git clone --depth=1 https://github.com/chishaxie/BlindWaterMark.git /opt/ipc-tools/tools/BlindWaterMark \
    && git clone --depth=1 https://github.com/AntSwordProject/AntSword.git /opt/ipc-tools/tools/Antsword/antSword-master \
    && git clone --depth=1 https://github.com/X1r0z/JNDIMap.git /opt/ipc-tools/tools/JNDIMap/JNDIMap-main \
    && git clone --depth=1 https://github.com/frohoff/ysoserial.git /opt/ipc-tools/tools/ysoserial-master \
    && git clone --depth=1 https://github.com/RsaCtfTool/RsaCtfTool.git /opt/RsaCtfTool \
    && pip3 install --no-cache-dir /opt/ipc-tools/tools/Fenjing /opt/RsaCtfTool || true \
    && pip3 install --no-cache-dir -r /opt/ipc-tools/tools/dirsearch/dirsearch/requirements.txt || true \
    && gem install zsteg || true \
    && mkdir -p /workspace \
    && ln -s /opt/ipc-tools/tools /workspace/tools

WORKDIR /workspace

CMD ["sleep", "infinity"]
