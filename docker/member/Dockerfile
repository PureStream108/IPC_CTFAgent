FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN set -eux; \
    cp /etc/apt/sources.list /etc/apt/sources.list.orig; \
    rm -f /etc/apt/apt.conf.d/docker-clean; \
    printf '%s\n' \
      'Acquire::Retries "1";' \
      'Acquire::http::Timeout "20";' \
      'Acquire::https::Timeout "20";' \
      'Acquire::http::Pipeline-Depth "0";' \
      'Acquire::http::No-Cache "true";' \
      'Acquire::BrokenProxy "true";' \
      'Acquire::ForceIPv4 "true";' \
      > /etc/apt/apt.conf.d/80-ipc-retries; \
    printf '%s\n' \
      '#!/bin/sh' \
      'set -u' \
      '' \
      'if [ "$#" -eq 0 ]; then' \
      '  echo "apt-install-retry: no packages specified" >&2' \
      '  exit 2' \
      'fi' \
      '' \
      'mirrors="${APT_MIRRORS:-http://mirrors.ustc.edu.cn/ubuntu http://mirrors.aliyun.com/ubuntu http://mirrors.tuna.tsinghua.edu.cn/ubuntu http://archive.ubuntu.com/ubuntu}"' \
      'attempt=1' \
      '' \
      'set_mirror() {' \
      '  mirror="$1"' \
      '  cp /etc/apt/sources.list.orig /etc/apt/sources.list' \
      '  sed -i \' \
      '    -e "s|http://archive.ubuntu.com/ubuntu|${mirror}|g" \' \
      '    -e "s|http://security.ubuntu.com/ubuntu|${mirror}|g" \' \
      '    /etc/apt/sources.list' \
      '}' \
      '' \
      'for mirror in $mirrors; do' \
      '  set_mirror "$mirror"' \
      '  for retry in 1 2; do' \
      '    echo "apt attempt ${attempt}: ${mirror}" >&2' \
      '    rm -rf /var/lib/apt/lists/*' \
      '    if apt-get update; then' \
      '      if apt-get install -y --no-install-recommends --fix-missing "$@"; then' \
      '        apt-get clean' \
      '        rm -rf /var/lib/apt/lists/*' \
      '        exit 0' \
      '      fi' \
      '      echo "apt attempt ${attempt}: running apt-get -f install" >&2' \
      '      apt-get install -y --no-install-recommends -f || true' \
      '      if apt-get install -y --no-install-recommends "$@"; then' \
      '        apt-get clean' \
      '        rm -rf /var/lib/apt/lists/*' \
      '        exit 0' \
      '      fi' \
      '    fi' \
      '    delay=$((retry * 5))' \
      '    echo "apt attempt ${attempt} failed; retrying in ${delay}s" >&2' \
      '    sleep "${delay}"' \
      '    attempt=$((attempt + 1))' \
      '  done' \
      'done' \
      '' \
      'exit 1' \
      > /usr/local/bin/apt-install-retry; \
    chmod +x /usr/local/bin/apt-install-retry

# Languages/runtimes (Seed.md: Java/Python/Go/Rust/PHP/Nodejs/Maven).
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked apt-install-retry \
        ca-certificates curl wget git build-essential \
        python3 python3-pip python3-venv \
        openjdk-17-jdk maven \
        golang-go rustc cargo \
        php-cli \
        nodejs npm ruby ruby-dev \
    && rm -rf /var/lib/apt/lists/*

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked apt-install-retry \
        nmap netcat-openbsd binutils file binwalk steghide exiftool \
        gdb sqlmap unzip zip jq openssl checksec \
        ripgrep xxd bsdmainutils pngcheck imagemagick ffmpeg sox \
        tesseract-ocr tesseract-ocr-eng \
        sagemath cmake pkg-config python3-dev libhwloc-dev \
        libgmp-dev libmpfr-dev libfplll-dev libeigen3-dev libz3-dev \
        libmhash-dev libmcrypt-dev libjpeg-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    mkdir -p /opt/cado-nfs; \
    curl -fL --retry 5 --retry-all-errors --retry-delay 5 -o /tmp/cado-nfs.tar.gz https://codeload.github.com/sethtroisi/cado-nfs/tar.gz/refs/heads/master; \
    tar -xzf /tmp/cado-nfs.tar.gz -C /opt/cado-nfs --strip-components=1; \
    rm -f /tmp/cado-nfs.tar.gz; \
    grep -rl -- '-Werror' /opt/cado-nfs | xargs -r sed -i 's/-Werror//g'; \
    sed -i '/#pragma GCC diagnostic error/d' /opt/cado-nfs/cado.h; \
    sed -i '1i #include <limits>' /opt/cado-nfs/sieve/logapprox.cpp; \
    cd /opt/cado-nfs \
    && CFLAGS="-O2 -fcommon -Wno-array-parameter -Wno-error=array-parameter -Wno-error" CXXFLAGS="-O2 -Wno-array-parameter -Wno-error=array-parameter -Wno-error" make -j2 \
    && printf '#!/bin/sh\nexec python3 /opt/cado-nfs/cado-nfs.py "$@"\n' > /usr/local/bin/cado-nfs \
    && chmod +x /usr/local/bin/cado-nfs

# radare2 is not consistently available from Ubuntu 22.04 apt repos.
RUN git clone --depth=1 https://github.com/radareorg/radare2 /tmp/r2 \
    && cd /tmp/r2 && bash sys/install.sh --install \
    && ldconfig \
    && rm -rf /tmp/r2

ENV PIP_INDEX_URL=https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple \
    PIP_ROOT_USER_ACTION=ignore

RUN pip3 install --no-cache-dir \
        TyphonBreaker pwntools pycryptodome sympy gmpy2 requests flask pyjwt z3-solver \
        Pillow pytesseract scipy numpy angr capstone unicorn ropgadget tqdm \
        fpylll volatility3 || true

RUN pip3 install --no-cache-dir \
        filelock fsspec jinja2 networkx sympy typing-extensions \
    && pip3 install --no-cache-dir --no-deps \
        --index-url https://download.pytorch.org/whl/cpu \
        torch \
    && pip3 install --no-cache-dir keras h5py

# Extra CTF tools. These are intentionally cloned here instead of copied from
# IPC_CTFAgent/tools, so Docker builds do not upload the local tools directory.
RUN mkdir -p /opt/ipc-tools/tools \
    && git clone --depth=1 https://github.com/sqlmapproject/sqlmap.git /opt/ipc-tools/tools/sqlmap/sqlmapproject-sqlmap-03be590 \
    && git clone --depth=1 https://github.com/maurosoria/dirsearch.git /opt/ipc-tools/tools/dirsearch/dirsearch \
    && git clone --depth=1 https://github.com/commixproject/commix.git /opt/ipc-tools/tools/commix \
    && git clone --depth=1 https://github.com/Ekultek/WhatWaf.git /opt/ipc-tools/tools/WhatWaf \
    && git clone --depth=1 https://github.com/Marven11/Fenjing.git /opt/ipc-tools/tools/Fenjing \
    && mkdir -p /opt/ipc-tools/tools/RSASIGN /opt/ipc-tools/tools/shiro550 \
    && git clone --depth=1 --branch release https://github.com/silentsignal/rsa_sign2n.git /opt/ipc-tools/tools/RSASIGN/rsa_sign2n-release \
    && git clone --depth=1 https://github.com/WiIs0n/Flask-cookie-generation-based-on-PIN-code.git /opt/ipc-tools/tools/Flask-cookie-generation-based-on-PIN-code-main \
    && git clone --depth=1 https://github.com/noraj/flask-session-cookie-manager.git /opt/ipc-tools/tools/flask-session-cookie/flask-session-cookie-manager-master \
    && git clone --depth=1 https://github.com/synacktiv/php_filter_chain_generator.git /opt/ipc-tools/tools/Php_chain/php_filter_chain_generator \
    && git clone --depth=1 https://github.com/chishaxie/BlindWaterMark.git /opt/ipc-tools/tools/BlindWaterMark \
    && git clone --depth=1 https://github.com/AntSwordProject/AntSword.git /opt/ipc-tools/tools/Antsword/antSword-master \
    && git clone --depth=1 https://github.com/X1r0z/JNDIMap.git /opt/ipc-tools/tools/JNDIMap/JNDIMap-main \
    && git clone --depth=1 https://github.com/frohoff/ysoserial.git /opt/ipc-tools/tools/ysoserial-master \
    && git clone --depth=1 https://github.com/RsaCtfTool/RsaCtfTool.git /opt/RsaCtfTool \
    && wget -q -O /opt/ipc-tools/tools/shiro550/shiro_attack-5.1.1-zulu-8-jfx.jar https://github.com/SummerSec/ShiroAttack2/releases/download/v5.1.1/shiro_attack-5.1.1-zulu-8-jfx.jar \
    && (pip3 install --no-cache-dir /opt/ipc-tools/tools/Fenjing /opt/RsaCtfTool || true) \
    && (pip3 install --no-cache-dir -r /opt/ipc-tools/tools/dirsearch/dirsearch/requirements.txt || true) \
    && gem install --no-document one_gadget \
    && (gem install zsteg || true) \
    && mkdir -p /workspace \
    && ln -s /opt/ipc-tools/tools /workspace/tools

COPY backend/tools/member_tools.txt /tools.txt

RUN sed -i 's/from fractions import gcd/from math import gcd/' /opt/cado-nfs/scripts/cadofactor/cadotask.py \
    && sed -i 's/origin is "localhost"/origin == "localhost"/' /opt/cado-nfs/scripts/cadofactor/wuserver.py \
    && printf '#!/bin/sh\nexport PYTHONPATH=/opt/cado-nfs/scripts/cadofactor${PYTHONPATH:+:$PYTHONPATH}\nexec python3 /opt/cado-nfs/cado-nfs.py "$@"\n' > /usr/local/bin/cado-nfs \
    && chmod +x /usr/local/bin/cado-nfs

WORKDIR /workspace

CMD ["sleep", "infinity"]
