#!/usr/bin/env python3
"""사내 소스코드/Git MCP 서버 (읽기 전용).

에이전트가 저장소를 스스로 탐색하게 만드는 게 목적임. ripgrep 으로 내용 검색,
ctags 로 심볼 정의 점프, git 으로 이력 조회까지 한 서버에서 처리함.

환경변수
  LLM_REPO_ROOT    저장소 루트 (기본: 프로세스 cwd)
  LLM_REPO_DENY    콜론 구분 차단 경로 패턴 (예: "secret:vendor/priv")
  LLM_TAGS_FILE    ctags 인덱스 경로 (기본: <root>/tags)
"""

import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _mcplib import Server, run_cmd, clamp  # noqa: E402

ROOT = os.path.realpath(os.environ.get("LLM_REPO_ROOT", os.getcwd()))
DENY = [p for p in os.environ.get("LLM_REPO_DENY", "").split(":") if p]
TAGS = os.environ.get("LLM_TAGS_FILE", os.path.join(ROOT, "tags"))

# ripgrep 이 있으면 쓰고, 없으면 grep 으로 떨어짐.
# 셸 함수/별칭으로만 존재하는 rg 는 서브프로세스에서 안 잡히므로 실제 실행 파일만 인정함.
_RG = shutil.which(os.environ.get("LLM_RG", "rg"))


def safe_path(rel, must_exist=True):
    """저장소 루트 밖으로 나가는 경로와 차단 목록을 거름.

    심볼릭 링크로 루트를 탈출하는 경우까지 막기 위해 realpath 로 정규화한 뒤
    비교함."""
    p = os.path.realpath(os.path.join(ROOT, rel))
    if p != ROOT and not p.startswith(ROOT + os.sep):
        raise PermissionError(f"저장소 루트 밖 경로는 접근 불가: {rel}")
    rest = os.path.relpath(p, ROOT)
    for d in DENY:
        if d in rest:
            raise PermissionError(f"차단된 경로: {rel}")
    if must_exist and not os.path.exists(p):
        raise FileNotFoundError(f"경로 없음: {rel}")
    return p


def _git(args, timeout=60):
    if not os.path.isdir(os.path.join(ROOT, ".git")):
        raise RuntimeError(f"git 저장소가 아님: {ROOT}")
    _, out = run_cmd(["git", "-C", ROOT] + args, timeout=timeout)
    return out


srv = Server("repo-mcp")


@srv.tool(
    "저장소 전체에서 정규식으로 코드를 검색함(ripgrep). 심볼 '사용처'를 찾을 때 사용. "
    "정의 위치만 필요하면 find_symbol 이 훨씬 빠름.",
    {"type": "object",
     "properties": {
         "pattern": {"type": "string", "description": "정규식 패턴"},
         "glob": {"type": "string", "description": "파일 필터. 예: '*.c', '*.{h,hpp}'"},
         "context": {"type": "integer", "description": "전후 컨텍스트 줄 수 (기본 2)"},
         "max_results": {"type": "integer", "description": "최대 매치 수 (기본 60)"},
     },
     "required": ["pattern"]},
)
def search_code(pattern, glob=None, context=2, max_results=60):
    ctx = max(0, min(int(context), 10))
    n = max(1, min(int(max_results), 300))

    if _RG:
        argv = [_RG, "--line-number", "--no-heading", "--color", "never",
                "--max-count", "20", "-C", str(ctx), "-m", str(n)]
        if glob:
            argv += ["--glob", glob]
        argv += ["-e", pattern, ROOT]
    else:
        # ripgrep 이 없는 폐쇄망 서버를 대비한 폴백. .git/build 는 수동 제외해야 함.
        argv = ["grep", "-rnE", "-C", str(ctx),
                "--exclude-dir=.git", "--exclude-dir=build",
                "--exclude-dir=node_modules"]
        if glob:
            argv += [f"--include={glob}"]
        argv += ["-e", pattern, ROOT]

    rc, out = run_cmd(argv, timeout=60)
    if rc == 1 and not out.strip():
        return f"매치 없음: {pattern}"
    if not _RG and out.count("\n") > n:
        out = "\n".join(out.splitlines()[:n]) + f"\n... [{n}건에서 잘림]"
    # 절대경로는 토큰만 잡아먹으므로 저장소 상대경로로 축약
    return clamp(out.replace(ROOT + os.sep, ""))


@srv.tool(
    "ctags 인덱스에서 심볼 정의 위치를 조회함. 함수/구조체/매크로 정의를 찾을 때 "
    "가장 먼저 쓸 것. 인덱스가 없으면 build_tags 를 먼저 호출.",
    {"type": "object",
     "properties": {
         "name": {"type": "string", "description": "심볼 이름 (정확히 일치)"},
         "prefix": {"type": "boolean", "description": "접두사 검색 (기본 false)"},
     },
     "required": ["name"]},
)
def find_symbol(name, prefix=False):
    if not os.path.isfile(TAGS):
        raise FileNotFoundError(f"ctags 인덱스가 없음: {TAGS}. build_tags 를 먼저 호출할 것")
    hits = []
    with open(TAGS, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("!_TAG_"):
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            sym = parts[0]
            if (sym.startswith(name) if prefix else sym == name):
                kind = parts[3].strip() if len(parts) > 3 else "?"
                loc = parts[1]
                if os.path.isabs(loc):
                    loc = os.path.relpath(loc, ROOT)
                hits.append(f"{sym}\t{loc}\t{kind}\t{parts[2].strip()}")
            if len(hits) >= 100:
                break
    if not hits:
        return f"심볼 없음: {name} (사용처 검색은 search_code 사용)"
    return clamp("\n".join(hits))


@srv.tool(
    "ctags 인덱스를 재생성함. 저장소를 처음 붙이거나 코드가 크게 바뀐 뒤 한 번 호출.",
    {"type": "object", "properties": {}},
)
def build_tags():
    rc, out = run_cmd(
        ["ctags", "-R", "--fields=+n", "--c-kinds=+p", "--c++-kinds=+p",
         "-f", TAGS, ROOT],
        timeout=600)
    if rc != 0:
        return f"ctags 실패(rc={rc}):\n{out}"
    n = sum(1 for _ in open(TAGS, encoding="utf-8", errors="replace"))
    return f"인덱스 생성 완료: {TAGS} ({n}개 항목)"


@srv.tool(
    "파일 내용을 줄 범위로 읽음. 대용량 소스를 통째로 넣지 말고 필요한 구간만 읽을 것.",
    {"type": "object",
     "properties": {
         "path": {"type": "string", "description": "저장소 상대 경로"},
         "start": {"type": "integer", "description": "시작 줄 (1부터, 기본 1)"},
         "end": {"type": "integer", "description": "끝 줄 (기본 start+200)"},
     },
     "required": ["path"]},
)
def read_file(path, start=1, end=None):
    p = safe_path(path)
    start = max(1, int(start))
    end = int(end) if end else start + 200
    lines = open(p, encoding="utf-8", errors="replace").read().splitlines()
    sel = lines[start - 1:end]
    body = "\n".join(f"{start + i:6d}| {l}" for i, l in enumerate(sel))
    return clamp(f"{path} ({start}-{min(end, len(lines))} / 총 {len(lines)}줄)\n{body}")


@srv.tool(
    "디렉터리 구조를 조회함. 저장소를 처음 파악할 때 사용.",
    {"type": "object",
     "properties": {
         "path": {"type": "string", "description": "저장소 상대 경로 (기본 루트)"},
         "depth": {"type": "integer", "description": "탐색 깊이 (기본 2)"},
     }},
)
def list_dir(path=".", depth=2):
    p = safe_path(path)
    rc, out = run_cmd(
        ["find", p, "-maxdepth", str(max(1, min(int(depth), 5))),
         "-not", "-path", "*/.git/*", "-not", "-path", "*/build/*"],
        timeout=60)
    return clamp(out.replace(ROOT + os.sep, ""))


@srv.tool(
    "커밋 이력을 조회함. 특정 파일의 변경 경위나 버그 유입 시점을 찾을 때 사용.",
    {"type": "object",
     "properties": {
         "path": {"type": "string", "description": "특정 파일/디렉터리로 한정 (선택)"},
         "limit": {"type": "integer", "description": "커밋 수 (기본 20)"},
         "grep": {"type": "string", "description": "커밋 메시지 검색어 (선택)"},
     }},
)
def git_log(path=None, limit=20, grep=None):
    args = ["log", f"-{max(1, min(int(limit), 200))}",
            "--pretty=format:%h %ad %an %s", "--date=short"]
    if grep:
        args += ["--grep", grep]
    if path:
        safe_path(path)
        args += ["--", path]
    return clamp(_git(args))


@srv.tool(
    "특정 줄 범위를 누가 언제 왜 바꿨는지 조회함. 의심 코드의 도입 커밋을 찾는 용도.",
    {"type": "object",
     "properties": {
         "path": {"type": "string"},
         "start": {"type": "integer"},
         "end": {"type": "integer"},
     },
     "required": ["path"]},
)
def git_blame(path, start=None, end=None):
    safe_path(path)
    args = ["blame", "--date=short", "-w"]
    if start:
        args += ["-L", f"{int(start)},{int(end) if end else int(start) + 40}"]
    args += ["--", path]
    return clamp(_git(args))


@srv.tool(
    "커밋 하나의 전체 diff 를 조회함. git_log/git_blame 으로 찾은 커밋을 확인할 때 사용.",
    {"type": "object",
     "properties": {
         "rev": {"type": "string", "description": "커밋 해시 또는 리비전"},
         "path": {"type": "string", "description": "특정 파일로 한정 (선택)"},
     },
     "required": ["rev"]},
)
def git_show(rev, path=None):
    if not re.fullmatch(r"[\w./~^@{}-]+", rev):
        raise ValueError(f"허용되지 않는 리비전 형식: {rev}")
    args = ["show", "--stat", "-p", rev]
    if path:
        safe_path(path)
        args += ["--", path]
    return clamp(_git(args))


@srv.tool(
    "작업 트리의 변경 사항(diff)을 조회함. 코드리뷰 요청 시 이걸로 대상을 가져올 것.",
    {"type": "object",
     "properties": {
         "staged": {"type": "boolean", "description": "스테이징된 변경만 (기본 false)"},
         "base": {"type": "string", "description": "비교 기준 리비전 (예: origin/main)"},
     }},
)
def git_diff(staged=False, base=None):
    args = ["diff"]
    if staged:
        args.append("--cached")
    if base:
        if not re.fullmatch(r"[\w./~^@{}-]+", base):
            raise ValueError(f"허용되지 않는 리비전 형식: {base}")
        args.append(base)
    return clamp(_git(args))


if __name__ == "__main__":
    srv.run()
