#!/usr/bin/env bash
set -euo pipefail

work=/tmp/ipc-e2e
rm -rf "$work"
mkdir -p "$work"
cd "$work"
printf '<html><body><h1>ipc-e2e</h1><a href="/next">next</a></body></html>\n' > index.html

python3 -m http.server 8765 --bind 127.0.0.1 >"$work/http.log" 2>&1 &
http_pid=$!
cleanup() {
  kill "$http_pid" 2>/dev/null || true
  playwright-cli -s=ipc-e2e close >/dev/null 2>&1 || true
  if [[ -n "${tcpdump_pid:-}" ]]; then kill "$tcpdump_pid" 2>/dev/null || true; fi
}
trap cleanup EXIT
sleep 1

printf '\n[e2e] playwright-cli open/snapshot/screenshot\n'
playwright-cli -s=ipc-e2e open http://127.0.0.1:8765
playwright-cli -s=ipc-e2e snapshot --filename="$work/snapshot.yml"
playwright-cli -s=ipc-e2e screenshot --filename="$work/shot.png"
test -s "$work/snapshot.yml"
test -s "$work/shot.png"
playwright-cli -s=ipc-e2e close

printf '\n[e2e] content discovery against the local server\n'
printf '{"ok":true}\n' > "$work/next"
printf 'next\nnot-a-real-path\n' > "$work/words.txt"
ffuf -s -t 10 -rate 50 -w "$work/words.txt" -u http://127.0.0.1:8765/FUZZ -mc 200 | grep -F 'next'
gobuster dir -q -t 10 --timeout 5s -w "$work/words.txt" -u http://127.0.0.1:8765/ | grep -F 'next'
feroxbuster -q -t 5 --depth 1 --status-codes 200 -w "$work/words.txt" -u http://127.0.0.1:8765/ | grep -F 'next'
nmap -sT -Pn -p 8765 127.0.0.1 | grep -F '8765/tcp open'

printf '\n[e2e] compile/gdb/r2/upx\n'
cat >"$work/sample.c" <<'EOF'
#include <stdio.h>
int main(void) { puts("ipc-e2e"); return 0; }
EOF
gcc -O0 -g -fno-pie -no-pie -o "$work/sample" "$work/sample.c"
"$work/sample" | grep -F ipc-e2e
gdb -q --batch -ex "file $work/sample" -ex "info functions" | head -40
gdbserver --version | head -1
r2 -q -c 'aaa; afl' "$work/sample" >/dev/null
cp "$work/sample" "$work/sample.upx"
upx -q "$work/sample.upx"
upx -q -d "$work/sample.upx" -o "$work/sample.unpacked"
"$work/sample.unpacked" | grep -F ipc-e2e

printf '\n[e2e] tcpdump/tshark\n'
timeout 10 tcpdump -i lo -U -w "$work/lo.pcap" 'tcp port 8765' >"$work/tcpdump.log" 2>&1 &
tcpdump_pid=$!
sleep 1
curl -fsS http://127.0.0.1:8765/ >/dev/null
curl -fsS http://127.0.0.1:8765/ >/dev/null
curl -fsS http://127.0.0.1:8765/ >/dev/null
wait "$tcpdump_pid" || true
unset tcpdump_pid
test -s "$work/lo.pcap"
tshark -r "$work/lo.pcap" -Y http.request -T fields -e http.host | grep -F 127.0.0.1

printf '\n[e2e] strace/foremost/steghide/binwalk/checksec/exiftool/socat/musl\n'
strace -f -e trace=execve true >"$work/strace.log" 2>&1
grep -F execve "$work/strace.log" >/dev/null
foremost -V >/dev/null
steghide --version >/dev/null
binwalk --help >/dev/null
checksec --file=/bin/true >/dev/null
exiftool -ver >/dev/null
socat -V >/dev/null
musl-gcc -static -O2 -o "$work/sample-static" "$work/sample.c"
file "$work/sample-static" | grep -F statically

printf '\n[e2e] shared playwright CLI path\n'
test -x /workspace/tools/playwright-cli/playwright-cli
/workspace/tools/playwright-cli/playwright-cli --help >/dev/null

printf '\n[e2e] all task-image CLI E2E checks passed\n'
