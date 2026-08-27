#!/usr/bin/env bash
# gdb MCP 실동작 검증 - 크래시 바이너리와 코어덤프를 만들어 backtrace 를 확인함.
#
#   sudo apt install gdb      # 선행
#   bash mcp/verify_gdb.sh
#
# 사내 리눅스에서는 코어덤프 경로가 apport/systemd-coredump 로 가로채질 수 있음.
# 그 경우 /proc/sys/kernel/core_pattern 을 확인하고 coredumpctl 로 꺼내야 함.

set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

command -v gdb >/dev/null || { echo "[X] gdb 미설치. sudo apt install gdb"; exit 1; }

cat > "$WORK/crash.c" <<'EOF'
#include <stdlib.h>
#include <string.h>

typedef struct { char name[16]; int refcount; } Session;

static int sessionLength(Session *s) { return (int)strlen(s->name); }

static void freeSession(Session *s) { free(s); }

int main(void) {
    Session *s = malloc(sizeof(Session));
    strcpy(s->name, "worker");
    s->refcount = 1;
    freeSession(s);
    s = NULL;
    return sessionLength(s);   /* 널 역참조로 SIGSEGV */
}
EOF

gcc -g -O0 -o "$WORK/crash" "$WORK/crash.c" || exit 1

cd "$WORK"
ulimit -c unlimited
PATTERN="$(cat /proc/sys/kernel/core_pattern 2>/dev/null || echo '?')"
./crash 2>/dev/null
CORE="$(ls -1 core core.* 2>/dev/null | head -1)"

if [ -z "$CORE" ]; then
    echo "[!] 코어덤프가 생성되지 않음. core_pattern='$PATTERN'"
    echo "    systemd-coredump 를 쓰는 시스템이면:  coredumpctl dump crash > core"
    echo "    직접 파일로 받으려면(루트 권한):  sysctl -w kernel.core_pattern=core"
    echo "[i] 코어 없이 load_binary + disas 만 검증함"
    python3 "$HERE/smoke_test.py" gdb_mcp.py --call load_binary "{\"binary\":\"$WORK/crash\"}" | tail -5
    python3 "$HERE/smoke_test.py" gdb_mcp.py --call disas '{"func":"main"}' 2>/dev/null | tail -5
    exit 0
fi

echo "[i] 코어덤프: $WORK/$CORE"
python3 - "$HERE" "$WORK/crash" "$WORK/$CORE" <<'PYEOF'
import json, os, subprocess, sys
here, binary, core = sys.argv[1], sys.argv[2], sys.argv[3]
reqs = [
 {"jsonrpc":"2.0","id":1,"method":"initialize",
  "params":{"protocolVersion":"2025-06-18","capabilities":{},
            "clientInfo":{"name":"verify","version":"0"}}},
 {"jsonrpc":"2.0","id":2,"method":"tools/call",
  "params":{"name":"load_core","arguments":{"binary":binary,"core":core}}},
 {"jsonrpc":"2.0","id":3,"method":"tools/call",
  "params":{"name":"backtrace","arguments":{"limit":10}}},
 {"jsonrpc":"2.0","id":4,"method":"tools/call",
  "params":{"name":"frame","arguments":{"n":0}}},
 {"jsonrpc":"2.0","id":5,"method":"tools/call",
  "params":{"name":"registers","arguments":{}}},
]
p = subprocess.run([sys.executable, os.path.join(here,"gdb_mcp.py")],
                   input="\n".join(json.dumps(r) for r in reqs)+"\n",
                   capture_output=True, text=True, timeout=300)
names = {2:"load_core",3:"backtrace",4:"frame(0)",5:"registers"}
bt = ""
for line in p.stdout.splitlines():
    m = json.loads(line)
    rid = m.get("id")
    if rid in names:
        r = m["result"]
        txt = r["content"][0]["text"]
        tag = "[X]" if r.get("isError") else "[O]"
        print(f"{tag} {names[rid]}:")
        print("    " + txt[:600].replace("\n","\n    "))
        if rid == 3: bt = txt
print()
if "sessionLength" in bt:
    print("[O] 백트레이스에서 크래시 함수(sessionLength) 확인됨 - gdb MCP 정상")
else:
    print("[!] 크래시 함수가 안 보임. 디버그 심볼(-g) 포함 여부를 확인할 것")
PYEOF
