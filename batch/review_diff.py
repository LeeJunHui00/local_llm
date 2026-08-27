#!/usr/bin/env python3
"""diff 기반 C/C++ 코드리뷰 배치 - API 직접 호출 예시.

에이전트 없이 API 만으로 할 수 있는 자동화의 최소 형태임.
여기서 파생해 커밋 메시지 생성 훅, 릴리스 노트 생성, 크래시 리포트 분류 등을 만들면 됨.

표준 라이브러리만 사용함 (폐쇄망 패키지 반입 불필요).

사용법
  source ~/.llm-env
  python3 batch/review_diff.py                      # 작업 트리 변경분
  python3 batch/review_diff.py --staged             # 스테이징된 변경분
  python3 batch/review_diff.py --base origin/main   # 브랜치 전체
  python3 batch/review_diff.py --json               # 기계 판독용 출력

git pre-commit 훅으로 쓰려면 --staged 로 걸되, 실패해도 커밋은 막지 말 것
(LLM 리뷰는 참고용이지 게이트가 아님).
"""

import argparse
import json
import os
import ssl
import subprocess
import sys
import urllib.request

# C/C++ 네이티브에서 실제로 사고가 나는 항목만 남긴 체크리스트.
# 일반적인 "좋은 코드" 조언은 노이즈만 늘리므로 의도적으로 제외함.
SYSTEM = """너는 C/C++ 네이티브 코드 리뷰어다. 주어진 diff 만 보고 결함을 찾는다.

우선순위 체크리스트:
1. 메모리 - use-after-free, double free, 누수, 소유권 불명확, 해제 후 콜백 진입
2. 버퍼 - off-by-one, 경계 검사 누락, strcpy/sprintf 류, sizeof 오용
3. UB - 미초기화 변수, 시그널드 오버플로, strict aliasing 위반, 정렬되지 않은 접근
4. 에러 경로 - 반환값 미확인, early return 시 자원 누수, errno 덮어쓰기
5. 동시성 - 락 범위 오류, 락 순서 역전, 원자성 없는 read-modify-write
6. 이식성 - 타입 크기 가정, 엔디안 가정, 부호 있는/없는 비교

규칙:
- diff 에 보이는 근거만으로 판단한다. 추측성 지적 금지.
- 스타일/네이밍/취향 지적 금지.
- 확신이 낮으면 confidence 를 low 로 표기하고, 확인에 필요한 파일을 needs 에 적는다.
- 결함이 없으면 빈 배열을 반환한다.

출력은 JSON 배열만. 다른 텍스트 금지.
[{"file":"경로","line":숫자,"severity":"high|medium|low","category":"메모리|버퍼|UB|에러경로|동시성|이식성","issue":"한 줄 요약","detail":"근거와 재현 조건","fix":"수정 방향","confidence":"high|medium|low","needs":"추가로 봐야 할 파일 (없으면 빈 문자열)"}]
"""


def sh(args):
    p = subprocess.run(args, capture_output=True, text=True, errors="replace")
    if p.returncode != 0 and not p.stdout:
        # git 은 실패 시 사용법 전체를 뱉어 원인이 묻히므로 앞부분만 남김
        err = "\n".join((p.stderr or "").strip().splitlines()[:5])
        sys.exit(f"명령 실패: {' '.join(args)}\n{err}")
    return p.stdout


def call_llm(diff, model, max_tokens=4096):
    base = os.environ.get("OPENAI_BASE_URL")
    key = os.environ.get("OPENAI_API_KEY", "")
    anthropic_base = os.environ.get("ANTHROPIC_BASE_URL")

    if base:  # OpenAI 호환
        url = f"{base.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {key}"}
        body = {"model": model, "max_tokens": max_tokens, "temperature": 0,
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": diff}]}
        pick = lambda r: r["choices"][0]["message"]["content"]
    elif anthropic_base:  # Anthropic 호환
        url = f"{anthropic_base.rstrip('/')}/v1/messages"
        headers = {"Content-Type": "application/json",
                   "x-api-key": os.environ.get("ANTHROPIC_AUTH_TOKEN", ""),
                   "anthropic-version": "2023-06-01"}
        body = {"model": model, "max_tokens": max_tokens, "temperature": 0,
                "system": SYSTEM,
                "messages": [{"role": "user", "content": diff}]}
        pick = lambda r: "".join(b.get("text", "") for b in r["content"])
    else:
        sys.exit("OPENAI_BASE_URL 또는 ANTHROPIC_BASE_URL 이 필요함. setup/env.sh.example 참고")

    ctx = None
    if os.environ.get("LLM_INSECURE_SSL") == "1":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=300, context=ctx) as resp:
        return pick(json.loads(resp.read().decode()))


def parse_findings(text):
    """모델이 JSON 앞뒤에 설명을 붙이는 경우가 흔하므로 배열만 잘라냄."""
    s, e = text.find("["), text.rfind("]")
    if s == -1 or e == -1:
        return None, text
    try:
        return json.loads(text[s:e + 1]), None
    except json.JSONDecodeError as err:
        return None, f"JSON 파싱 실패: {err}\n원문:\n{text[:1000]}"


SEV_ORDER = {"high": 0, "medium": 1, "low": 2}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--staged", action="store_true")
    ap.add_argument("--base", help="비교 기준 리비전 (예: origin/main)")
    ap.add_argument("--model", default=os.environ.get("LLM_MODEL"),
                    help="모델 ID. 미지정 시 $LLM_MODEL 사용")
    ap.add_argument("--max-diff-bytes", type=int, default=60000,
                    help="이보다 크면 파일 단위로 쪼개 호출 (기본 60000)")
    ap.add_argument("--json", action="store_true", help="JSON 으로 출력")
    a = ap.parse_args()

    # env.sh.example 의 자리표시자를 그대로 source 한 경우까지 걸러냄.
    # 자리표시자를 그냥 보내면 API 400 만 떨어져서 원인 파악이 오래 걸림.
    if not a.model or a.model.startswith("<"):
        sys.exit("모델 ID 가 없음. --model 로 지정하거나 $LLM_MODEL 을 설정할 것.\n"
                 "사용 가능한 ID 는 `python3 setup/capability-check.py` 로 확인 가능함")

    if subprocess.run(["git", "rev-parse", "--git-dir"],
                      capture_output=True).returncode != 0:
        sys.exit("git 저장소가 아님. 저장소 안에서 실행할 것")

    args = ["git", "diff", "-U10"]
    if a.staged:
        args.append("--cached")
    if a.base:
        args.append(a.base)
    # 소스 파일만. 바이너리/생성물/서드파티는 리뷰 대상이 아님
    args += ["--", "*.c", "*.cc", "*.cpp", "*.cxx", "*.h", "*.hpp", "*.hh"]
    diff = sh(args)

    if not diff.strip():
        print("리뷰할 C/C++ 변경 없음")
        return

    # 큰 diff 는 파일 단위로 쪼갬. 통째로 넣으면 사내 모델 컨텍스트를 넘기거나
    # 뒷부분 파일을 대충 보게 됨.
    chunks = [diff]
    if len(diff) > a.max_diff_bytes:
        chunks = ["diff --git" + c for c in diff.split("diff --git") if c.strip()]
        print(f"[i] diff {len(diff)}바이트 -> 파일 단위 {len(chunks)}건으로 분할 호출",
              file=sys.stderr)

    findings, errors = [], []
    for i, c in enumerate(chunks, 1):
        if len(chunks) > 1:
            print(f"[i] {i}/{len(chunks)} 처리 중...", file=sys.stderr)
        got, err = parse_findings(call_llm(c, a.model))
        if err:
            errors.append(err)
        else:
            findings.extend(got)

    findings.sort(key=lambda f: SEV_ORDER.get(f.get("severity", "low"), 3))

    if a.json:
        print(json.dumps({"findings": findings, "errors": errors},
                         ensure_ascii=False, indent=2))
        return

    if not findings:
        print("지적 사항 없음" + (f" (파싱 실패 {len(errors)}건)" if errors else ""))
    for f in findings:
        loc = f"{f.get('file','?')}:{f.get('line','?')}"
        print(f"\n[{f.get('severity','?').upper()}/{f.get('category','?')}] {loc}"
              f"  (확신도 {f.get('confidence','?')})")
        print(f"  {f.get('issue','')}")
        if f.get("detail"):
            print(f"  근거: {f['detail']}")
        if f.get("fix"):
            print(f"  수정: {f['fix']}")
        if f.get("needs"):
            print(f"  확인 필요: {f['needs']}")
    for e in errors:
        print(f"\n[!] {e}", file=sys.stderr)

    print(f"\n총 {len(findings)}건. LLM 리뷰는 참고용이며 사람 리뷰를 대체하지 않음.")


if __name__ == "__main__":
    main()
