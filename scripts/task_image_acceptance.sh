#!/usr/bin/env bash
set -euo pipefail

run() {
  local label="$1"
  shift
  printf '\n[acceptance] %s\n' "$label"
  "$@"
}

# Some of these CLIs print usage and exit non-zero for -h/--help, so their
# version probe is not a stable success signal. For those, assert that the
# executable exists and that it starts, and say so here rather than pretending
# a --version flag exists.
check_cli() {
  local name="$1"
  shift
  printf '\n[acceptance] %s (existence + start)\n' "$name"
  local bin
  bin="$(command -v "$name")"
  test -x "$bin"
  "$@" >/dev/null 2>&1 || true
}

run "gcc" gcc --version
run "g++" g++ --version
run "make" make --version
run "gdb" gdb --version
run "gdbserver" gdbserver --version
run "radare2" r2 -v
run "binwalk" binwalk --help
run "foremost" foremost -V
run "steghide" steghide --version
run "checksec" checksec --file=/bin/true
run "objdump" objdump --version
run "readelf" readelf --version
run "nm" nm --version
run "elfutils" eu-readelf --version
run "strace" strace -V
run "file" file --version
run "xxd" xxd -v
run "exiftool" exiftool -ver
run "tshark" tshark -D
run "tcpdump" tcpdump --version
run "socat" socat -V
run "musl-gcc" musl-gcc --version
run "upx version" bash -lc 'upx --version 2>&1 | grep -F 4.2.4'
run "Python CTF imports" python3 -c "import jwt, jwcrypto, scapy, dpkt; print('jwt/scapy/dpkt ready')"
run "ysoserial jar" test -s /workspace/tools/ysoserial-master/ysoserial.jar
run "Keras" bash -lc 'KERAS_BACKEND=torch python3 -c "import keras; print(keras.__version__)"'
run "playwright-cli" playwright-cli --help

printf '\n[acceptance] scanning and web enumeration\n'
run "nmap" nmap --version
run "ncat" ncat --version
check_cli masscan masscan --version  # prints the version but exits 1
run "whatweb" whatweb --version
check_cli wafw00f wafw00f --help
check_cli nikto nikto -Version
run "nuclei" nuclei -version
run "nuclei templates" bash -lc 'nuclei -duc -tl > /tmp/ipc-nuclei-tl.txt && test -s /tmp/ipc-nuclei-tl.txt && wc -l /tmp/ipc-nuclei-tl.txt'
run "ffuf" ffuf -V
run "feroxbuster" feroxbuster --version
run "gobuster" gobuster --version
check_cli dirb dirb
check_cli wfuzz wfuzz --help
run "sqlmap" sqlmap --version
run "sslscan" sslscan --version

printf '\n[acceptance] service enumeration\n'
check_cli enum4linux enum4linux -h
check_cli smbmap smbmap -h
check_cli onesixtyone onesixtyone -h
check_cli dnsmap dnsmap
run "dig" dig -v
run "ldapsearch" ldapsearch -VV
run "snmpwalk" snmpwalk -V
check_cli showmount showmount --help

printf '\n[acceptance] credential auditing\n'
check_cli hydra hydra -h
check_cli medusa medusa -h
check_cli ncrack ncrack -h
check_cli john bash -lc 'john < /dev/null'  # Ubuntu ships john 1.8, which has no --list
run "hashcat" hashcat --version
run "sshpass" sshpass -V

printf '\n[acceptance] wordlists\n'
test -s /workspace/wordlists/rockyou.txt
test -s /workspace/wordlists/SecLists/Discovery/Web-Content/common.txt
test -s /workspace/wordlists/SecLists/Passwords/Common-Credentials/xato-net-10-million-passwords-100000.txt

printf '\n[acceptance] removed tools\n'
for command in rustc cargo ghidra fenjing; do
  if command -v "$command" >/dev/null 2>&1; then
    echo "unexpected command present: $command" >&2
    exit 1
  fi
done
# UPX is now the Kali distro build (the Ubuntu 3.96 package was why it used
# to be built from an upstream release). The package must exist and provide
# the 4.x CLI.
dpkg -s upx-ucl >/dev/null 2>&1
upx --version >/dev/null
for command in RsaCtfTool flask-unsign; do
  if command -v "$command" >/dev/null 2>&1; then
    echo "unexpected command present: $command" >&2
    exit 1
  fi
done
if python3 -c "import pyghidra" >/dev/null 2>&1; then
  echo "pyghidra must not be importable" >&2
  exit 1
fi
test ! -e /opt/ghidra
test ! -e /workspace/tools/Fenjing
test ! -e /workspace/tools/JNDIMap
test ! -e /workspace/tools/shiro550

printf '\n[acceptance] retained tools\n'
test -d /workspace/tools/ysoserial-master
command -v sqlmap >/dev/null || test -f /workspace/tools/sqlmap/sqlmapproject-sqlmap-03be590/sqlmap.py
java -version
run "ysoserial source" test -d /workspace/tools/ysoserial-master/src

printf '\n[acceptance] shared CLI pool\n'
test -x /workspace/tools/playwright-cli/playwright-cli
/workspace/tools/playwright-cli/playwright-cli --help >/dev/null

printf '\n[acceptance] all task-image checks passed\n'