#!/usr/bin/env python3
"""빌드 MCP 서버 - 빌드 실행 + 컴파일 에러 구조화.

핵심은 parse_errors 임. gcc/clang 의 원문 로그는 템플릿 인스턴스화 백트레이스 등으로
수천 줄이 나오는데, 이걸 그대로 모델에 넣으면 컨텍스트만 태우고 정확도는 떨어짐.
{file, line, severity, message} 로 정규화해서 넘기면 토큰도 줄고 판단도 정확해짐.

환경변수
  LLM_BUILD_DIR   빌드 디렉터리 (기본: cwd)
  LLM_BUILD_CMD   빌드 명령 (기본: "cmake --build . -j")
  LLM_BUILD_TIMEOUT  타임아웃 초 (기본 1800)
"""

import os
import re
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _mcplib import Server, run_cmd, clamp  # noqa: E402

BUILD_DIR = os.path.realpath(os.environ.get("LLM_BUILD_DIR", os.getcwd()))
# 컴파일러는 빌드 디렉터리 기준 상대경로(../src/x.c)를 뱉음. repo-mcp 의 read_file 이
# 저장소 상대경로를 받으므로, 두 서버가 같은 경로 표기를 쓰도록 여기서 정규화함.
REPO_ROOT = os.path.realpath(os.environ.get("LLM_REPO_ROOT", BUILD_DIR))
BUILD_CMD = os.environ.get("LLM_BUILD_CMD", "cmake --build . -j")
TIMEOUT = int(os.environ.get("LLM_BUILD_TIMEOUT", "1800"))

LAST = {"output": "", "rc": None}

# gcc/clang 공통: path:line:col: severity: message
DIAG = re.compile(
    r"^(?P<file>[^\s:][^:]*):(?P<line>\d+):(?:(?P<col>\d+):)?\s+"
    r"(?P<sev>error|warning|note|fatal error):\s+(?P<msg>.*)$")
# ld 에러: 파일:줄 형식이 아닌 경우가 많음
LD = re.compile(r"(undefined reference to|multiple definition of|cannot find -l|"
                r"undefined symbol:|duplicate symbol)")


srv = Server("build-mcp")


@srv.tool(
    "빌드를 실행함. 출력은 저장되며 parse_errors 로 구조화해 조회할 수 있음. "
    "출력이 길 수 있으니 결과 판단은 parse_errors 를 쓸 것.",
    {"type": "object",
     "properties": {
         "target": {"type": "string", "description": "빌드 타깃 (선택)"},
         "clean": {"type": "boolean", "description": "clean 후 빌드 (기본 false)"},
     }},
)
def build(target=None, clean=False):
    argv = shlex.split(BUILD_CMD)
    if clean:
        argv += ["--clean-first"] if "cmake" in BUILD_CMD else ["clean"]
    if target:
        if not re.fullmatch(r"[\w./+-]+", target):
            raise ValueError(f"허용되지 않는 타깃 이름: {target}")
        argv += (["--target", target] if "cmake" in BUILD_CMD else [target])

    rc, out = run_cmd(argv, timeout=TIMEOUT, cwd=BUILD_DIR)
    LAST["output"] = out
    LAST["rc"] = rc

    diags = _collect(out)
    errs = [d for d in diags if d["severity"] in ("error", "fatal error")]
    status = "성공" if rc == 0 else f"실패(rc={rc})"
    return (f"빌드 {status}. 에러 {len(errs)}건, 진단 총 {len(diags)}건.\n"
            f"상세는 parse_errors 호출. 마지막 40줄:\n"
            + "\n".join(out.splitlines()[-40:]))


def _norm(path):
    """진단 경로를 저장소 루트 기준 상대경로로 변환."""
    ab = path if os.path.isabs(path) else os.path.join(BUILD_DIR, path)
    ab = os.path.realpath(ab)
    if ab.startswith(REPO_ROOT + os.sep):
        return os.path.relpath(ab, REPO_ROOT)
    return path


def _collect(text):
    diags = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = DIAG.match(line.strip())
        if m:
            d = m.groupdict()
            # 진단 바로 다음 줄들에 있는 코드 인용/캐럿은 원인 파악에 유용하므로 한 줄만 붙임
            snippet = lines[i + 1].strip() if i + 1 < len(lines) else ""
            diags.append({
                "file": _norm(d["file"]),
                "line": int(d["line"]),
                "col": int(d["col"]) if d["col"] else None,
                "severity": d["sev"],
                "message": d["msg"],
                "snippet": snippet if not DIAG.match(snippet) else "",
            })
        elif LD.search(line):
            diags.append({"file": "<link>", "line": 0, "col": None,
                          "severity": "error", "message": line.strip(), "snippet": ""})
    return diags


@srv.tool(
    "마지막 빌드의 에러/경고를 구조화해 반환함. note 는 기본 제외되며 "
    "템플릿 인스턴스화 추적이 필요할 때만 include_notes=true 로 조회할 것.",
    {"type": "object",
     "properties": {
         "severity": {"type": "string",
                      "description": "'error'(기본) 또는 'all'"},
         "include_notes": {"type": "boolean", "description": "note 포함 (기본 false)"},
         "limit": {"type": "integer", "description": "최대 건수 (기본 40)"},
     }},
)
def parse_errors(severity="error", include_notes=False, limit=40):
    if LAST["rc"] is None:
        raise RuntimeError("빌드 기록이 없음. build 를 먼저 호출할 것")
    diags = _collect(LAST["output"])
    if severity == "error":
        diags = [d for d in diags if d["severity"] in ("error", "fatal error")]
    if not include_notes:
        diags = [d for d in diags if d["severity"] != "note"]

    if not diags:
        return f"진단 없음 (마지막 빌드 rc={LAST['rc']})"

    limit = max(1, min(int(limit), 200))
    out = [f"총 {len(diags)}건 중 {min(limit, len(diags))}건 표시 (rc={LAST['rc']})"]
    for d in diags[:limit]:
        loc = f"{d['file']}:{d['line']}" + (f":{d['col']}" if d["col"] else "")
        out.append(f"[{d['severity']}] {loc}\n  {d['message']}"
                   + (f"\n  > {d['snippet']}" if d["snippet"] else ""))
    return clamp("\n".join(out))


@srv.tool(
    "마지막 빌드의 원문 로그를 조회함. parse_errors 로 안 잡히는 툴체인/링커 문제 확인용.",
    {"type": "object",
     "properties": {"tail": {"type": "integer", "description": "마지막 N줄 (기본 200)"}}},
)
def raw_log(tail=200):
    if LAST["rc"] is None:
        raise RuntimeError("빌드 기록이 없음. build 를 먼저 호출할 것")
    lines = LAST["output"].splitlines()
    return clamp("\n".join(lines[-max(1, min(int(tail), 2000)):]))


@srv.tool(
    "빌드 설정(디렉터리, 명령, 타임아웃)을 조회함. 빌드가 엉뚱한 곳에서 도는지 확인용.",
    {"type": "object", "properties": {}},
)
def build_config():
    return (f"BUILD_DIR={BUILD_DIR}\nREPO_ROOT={REPO_ROOT}\n"
            f"BUILD_CMD={BUILD_CMD}\nTIMEOUT={TIMEOUT}s\n"
            f"마지막 빌드 rc={LAST['rc']}")


if __name__ == "__main__":
    srv.run()
