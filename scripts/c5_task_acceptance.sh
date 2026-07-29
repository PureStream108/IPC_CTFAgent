#!/usr/bin/env bash
set -euo pipefail

run() {
  local label="$1"
  shift
  printf '\n[C5] %s\n' "$label"
  "$@"
}

run "Java runtime" java -version
run "Java compiler" javac -version
run "Maven" mvn -version

ysoserial_dir=/workspace/tools/ysoserial-master
if [[ -d "$ysoserial_dir" ]]; then
  ysoserial_jar="$(find "$ysoserial_dir" -type f -name 'ysoserial-*-all.jar' -print -quit)"
  if [[ -z "$ysoserial_jar" ]]; then
    # Upstream still targets Java 6, which modern JDKs no longer accept. The
    # task image uses JDK 21, so build the unchanged sources as Java 8 bytecode.
    sed -i \
      -e 's|<source>1\.6</source>|<source>1.8</source>|' \
      -e 's|<target>1\.6</target>|<target>1.8</target>|' \
      "$ysoserial_dir/pom.xml"
    cat >/tmp/c5-maven-settings.xml <<'EOF'
<settings>
  <mirrors>
    <mirror>
      <id>aliyun-public</id>
      <mirrorOf>*</mirrorOf>
      <url>https://maven.aliyun.com/repository/public</url>
    </mirror>
  </mirrors>
</settings>
EOF
    run "Build ysoserial" mvn -q -s /tmp/c5-maven-settings.xml \
      -f "$ysoserial_dir/pom.xml" -DskipTests package
    ysoserial_jar="$(find "$ysoserial_dir" -type f -name 'ysoserial-*-all.jar' -print -quit)"
  fi
  test -n "$ysoserial_jar"
  printf '\n[C5] ysoserial\n'
  java -jar "$ysoserial_jar" 2>&1 | head -8 || true
fi

ghidra_help="$("${IPC_GHIDRA_HEADLESS:-/opt/ghidra/support/analyzeHeadless}" 2>&1 || true)"
grep -qi "usage" <<<"$ghidra_help"
run "PyGhidra import" python3 -c "import importlib.metadata, pyghidra; print(importlib.metadata.version('pyghidra'))"

cat >/tmp/c5_reverse.c <<'EOF'
#include <stdio.h>
__attribute__((noinline)) int secret_check(int value) {
    return value == 0x1337;
}
int main(int argc, char **argv) {
    int value = argc > 1 ? 0x1337 : 0;
    puts(secret_check(value) ? "accepted" : "rejected");
    return 0;
}
EOF
run "Compile reverse fixture" gcc -O0 -g -fno-pie -no-pie -o /tmp/c5_reverse /tmp/c5_reverse.c

run "Ghidra headless analysis" timeout 120 \
  "${IPC_GHIDRA_HEADLESS:-/opt/ghidra/support/analyzeHeadless}" \
  /tmp/c5-headless C5 -import /tmp/c5_reverse \
  -analysisTimeoutPerFile 60 -deleteProject

run "Reverse MCP decompile and r2 path" python3 - <<'PY'
import json
from backend.mcp.reverse_mcp import _decompile_sync, _r2_cmd_sync

result = _decompile_sync("/tmp/c5_reverse", "secret_check", 90)
assert result.get("available"), result
assert (result.get("pseudocode") or result.get("disassembly", "")).strip(), result
print(json.dumps(result, ensure_ascii=False)[:1200])

r2 = _r2_cmd_sync("/tmp/c5_reverse", "aaa; afl")
assert r2.get("available") and r2.get("output", "").strip(), r2
print(r2["output"])
PY

test -z "$(find /tmp -maxdepth 1 -type d -name 'ipc-ghidra-*' -print -quit)"
run "cado-nfs" bash -lc 'cado-nfs --help >/dev/null 2>&1 || cado-nfs -h >/dev/null'
run "radare2" r2 -v
run "radare2 analysis" r2 -q -c "aaa; afl; q" /tmp/c5_reverse
run "nmap" nmap --version
run "sqlmap" sqlmap --version
run "SageMath" sage --version
run "CTF Python imports" python3 -c "import angr, pwn, volatility3; print('angr/pwn/volatility3 ready')"
run "ripgrep" rg --version
run "Playwright Chromium" python3 - <<'PY'
from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    print(browser.version)
    browser.close()
PY

printf '\n[C5] all task-image checks passed\n'
