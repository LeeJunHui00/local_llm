#!/usr/bin/env python3
"""gdb MCP 서버 - 코어덤프/바이너리 정적 분석 전용 (읽기 전용).

의도적으로 제외한 기능: run/continue/attach/set var 등 실행 제어.
에이전트가 실수로 프로세스를 붙잡거나 메모리를 변경하는 사고를 막기 위함임.
분석에 필요한 건 backtrace / 지역변수 / 표현식 평가 / 디스어셈블이 대부분임.

환경변수
  LLM_GDB            gdb 실행 파일 경로 (기본 "gdb")
  LLM_GDB_TIMEOUT    호출당 타임아웃 초 (기본 60)
  LLM_GDB_SYSROOT    크로스 디버깅 시 sysroot
  LLM_GDB_SOLIB_PATH 공유 라이브러리 검색 경로
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _mcplib import Server, run_cmd, clamp  # noqa: E402

GDB = os.environ.get("LLM_GDB", "gdb")
TIMEOUT = int(os.environ.get("LLM_GDB_TIMEOUT", "60"))

STATE = {"binary": None, "core": None}

# gdb 명령 문자열로 셸을 여는 경로를 차단함.
# -ex 는 argv 로 전달되므로 셸 인젝션은 없지만, gdb 자체가 shell/pipe/file 등
# 외부 실행 명령을 갖고 있어 명령 이름 수준에서 막아야 함.
_BANNED = ("shell", "!", "pipe", "python", "python-interactive", "pi", "eval",
           "compile", "run", "start", "attach", "kill", "set var", "call ",
           "define", "source", "dump", "append", "generate-core-file")


def _guard(cmd):
    if "\n" in cmd or "\r" in cmd:
        raise ValueError("개행 문자는 허용되지 않음 (명령 인젝션 방지)")
    low = cmd.strip().lower()
    for b in _BANNED:
        if low == b.strip() or low.startswith(b):
            raise ValueError(f"허용되지 않는 gdb 명령: {cmd!r}")
    return cmd


def _gdb(commands, need_core=False):
    if not STATE["binary"]:
        raise RuntimeError("먼저 load_core 또는 load_binary 를 호출해야 함")
    if need_core and not STATE["core"]:
        raise RuntimeError("이 도구는 코어덤프가 필요함. load_core 를 호출할 것")

    argv = [GDB, "-batch", "-nx", "-q"]
    if os.environ.get("LLM_GDB_SYSROOT"):
        argv += ["-ex", f"set sysroot {os.environ['LLM_GDB_SYSROOT']}"]
    if os.environ.get("LLM_GDB_SOLIB_PATH"):
        argv += ["-ex", f"set solib-search-path {os.environ['LLM_GDB_SOLIB_PATH']}"]
    # 페이징/확인 프롬프트가 뜨면 batch 모드에서도 출력이 뭉개짐
    argv += ["-ex", "set pagination off", "-ex", "set confirm off"]
    for c in commands:
        argv += ["-ex", _guard(c)]
    argv.append(STATE["binary"])
    if STATE["core"]:
        argv.append(STATE["core"])

    _, out = run_cmd(argv, timeout=TIMEOUT)
    return clamp(out)


def _resolve(path, what):
    p = os.path.realpath(os.path.expanduser(path))
    if not os.path.isfile(p):
        raise FileNotFoundError(f"{what} 파일이 없음: {path}")
    return p


srv = Server("gdb-mcp")


@srv.tool(
    "분석할 바이너리와 코어덤프를 로드함. 크래시 분석은 항상 이 호출로 시작할 것. "
    "디버그 심볼이 없는 바이너리면 백트레이스가 주소만 나오므로 심볼 포함 빌드를 지정해야 함.",
    {"type": "object",
     "properties": {
         "binary": {"type": "string", "description": "실행 파일 경로 (심볼 포함본 권장)"},
         "core": {"type": "string", "description": "코어덤프 파일 경로"},
     },
     "required": ["binary", "core"]},
)
def load_core(binary, core):
    STATE["binary"] = _resolve(binary, "바이너리")
    STATE["core"] = _resolve(core, "코어덤프")
    return _gdb(["info program", "bt 1"])


@srv.tool(
    "코어덤프 없이 바이너리만 로드함. 심볼/디스어셈블 조회용.",
    {"type": "object",
     "properties": {"binary": {"type": "string"}},
     "required": ["binary"]},
)
def load_binary(binary):
    STATE["binary"] = _resolve(binary, "바이너리")
    STATE["core"] = None
    return _gdb(["info file"])


@srv.tool(
    "백트레이스를 조회함. all_threads=true 면 전체 스레드(교착/경합 분석용). "
    "프레임이 많으면 limit 로 줄일 것.",
    {"type": "object",
     "properties": {
         "all_threads": {"type": "boolean", "description": "전체 스레드 백트레이스 (기본 false)"},
         "limit": {"type": "integer", "description": "출력할 프레임 수 (기본 40)"},
     }},
)
def backtrace(all_threads=False, limit=40):
    limit = max(1, min(int(limit), 200))
    cmd = f"thread apply all bt {limit}" if all_threads else f"bt {limit}"
    return _gdb([cmd], need_core=True)


@srv.tool(
    "특정 프레임의 상세 정보(인자, 지역변수, 소스 위치)를 조회함. "
    "backtrace 로 의심 프레임을 고른 뒤 호출할 것.",
    {"type": "object",
     "properties": {"n": {"type": "integer", "description": "프레임 번호"}},
     "required": ["n"]},
)
def frame(n):
    n = int(n)
    return _gdb([f"frame {n}", "info frame", "info args", "info locals", "list"],
                need_core=True)


@srv.tool(
    "지정 프레임에서 표현식을 평가함. 구조체 포인터 역참조 등에 사용. "
    "부작용 있는 표현식(함수 호출, 대입)은 차단됨.",
    {"type": "object",
     "properties": {
         "expr": {"type": "string", "description": "예: *pNode, hdr->len, argv[1]"},
         "n": {"type": "integer", "description": "평가할 프레임 번호 (기본 0)"},
     },
     "required": ["expr"]},
)
def print_expr(expr, n=0):
    if "=" in expr.replace("==", "").replace("!=", "").replace(">=", "").replace("<=", ""):
        raise ValueError("대입 연산은 허용되지 않음 (읽기 전용)")
    return _gdb([f"frame {int(n)}", f"print {expr}", f"ptype {expr}"], need_core=True)


@srv.tool(
    "함수를 디스어셈블함. 심볼이 깨졌거나 최적화로 소스 대조가 안 될 때 사용.",
    {"type": "object",
     "properties": {
         "func": {"type": "string", "description": "함수명 또는 주소"},
         "source": {"type": "boolean", "description": "소스 라인 섞어서 출력 (기본 true)"},
     },
     "required": ["func"]},
)
def disas(func, source=True):
    return _gdb([f"disassemble /{'s' if source else 'r'} {func}"])


@srv.tool(
    "소스 코드를 조회함. 위치는 'file.c:123' 또는 '함수명' 형식.",
    {"type": "object",
     "properties": {
         "location": {"type": "string"},
         "lines": {"type": "integer", "description": "출력 줄 수 (기본 20)"},
     },
     "required": ["location"]},
)
def list_source(location, lines=20):
    return _gdb([f"set listsize {max(5, min(int(lines), 200))}", f"list {location}"])


@srv.tool(
    "로드된 공유 라이브러리와 심볼 적재 상태를 조회함. "
    "백트레이스에 ?? 가 나올 때 심볼/라이브러리 경로 문제인지 확인용.",
    {"type": "object", "properties": {}},
)
def info_libs():
    return _gdb(["info sharedlibrary", "info threads"])


@srv.tool(
    "레지스터와 크래시 지점 주변 메모리를 조회함. 스택 손상/널 역참조 구분용.",
    {"type": "object",
     "properties": {"n": {"type": "integer", "description": "프레임 번호 (기본 0)"}}},
)
def registers(n=0):
    return _gdb([f"frame {int(n)}", "info registers", "x/8i $pc", "x/16gx $sp"],
                need_core=True)


if __name__ == "__main__":
    srv.run()
