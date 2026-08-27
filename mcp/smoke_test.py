#!/usr/bin/env python3
"""MCP 서버 단독 검증 - 에이전트 없이 stdio JSON-RPC 왕복을 확인함.

사내 LLM 연결 전에 서버 자체가 정상인지 먼저 갈라내기 위한 도구임.
서버가 죽거나 도구가 실패하면 에이전트 쪽에서는 원인이 안 보임.

사용법
  python3 mcp/smoke_test.py repo_mcp.py
  python3 mcp/smoke_test.py repo_mcp.py --call find_symbol '{"name":"main"}'
  python3 mcp/smoke_test.py gdb_mcp.py --call load_core '{"binary":"./a.out","core":"./core"}'
"""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    server = os.path.join(HERE, sys.argv[1])
    if not os.path.isfile(server):
        sys.exit(f"서버 파일 없음: {server}")

    calls = []
    if "--call" in sys.argv:
        i = sys.argv.index("--call")
        name = sys.argv[i + 1]
        args = json.loads(sys.argv[i + 2]) if len(sys.argv) > i + 2 else {}
        calls.append((name, args))

    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "smoke-test", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    for n, (name, args) in enumerate(calls, start=3):
        reqs.append({"jsonrpc": "2.0", "id": n, "method": "tools/call",
                     "params": {"name": name, "arguments": args}})

    stdin = "\n".join(json.dumps(r) for r in reqs) + "\n"
    p = subprocess.run([sys.executable, server], input=stdin,
                       capture_output=True, text=True, timeout=900)
    if p.stderr.strip():
        print(f"[stderr]\n{p.stderr}", file=sys.stderr)

    lines = [l for l in p.stdout.splitlines() if l.strip()]
    if not lines:
        sys.exit(f"응답 없음 (rc={p.returncode}). 서버가 즉시 종료됨")

    ok = True
    for line in lines:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            print(f"[X] JSON 아님: {line[:200]}")
            ok = False
            continue

        rid = msg.get("id")
        if "error" in msg:
            print(f"[X] id={rid} 프로토콜 에러: {msg['error']}")
            ok = False
        elif rid == 1:
            info = msg["result"]["serverInfo"]
            print(f"[O] initialize: {info['name']} v{info['version']} "
                  f"(protocol {msg['result']['protocolVersion']})")
        elif rid == 2:
            tools = msg["result"]["tools"]
            print(f"[O] tools/list: {len(tools)}개")
            for t in tools:
                req = t["inputSchema"].get("required", [])
                print(f"      - {t['name']}({', '.join(req)})")
        else:
            r = msg["result"]
            text = r["content"][0]["text"]
            tag = "[X] 도구 실패" if r.get("isError") else "[O] tools/call"
            if r.get("isError"):
                ok = False
            print(f"{tag} id={rid}:\n{text[:1500]}")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
