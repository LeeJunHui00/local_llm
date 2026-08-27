"""의존성 없는 최소 MCP stdio 서버.

폐쇄망에서 `mcp` SDK wheel 반입이 어려운 경우를 대비해 표준 라이브러리만으로
구현함. 지원 범위는 initialize / tools/list / tools/call / ping 이며,
CLI 코딩 에이전트가 MCP 서버를 붙일 때 실제로 쓰는 부분은 이게 전부임.

SDK 를 반입할 수 있다면 그쪽으로 갈아타도 서버 코드(도구 함수)는 그대로 재사용 가능함.
"""

import json
import os
import shlex
import subprocess
import sys

PROTOCOL_VERSION = "2025-06-18"

# 도구 응답 최대 길이. 사내 모델 컨텍스트가 좁을 가능성이 높아 기본값을 보수적으로 둠.
MAX_OUTPUT = int(os.environ.get("LLM_MCP_MAX_OUTPUT", "20000"))


def clamp(text, limit=None):
    """긴 출력을 앞뒤만 남기고 잘라냄. 중간을 버리는 이유는 gdb/빌드 출력이
    보통 앞(요약)과 뒤(결론)에 정보가 몰려 있기 때문임."""
    limit = limit or MAX_OUTPUT
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2:]
    dropped = len(text) - limit
    return f"{head}\n\n... [{dropped}자 생략] ...\n\n{tail}"


def run_cmd(argv, timeout=60, cwd=None, env=None):
    """서브프로세스 실행. 셸을 거치지 않으므로 인자 인젝션이 발생하지 않음."""
    try:
        p = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            cwd=cwd,
            env=env,
        )
    except FileNotFoundError:
        raise RuntimeError(f"실행 파일을 찾을 수 없음: {argv[0]}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"타임아웃 {timeout}s 초과: {shlex.join(argv)}")
    out = p.stdout or ""
    err = p.stderr or ""
    if err.strip():
        out = f"{out}\n[stderr]\n{err}"
    return p.returncode, out


class Server:
    def __init__(self, name, version="0.1.0"):
        self.name = name
        self.version = version
        self._tools = {}

    def tool(self, description, schema):
        """도구 등록 데코레이터. 함수명이 곧 도구 이름이 됨."""
        def deco(fn):
            self._tools[fn.__name__] = {
                "name": fn.__name__,
                "description": description,
                "inputSchema": schema,
                "_fn": fn,
            }
            return fn
        return deco

    # --- JSON-RPC ---

    def _write(self, obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def _ok(self, rid, result):
        self._write({"jsonrpc": "2.0", "id": rid, "result": result})

    def _err(self, rid, code, message):
        self._write({"jsonrpc": "2.0", "id": rid,
                     "error": {"code": code, "message": message}})

    def run(self):
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                self._dispatch(msg)
            except Exception as e:  # 서버가 죽으면 에이전트 세션 전체가 끊기므로 방어
                rid = msg.get("id")
                if rid is not None:
                    self._err(rid, -32603, f"{type(e).__name__}: {e}")

    def _dispatch(self, msg):
        rid = msg.get("id")
        if rid is None:  # notification (notifications/initialized 등) 은 응답 없음
            return
        method = msg.get("method")
        params = msg.get("params") or {}

        if method == "initialize":
            self._ok(rid, {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.name, "version": self.version},
            })
        elif method == "ping":
            self._ok(rid, {})
        elif method == "tools/list":
            self._ok(rid, {"tools": [
                {k: v for k, v in t.items() if k != "_fn"}
                for t in self._tools.values()
            ]})
        elif method == "tools/call":
            self._call(rid, params)
        else:
            self._err(rid, -32601, f"method not found: {method}")

    def _call(self, rid, params):
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = self._tools.get(name)
        if tool is None:
            self._err(rid, -32602, f"unknown tool: {name}")
            return
        try:
            text = tool["_fn"](**args)
        except TypeError as e:
            self._ok(rid, {"content": [{"type": "text", "text": f"인자 오류: {e}"}],
                           "isError": True})
            return
        except Exception as e:
            # 도구 실패는 프로토콜 에러가 아니라 isError 결과로 돌려줘야
            # 모델이 스스로 다음 수를 판단할 수 있음.
            self._ok(rid, {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
                           "isError": True})
            return
        self._ok(rid, {"content": [{"type": "text", "text": clamp(str(text))}]})
