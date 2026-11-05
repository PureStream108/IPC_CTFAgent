ARG IPC_TASK_BASE=kalilinux/kali-rolling:latest
FROM ${IPC_TASK_BASE}

ENV DEBIAN_FRONTEND=noninteractive
ARG GITHUB_MIRROR=https://github.com
ARG GITHUB_RELEASE_PROXY=https://gh-proxy.com/

RUN set -eux; \
    if [ -f /etc/apt/sources.list ]; then cp /etc/apt/sources.list /etc/apt/sources.list.orig; fi; \
    if [ -f /etc/apt/sources.list.d/kali.sources ]; then cp /etc/apt/sources.list.d/kali.sources /etc/apt/sources.list.d/kali.sources.orig; fi; \
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
      'if [ -f /etc/apt/sources.list.d/kali.sources ]; then' \
      '  mirrors="${APT_MIRRORS:-http://mirrors.tuna.tsinghua.edu.cn/kali http://mirrors.ustc.edu.cn/kali http://http.kali.org/kali http://kali.download/kali}"' \
      'else' \
      '  mirrors="${APT_MIRRORS:-http://mirrors.ustc.edu.cn/ubuntu http://mirrors.aliyun.com/ubuntu http://mirrors.tuna.tsinghua.edu.cn/ubuntu http://archive.ubuntu.com/ubuntu}"' \
      'fi' \
      'attempt=1' \
      '' \
      'set_mirror() {' \
      '  mirror="$1"' \
      '  if [ -f /etc/apt/sources.list.orig ]; then' \
      '    cp /etc/apt/sources.list.orig /etc/apt/sources.list' \
      '    sed -i -e "s|http://archive.ubuntu.com/ubuntu|${mirror}|g" -e "s|http://security.ubuntu.com/ubuntu|${mirror}|g" /etc/apt/sources.list' \
      '  fi' \
      '  if [ -f /etc/apt/sources.list.d/kali.sources.orig ]; then' \
      '    cp /etc/apt/sources.list.d/kali.sources.orig /etc/apt/sources.list.d/kali.sources' \
      '    sed -i -E "s|^URIs:.*|URIs: ${mirror}|" /etc/apt/sources.list.d/kali.sources' \
      '  fi' \
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

# Standard, reproducible runtime set. Node.js is installed from the official
# release below. Go and sagemath are deliberately not provisioned: they are the
# two largest optional stacks (~0.43 GB and ~2.8 GB) and a challenge that needs
# them can install them at solve time. Maven is a binary tarball installed after
# the JDK layer so it reuses /opt/java21 instead of pulling a second JDK.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked apt-install-retry \
        ca-certificates curl wget git build-essential gcc g++ make xz-utils \
        python3 python3-pip python3-venv \
        php-cli \
        ruby ruby-dev \
    && rm -rf /var/lib/apt/lists/* /usr/share/doc/* /usr/share/man/* /usr/share/locale/*

# GitHub's git endpoint is blocked on some build networks while codeload is
# reachable. Fetch a repository tarball and unpack it into a clean destination.
RUN printf '%s\n' \
      '#!/bin/sh' \
      'set -eu' \
      'repo="$1"' \
      'ref="$2"' \
      'dest="$3"' \
      'url="https://codeload.github.com/${repo}/tar.gz/refs/${ref}"' \
      'proxy="${GITHUB_RELEASE_PROXY:-}"' \
      'n=1' \
      'while [ "$n" -le 5 ]; do' \
      '  for candidate in "${proxy}${url}" "$url"; do' \
      '    rm -f /tmp/github-tar.gz' \
      '    if curl -fL --retry 3 --retry-all-errors --retry-delay 3 -o /tmp/github-tar.gz "$candidate"; then' \
      '      rm -rf "$dest"' \
      '      mkdir -p "$dest"' \
      '      tar -xzf /tmp/github-tar.gz -C "$dest" --strip-components=1' \
      '      rm -f /tmp/github-tar.gz' \
      '      exit 0' \
      '    fi' \
      '  done' \
      '  sleep $((n * 5))' \
      '  n=$((n + 1))' \
      'done' \
      'exit 1' \
      > /usr/local/bin/github-tar; \
    chmod 0755 /usr/local/bin/github-tar

# Release-asset counterpart to github-tar. Checksums are verified only when the
# release publishes them: a hard-coded digest that is wrong once would make the
# image unbuildable for everyone, while a missing checksum must not be silently
# ignored. Every caller still executes the binary as a version check below.
RUN printf '%s\n' \
      '#!/bin/sh' \
      'set -eu' \
      'repo="$1"' \
      'tag="$2"' \
      'asset="$3"' \
      'dest="$4"' \
      'sums="${5:-}"' \
      'proxy="${GITHUB_RELEASE_PROXY:-}"' \
      'base_url() { printf "%s/%s/releases/download/%s/%s" "$1" "$repo" "$tag" "$2"; }' \
      'fetched=0' \
      'for base in "${proxy}${GITHUB_MIRROR}" "${GITHUB_MIRROR}"; do' \
      '  if curl -fL --retry 3 --retry-all-errors --retry-delay 3 -o "$dest" "$(base_url "$base" "$asset")"; then' \
      '    fetched=1; break' \
      '  fi' \
      'done' \
      'test "$fetched" = 1' \
      'if [ -n "$sums" ]; then' \
      '  for base in "${proxy}${GITHUB_MIRROR}" "${GITHUB_MIRROR}"; do' \
      '    if curl -fsSL --retry 2 -o /tmp/github-release-sums "$(base_url "$base" "$sums")"; then' \
      '      expected=$(grep -E "  $asset\$" /tmp/github-release-sums | head -1 | awk "{print \$1}")' \
      '      if [ -n "$expected" ]; then' \
      '        actual=$(sha256sum "$dest" | awk "{print \$1}")' \
      '        if [ "$expected" != "$actual" ]; then' \
      '          echo "checksum mismatch for $asset" >&2; exit 1' \
      '        fi' \
      '      fi' \
      '      rm -f /tmp/github-release-sums' \
      '      break' \
      '    fi' \
      '  done' \
      'fi' \
      > /usr/local/bin/github-release; \
    chmod 0755 /usr/local/bin/github-release

# Reclaim any stale tooling inherited from a custom IPC_TASK_BASE. This runs
# before the new versions are installed so an old upx-ucl cannot shadow UPX.
RUN set -eux; \
    apt-get purge -y upx-ucl rustc cargo nodejs npm || true; \
    rm -f /usr/bin/upx /usr/local/bin/upx; \
    rm -rf /opt/ghidra /opt/ghidra_* /root/.cargo /root/.rustup; \
    rm -rf /opt/ipc-tools/tools/Fenjing /opt/ipc-tools/tools/JNDIMap \
           /opt/ipc-tools/tools/shiro550 /opt/ipc-tools/tools/Php_chain; \
    pip3 uninstall -y pyghidra playwright TyphonBreaker || true

# Debian's tshark package asks whether dumpcap may capture as non-root. The
# task container runs as root, and a debconf prompt would otherwise abort the
# non-interactive build.
RUN printf '%s\n' 'wireshark-common wireshark-common/install-setuid boolean false' \
        | debconf-set-selections

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked apt-install-retry \
        nmap netcat-openbsd binutils file binwalk steghide libimage-exiftool-perl \
        gdb gdbserver sqlmap unzip zip jq openssl checksec \
        ripgrep xxd bsdmainutils pngcheck imagemagick ffmpeg sox \
        tesseract-ocr tesseract-ocr-eng \
        tshark tcpdump strace socat musl-tools foremost elfutils \
        upx-ucl radare2 cmake pkg-config python3-dev \
        python3-onnx python3-onnxruntime \
        libgmp-dev libmpfr-dev libfplll-dev libjpeg-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/* /usr/share/doc/* /usr/share/man/* /usr/share/locale/*

# Kali's upx-ucl is 4.x; the Ubuntu 22.04 package was 3.96, which is why the
# image used to build UPX from an upstream release. Verify the distro build.
RUN upx --version 2>&1 | head -1 | grep -Eiq 'upx 4\.' && r2 -v

RUN set -eux; \
    mkdir -p /opt/cado-nfs; \
    curl -fL --retry 5 --retry-all-errors --retry-delay 5 -o /tmp/cado-nfs.tar.gz https://codeload.github.com/sethtroisi/cado-nfs/tar.gz/refs/heads/master; \
    tar -xzf /tmp/cado-nfs.tar.gz -C /opt/cado-nfs --strip-components=1; \
    rm -f /tmp/cado-nfs.tar.gz; \
    grep -rl -- '-Werror' /opt/cado-nfs | xargs -r sed -i 's/-Werror//g'; \
    find /opt/cado-nfs -name CMakeLists.txt -exec sed -i -E 's/cmake_minimum_required\(VERSION (2\.[0-9]+|3\.0)([^)]*)\)/cmake_minimum_required(VERSION 3.5\2)/' {} +; \
    sed -i 's|// continue;|/* continue; */|' /opt/cado-nfs/gf2x/lowlevel/gen_bb_mul_code.c; \
    sed -i '/#pragma GCC diagnostic error/d' /opt/cado-nfs/cado.h; \
    sed -i '1i #include <limits>' /opt/cado-nfs/sieve/logapprox.cpp; \
    cd /opt/cado-nfs \
    && CFLAGS="-O2 -fcommon -Wno-array-parameter -Wno-error=array-parameter -Wno-error" CXXFLAGS="-O2 -Wno-array-parameter -Wno-error=array-parameter -Wno-error" make -j2 \
    && find /opt/cado-nfs -type f -name '*.o' -delete \
    && printf '#!/bin/sh\nexec python3 /opt/cado-nfs/cado-nfs.py "$@"\n' > /usr/local/bin/cado-nfs \
    && chmod +x /usr/local/bin/cado-nfs

# Official Node 22 rather than the distro's nodejs package. The checksum makes
# the build fail closed when a mirror serves a different artifact.
ARG NODE_VERSION=22.23.4
ARG NODE_SHA256=1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce
ARG NODE_MIRROR=https://nodejs.org/dist
RUN set -eux; \
    curl -fL --retry 5 --retry-all-errors --retry-delay 5 \
      -o /tmp/node.tar.xz "${NODE_MIRROR}/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz"; \
    echo "${NODE_SHA256}  /tmp/node.tar.xz" | sha256sum -c -; \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner; \
    rm -f /tmp/node.tar.xz; \
    test "$(node --version)" = "v${NODE_VERSION}"; \
    node --version; \
    npm --version

ENV PIP_INDEX_URL=https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple \
    PIP_ROOT_USER_ACTION=ignore \
    PIP_BREAK_SYSTEM_PACKAGES=1

# Core CTF Python packages. PyJWT owns the "jwt" import; jwcrypto adds JWS/JWE
# support without installing a second module under the same name. The task
# image intentionally has no Python Playwright dependency: browser automation
# goes through @playwright/cli below.
RUN pip3 install --no-cache-dir --ignore-installed \
        pyjwt jwcrypto==1.5.6 scapy dpkt \
        pwntools pycryptodome sympy gmpy2 requests flask z3-solver \
        Pillow pytesseract scipy numpy angr capstone unicorn ropgadget tqdm \
        fpylll volatility3

RUN pip3 install --no-cache-dir \
        filelock fsspec jinja2 "networkx>=3.2,<4" sympy typing-extensions

# The CPU torch wheel is ~210 MB and this build network drops long transfers
# (observed: IncompleteRead at 138/209 MB). pip retries a failed request but
# not a broken body, so retry the install itself and keep the cache clean.
ARG TORCH_CPU_INDEX=https://download.pytorch.org/whl/cpu
RUN set -eux; \
    attempt=1; \
    while [ "$attempt" -le 4 ]; do \
      if pip3 install --no-cache-dir --no-deps --retries 10 --timeout 180 \
          --index-url "${TORCH_CPU_INDEX}" torch; then \
        break; \
      fi; \
      test "$attempt" -lt 4; \
      attempt=$((attempt + 1)); \
      sleep $((attempt * 10)); \
    done

RUN pip3 install --no-cache-dir keras h5py

# Extra CTF tools. These are intentionally cloned here instead of copied from
# IPC_CTFAgent/tools, so Docker builds do not upload the local tools directory.
RUN mkdir -p /opt/ipc-tools/tools \
    && github-tar WiIs0n/Flask-cookie-generation-based-on-PIN-code heads/main /opt/ipc-tools/tools/Flask-cookie-generation-based-on-PIN-code-main \
    && github-tar frohoff/ysoserial heads/master /opt/ipc-tools/tools/ysoserial-master \
    && github-tar commixproject/commix heads/master /opt/ipc-tools/tools/commix \
    && gem install --no-document one_gadget \
    && (gem install zsteg || true) \
    && mkdir -p /workspace \
    && ln -s /opt/ipc-tools/tools /workspace/tools

# Scanning, web enumeration and credential-audit CLI set. Kali packages all of
# these, so the image no longer builds ffuf/feroxbuster/nuclei/gobuster/wafw00f/
# wfuzz/enum4linux from upstream release assets: the distro packages are
# maintained, carry the same CLIs, and remove seven download/checksum steps.
# commix stays an upstream checkout: Kali's commix Depends: metasploit-framework,
# which would add ~270 MB and an entire exploitation suite to this image.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked apt-install-retry \
        ncat masscan whatweb nikto smbmap onesixtyone \
        dnsmap bind9-dnsutils ldap-utils snmp nfs-common \
        dirb sslscan ffuf feroxbuster nuclei gobuster \
        hydra medusa ncrack john hashcat sshpass \
        perl smbclient enum4linux wafw00f wfuzz \
        ca-certificates curl unzip python3-pip \
    && rm -rf /var/lib/apt/lists/* /usr/share/doc/* /usr/share/man/* /usr/share/locale/*

ARG NUCLEI_TEMPLATES_TAG=10.1.0

# The template tree is baked into the image. Without it nuclei would fetch
# templates on first run, which turns a solve into a network dependency.
# The directory must be a real one: nuclei does not resolve a symlinked
# $HOME/nuclei-templates (distro and upstream builds) and reports "no templates found".
RUN set -eux; \
    github-tar projectdiscovery/nuclei-templates "tags/v${NUCLEI_TEMPLATES_TAG}" /root/nuclei-templates; \
    test -d /root/nuclei-templates/http; \
    test "$(find /root/nuclei-templates -name '*.yaml' | wc -l)" -gt 1000; \
    nuclei -duc -tl -t /root/nuclei-templates > /tmp/nuclei-templates.txt 2>/dev/null; \
    test "$(wc -l < /tmp/nuclei-templates.txt)" -gt 1000; \
    wc -l /tmp/nuclei-templates.txt; \
    rm -f /tmp/nuclei-templates.txt

# raw.githubusercontent.com drops large transfers on some build networks
# (observed: TLS stream truncated repeatedly on the 50MB rockyou archive, which
# killed the whole image build). Try the release proxy first, then the direct
# URL, and let curl treat a stalled transfer as a retryable error.
RUN printf '%s\n' \
      '#!/bin/sh' \
      'set -eu' \
      'url="$1"' \
      'dest="$2"' \
      'proxy="${GITHUB_RELEASE_PROXY:-}"' \
      'attempt=1' \
      'while [ "$attempt" -le 2 ]; do' \
      '  for candidate in "${proxy}${url}" "$url"; do' \
      '    if curl -fL --retry 4 --retry-all-errors --retry-delay 5 \' \
      '        --speed-limit 1024 --speed-time 60 \' \
      '        -o "$dest" "$candidate"; then' \
      '      exit 0' \
      '    fi' \
      '  done' \
      '  sleep $((attempt * 5))' \
      '  attempt=$((attempt + 1))' \
      'done' \
      'exit 1' \
      > /usr/local/bin/github-raw; \
    chmod 0755 /usr/local/bin/github-raw

# Wordlists stay in one shared location and are exposed under /workspace.
# SecLists is pinned to the 2026.1 tag; only the useful directories are checked
# out. rockyou comes from the same tag's Leaked-Databases archive, and every
# download goes through github-raw because a bare curl to that host failed the
# whole build on this network.
RUN set -eux; \
    base="https://raw.githubusercontent.com/danielmiessler/SecLists/2026.1"; \
    mkdir -p /opt/ipc-tools/wordlists/SecLists; \
    for path in \
      "Discovery/Web-Content/common.txt" \
      "Discovery/Web-Content/raft-medium-directories.txt" \
      "Passwords/Common-Credentials/xato-net-10-million-passwords-100000.txt" \
      "Usernames/top-usernames-shortlist.txt"; do \
        mkdir -p "/opt/ipc-tools/wordlists/SecLists/$(dirname "$path")"; \
        github-raw "$base/$path" "/opt/ipc-tools/wordlists/SecLists/$path"; \
    done; \
    github-raw "$base/Passwords/Leaked-Databases/rockyou.txt.tar.gz" /tmp/rockyou.tar.gz; \
    tar -xzf /tmp/rockyou.tar.gz -C /opt/ipc-tools/wordlists; \
    rm -f /tmp/rockyou.tar.gz; \
    test -s /opt/ipc-tools/wordlists/rockyou.txt; \
    test -s /opt/ipc-tools/wordlists/SecLists/Discovery/Web-Content/common.txt; \
    ln -sfn /opt/ipc-tools/wordlists /workspace/wordlists

COPY backend/tools/member_tools.txt /tools.txt

RUN sed -i 's/from fractions import gcd/from math import gcd/' /opt/cado-nfs/scripts/cadofactor/cadotask.py \
    && sed -i 's/origin is "localhost"/origin == "localhost"/' /opt/cado-nfs/scripts/cadofactor/wuserver.py \
    && printf '#!/bin/sh\nexport PYTHONPATH=/opt/cado-nfs/scripts/cadofactor${PYTHONPATH:+:$PYTHONPATH}\nexec python3 /opt/cado-nfs/cado-nfs.py "$@"\n' > /usr/local/bin/cado-nfs \
    && chmod +x /usr/local/bin/cado-nfs

# Temurin 21 remains for Java challenges and Maven. Ghidra/PyGhidra are gone;
# reverse engineering uses radare2, gdb, objdump/readelf and elfutils instead.
ARG TEMURIN_JDK_URL=https://api.adoptium.net/v3/binary/latest/21/ga/linux/x64/jdk/hotspot/normal/eclipse
# The Adoptium API answers with a 307 to a ~200MB GitHub asset. Both hops drop
# on this network (wget exit 4 on the transfer, curl "connection refused" after
# the redirect), which failed the whole build. Try the API, then the resolved
# release asset directly, then the same asset through the release proxy; resume
# partial downloads instead of restarting 200MB from zero.
ARG TEMURIN_JDK_FALLBACK=https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.13%2B9/OpenJDK21U-jdk_x64_linux_hotspot_21.0.13_9.tar.gz
RUN set -eux; \
    if [ -x /opt/java21/bin/java ]; then \
      echo "reusing the Java 21 runtime from the task base image"; \
    else \
      mkdir -p /opt/java21; \
      rm -f /tmp/jdk21.tar.gz; \
      downloaded=0; \
      for candidate in "$TEMURIN_JDK_URL" "$TEMURIN_JDK_FALLBACK" \
                       "${GITHUB_RELEASE_PROXY}${TEMURIN_JDK_FALLBACK}"; do \
        n=1; \
        while [ "$n" -le 4 ]; do \
          if curl -fL --retry 2 --retry-all-errors --retry-delay 5 \
               --speed-limit 1024 --speed-time 60 \
               -C - -o /tmp/jdk21.tar.gz "$candidate"; then \
            downloaded=1; break; \
          fi; \
          sleep $((n * 5)); \
          n=$((n + 1)); \
        done; \
        if [ "$downloaded" = 1 ]; then break; fi; \
      done; \
      test "$downloaded" = 1; \
      test -s /tmp/jdk21.tar.gz; \
      tar -xzf /tmp/jdk21.tar.gz -C /opt/java21 --strip-components=1; \
      rm -f /tmp/jdk21.tar.gz; \
    fi
ENV JAVA_HOME=/opt/java21
ENV PATH="/opt/java21/bin:${PATH}"

# Maven as a plain binary tarball. The distro package depends on default-jdk and
# would pull a second JDK (~0.24 GB) next to Temurin 21; this build reuses
# JAVA_HOME=/opt/java21 and verifies the upstream SHA-512.
ARG MAVEN_VERSION=3.9.9
RUN set -eux; \
    base="https://archive.apache.org/dist/maven/maven-3/${MAVEN_VERSION}/binaries"; \
    curl -fL --retry 5 --retry-all-errors --retry-delay 5 -o /tmp/maven.tar.gz \
      "${base}/apache-maven-${MAVEN_VERSION}-bin.tar.gz"; \
    curl -fL --retry 5 --retry-all-errors --retry-delay 5 -o /tmp/maven.tar.gz.sha512 \
      "${base}/apache-maven-${MAVEN_VERSION}-bin.tar.gz.sha512"; \
    expected="$(cut -d' ' -f1 /tmp/maven.tar.gz.sha512)"; \
    echo "${expected}  /tmp/maven.tar.gz" | sha512sum -c -; \
    mkdir -p /opt/maven; \
    tar -xzf /tmp/maven.tar.gz -C /opt/maven --strip-components=1; \
    rm -f /tmp/maven.tar.gz /tmp/maven.tar.gz.sha512; \
    JAVA_HOME=/opt/java21 /opt/maven/bin/mvn -v
ENV MAVEN_HOME=/opt/maven
ENV PATH="/opt/maven/bin:${PATH}"

# Playwright CLI: installed outside the Python environment and shared through
# /opt/ipc-tools/tools. The browser is only started when a Member invokes the
# CLI; there is no resident Chrome and no Browser MCP.
ARG PLAYWRIGHT_CLI_VERSION=0.1.22
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers
RUN set -eux; \
    mkdir -p /opt/playwright-cli; \
    cd /opt/playwright-cli; \
    npm init -y >/dev/null; \
    npm install --no-audit --no-fund "@playwright/cli@${PLAYWRIGHT_CLI_VERSION}"; \
    (node /opt/playwright-cli/node_modules/playwright/cli.js install --with-deps --only-shell chromium \
       || node /opt/playwright-cli/node_modules/playwright/cli.js install --only-shell chromium); \
    find /opt/playwright-browsers -maxdepth 1 -type d -name 'chromium-*' -exec rm -rf {} +; \
    mkdir -p /opt/ipc-tools/tools/playwright-cli; \
    printf '%s\n' '#!/bin/sh' \
      'export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/opt/playwright-browsers}"' \
      'exec /opt/playwright-cli/node_modules/.bin/playwright-cli "$@"' \
      > /opt/ipc-tools/tools/playwright-cli/playwright-cli; \
    chmod 0755 /opt/ipc-tools/tools/playwright-cli/playwright-cli; \
    ln -sfn /opt/ipc-tools/tools/playwright-cli/playwright-cli /usr/local/bin/playwright-cli; \
    test -x /workspace/tools/playwright-cli/playwright-cli; \
    chrome_bin="$(find /opt/playwright-browsers -path '*/chrome-headless-shell-linux64/chrome-headless-shell' -type f | head -1)"; \
    test -x "$chrome_bin"; \
    mkdir -p /opt/google/chrome; \
    printf '#!/bin/sh\nexec %s --no-sandbox "$@"\n' "$chrome_bin" > /opt/google/chrome/chrome; \
    chmod 0755 /opt/google/chrome/chrome; \
    playwright-cli --help >/dev/null

# The distro's system packaging library can be too old for current setuptools and
# silently produce empty UNKNOWN wheels, so use an explicit build toolchain.
RUN pip3 install --no-cache-dir --ignore-installed --upgrade "setuptools>=68" "packaging>=24" wheel \
    && curl -fL --retry 5 --retry-all-errors --retry-delay 5 \
        -o /tmp/r2pipe.tar.gz \
        "https://codeload.github.com/radareorg/radare2-r2pipe/tar.gz/d87a5759b1c1a522f0f7e59baa938f80415fd038" \
    && mkdir -p /tmp/r2pipe \
    && tar -xzf /tmp/r2pipe.tar.gz -C /tmp/r2pipe --strip-components=1 \
    && pip3 install --no-cache-dir --no-build-isolation /tmp/r2pipe/python \
    && rm -rf /tmp/r2pipe /tmp/r2pipe.tar.gz

COPY pyproject.toml /opt/ipc/pyproject.toml
COPY backend /opt/ipc/backend
RUN pip3 install --no-cache-dir --no-build-isolation /opt/ipc

# Keep the frequently adjusted AI compatibility lock at the end so updating
# it does not invalidate the large CTF/JDK layers above.
COPY docker/member/ai-requirements.txt /opt/ipc-ai-requirements.txt
COPY scripts/ai_smoke.py /opt/ipc-ai-smoke.py
RUN pip3 install --no-cache-dir --ignore-installed -r /opt/ipc-ai-requirements.txt \
    && python3 /opt/ipc-ai-smoke.py

# Compatibility add-ons that must follow the app/ai installs: Keras needs
# sympy>=1.13.3 when using the CPU torch backend, and crypto scripts use gmpy2.
# The prebuilt ysoserial jar avoids requiring Maven/network at solve time.
ENV KERAS_BACKEND=torch
RUN set -eux; \
    pip3 install --no-cache-dir "sympy>=1.13.9" "gmpy2>=2.2"; \
    downloaded=0; \
    for base in "${GITHUB_RELEASE_PROXY}${GITHUB_MIRROR}" "${GITHUB_MIRROR}"; do \
      rm -f /opt/ipc-tools/tools/ysoserial-master/ysoserial.jar; \
      if curl -fL --retry 3 --retry-all-errors --retry-delay 3 \
        --speed-limit 1024 --speed-time 30 \
        -o /opt/ipc-tools/tools/ysoserial-master/ysoserial.jar \
        "${base}/frohoff/ysoserial/releases/download/v0.0.6/ysoserial-all.jar"; then \
        downloaded=1; break; \
      fi; \
    done; \
    test "${downloaded}" = 1; \
    test -s /opt/ipc-tools/tools/ysoserial-master/ysoserial.jar; \
    python3 -c "import keras, gmpy2; print(keras.__version__, gmpy2.version())"

# Kali's nmap binary carries cap_net_admin in its file capabilities. Docker's
# default bounding set does not include that capability, so exec() fails with
# EPERM and every nmap invocation (including a connect scan) dies before it
# starts. Keep only capabilities a default container already holds. This stays
# the last layer so a capability change never rebuilds the toolchain.
RUN setcap cap_net_raw,cap_net_bind_service+ep /usr/lib/nmap/nmap \
    && getcap /usr/lib/nmap/nmap

# Nuclei 3.x looks in $HOME/.local/nuclei-templates by default, while the image
# bakes the pinned tree at /root/nuclei-templates. Point the default at the
# baked tree so a member command does not need to remember -t (and does not
# fetch templates at solve time).
RUN printf '%s\n' '#!/bin/sh' \
      'exec /usr/bin/nuclei -t /root/nuclei-templates "$@"' \
      > /usr/local/bin/nuclei \
    && chmod 0755 /usr/local/bin/nuclei \
    && /usr/local/bin/nuclei -duc -tl 2>/dev/null | wc -l

WORKDIR /workspace

CMD ["sleep", "infinity"]
