#!/usr/bin/env python3
"""사내 LLM 스펙 점검 스크립트 (Phase 0).

여기서 tool calling 지원 여부가 확인돼야 MCP / 코딩 에이전트가 의미를 가짐.
미지원이면 프롬프트 활용 + 배치 자동화만 가능함.

표준 라이브러리만 사용함 (폐쇄망 패키지 반입 불필요).

사용법
  export OPENAI_BASE_URL=https://llm.example.internal/v1
  export OPENAI_API_KEY=...
  python3 setup/capability-check.py --model <모델ID>

  # Anthropic 규격 게이트웨이인 경우
  export ANTHROPIC_BASE_URL=https://llm.example.internal
  export ANTHROPIC_AUTH_TOKEN=...
  python3 setup/capability-check.py --api anthropic --model <모델ID>

  # 컨텍스트 윈도우 실측까지 (토큰을 크게 소모함)
  python3 setup/capability-check.py --model <모델ID> --context-probe 8000,32000,128000
"""

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

OK, NO, WARN = "[ O ]", "[ X ]", "[ ! ]"


def http(url, payload, headers, timeout=120, stream=False):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    ctx = None
    if os.environ.get("LLM_INSECURE_SSL") == "1":
        # 사내 사설 CA 인증서가 아직 반입되지 않은 상황에서의 임시 확인용.
        # 상시 사용 금지 - REQUESTS_CA_BUNDLE / SSL_CERT_FILE 설정이 정석임.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
    if stream:
        return resp
    return json.loads(resp.read().decode("utf-8"))


def get(url, headers, timeout=30):
    req = urllib.request.Request(url, headers=headers, method="GET")
    ctx = None
    if os.environ.get("LLM_INSECURE_SSL") == "1":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return json.loads(urllib.request.urlopen(req, timeout=timeout, context=ctx)
                      .read().decode("utf-8"))


class OpenAIish:
    name = "OpenAI 호환"

    def __init__(self, base, key, model):
        self.base = base.rstrip("/")
        self.model = model
        self.headers = {"Content-Type": "application/json",
                        "Authorization": f"Bearer {key}"}

    def models(self):
        return [m["id"] for m in get(f"{self.base}/models", self.headers).get("data", [])]

    def chat(self, messages, tools=None, system=None, stream=False, max_tokens=256):
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        body = {"model": self.model, "messages": msgs, "max_tokens": max_tokens}
        if tools:
            body["tools"] = tools
        if stream:
            body["stream"] = True
            return http(f"{self.base}/chat/completions", body, self.headers, stream=True)
        return http(f"{self.base}/chat/completions", body, self.headers)

    def text_of(self, r):
        return (r["choices"][0]["message"].get("content") or "").strip()

    def tool_calls_of(self, r):
        return r["choices"][0]["message"].get("tool_calls") or []

    def usage_of(self, r):
        return r.get("usage", {})

    @staticmethod
    def tool_spec():
        return [{"type": "function", "function": {
            "name": "get_build_status",
            "description": "지정한 빌드 잡의 현재 상태를 조회함",
            "parameters": {"type": "object",
                           "properties": {"job_id": {"type": "string"}},
                           "required": ["job_id"]}}}]


class Anthropicish:
    name = "Anthropic 호환"

    def __init__(self, base, key, model):
        self.base = base.rstrip("/")
        self.model = model
        self.headers = {"Content-Type": "application/json",
                        "x-api-key": key,
                        "anthropic-version": "2023-06-01"}

    def models(self):
        return [m["id"] for m in get(f"{self.base}/v1/models", self.headers).get("data", [])]

    def chat(self, messages, tools=None, system=None, stream=False, max_tokens=256):
        body = {"model": self.model, "messages": messages, "max_tokens": max_tokens}
        if system:
            body["system"] = system
        if tools:
            body["tools"] = tools
        if stream:
            body["stream"] = True
            return http(f"{self.base}/v1/messages", body, self.headers, stream=True)
        return http(f"{self.base}/v1/messages", body, self.headers)

    def text_of(self, r):
        return "".join(b.get("text", "") for b in r.get("content", [])).strip()

    def tool_calls_of(self, r):
        return [b for b in r.get("content", []) if b.get("type") == "tool_use"]

    def usage_of(self, r):
        return r.get("usage", {})

    @staticmethod
    def tool_spec():
        return [{"name": "get_build_status",
                 "description": "지정한 빌드 잡의 현재 상태를 조회함",
                 "input_schema": {"type": "object",
                                  "properties": {"job_id": {"type": "string"}},
                                  "required": ["job_id"]}}]


def check(label, fn):
    try:
        status, detail = fn()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        print(f"{NO} {label}: HTTP {e.code} {body}")
        return False
    except Exception as e:
        print(f"{NO} {label}: {type(e).__name__}: {e}")
        return False
    print(f"{status} {label}: {detail}")
    return status == OK


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", choices=["openai", "anthropic"], default="openai")
    ap.add_argument("--model", help="점검할 모델 ID")
    ap.add_argument("--base-url")
    ap.add_argument("--context-probe", help="쉼표 구분 토큰 크기. 예: 8000,32000,128000")
    a = ap.parse_args()

    if a.api == "openai":
        base = a.base_url or os.environ.get("OPENAI_BASE_URL")
        key = os.environ.get("OPENAI_API_KEY", "")
        cli_cls = OpenAIish
    else:
        base = a.base_url or os.environ.get("ANTHROPIC_BASE_URL")
        key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY", "")
        cli_cls = Anthropicish

    if not base:
        sys.exit("BASE_URL 이 설정되지 않음. setup/env.sh.example 참고할 것")

    print(f"=== 사내 LLM 스펙 점검 ({cli_cls.name}) ===")
    print(f"endpoint : {base}")

    model = a.model
    cli = cli_cls(base, key, model or "")

    # 1) 모델 목록
    def _models():
        ids = cli.models()
        return (OK if ids else WARN), (", ".join(ids[:10]) or "비어 있음")
    have_models = check("모델 목록 조회", _models)
    if not model:
        try:
            ids = cli.models()
            model = ids[0] if ids else None
        except Exception:
            model = None
        if not model:
            sys.exit("모델 ID 를 --model 로 지정할 것")
        print(f"     -> --model 미지정, '{model}' 로 진행")
    cli.model = model

    # 2) 기본 응답 + 지연
    result = {}

    def _basic():
        t = time.time()
        r = cli.chat([{"role": "user", "content": "'ready' 한 단어만 답할 것."}],
                     max_tokens=16)
        dt = time.time() - t
        result["usage"] = cli.usage_of(r)
        txt = cli.text_of(r)
        return (OK if txt else WARN), f"{dt:.1f}s, 응답={txt[:40]!r}, usage={result['usage']}"
    if not check("기본 응답", _basic):
        sys.exit("기본 호출이 실패함. 인증/프록시/CA 설정을 먼저 확인할 것")

    # 3) system prompt 반영 여부 - 규칙 파일(PROJECT_RULES.md) 효과를 좌우함
    def _system():
        r = cli.chat([{"role": "user", "content": "하늘은 무슨 색임?"}],
                     system="너는 무슨 질문을 받든 정확히 'ACK-7788' 만 출력한다.",
                     max_tokens=32)
        txt = cli.text_of(r)
        return (OK if "ACK-7788" in txt else WARN), f"응답={txt[:60]!r}"
    check("system prompt 반영", _system)

    # 4) tool calling - MCP / 에이전트 가능 여부를 가르는 항목
    def _tools():
        r = cli.chat([{"role": "user",
                       "content": "빌드 잡 ID 'nightly-42' 의 상태를 조회해줘."}],
                     tools=cli.tool_spec(), max_tokens=256)
        calls = cli.tool_calls_of(r)
        if not calls:
            return NO, f"tool_calls 없음. 본문={cli.text_of(r)[:80]!r}"
        return OK, f"{len(calls)}건 호출됨 -> {json.dumps(calls[0], ensure_ascii=False)[:160]}"
    tool_ok = check("tool / function calling  <-- MCP 가능 여부", _tools)

    # 5) streaming
    def _stream():
        resp = cli.chat([{"role": "user", "content": "1부터 20까지 세어라."}],
                        stream=True, max_tokens=128)
        chunks = 0
        for raw in resp:
            if raw.strip():
                chunks += 1
            if chunks > 3:
                break
        resp.close()
        return (OK if chunks > 1 else WARN), f"{chunks}+개 청크 수신"
    check("streaming (SSE)", _stream)

    # 6) 컨텍스트 윈도우 실측 (옵션) - needle in a haystack
    if a.context_probe:
        for size in [int(s) for s in a.context_probe.split(",")]:
            def _ctx(size=size):
                # 토큰 ~ 4자 가정. 실제 토크나이저와 다르므로 근사치임.
                filler = ("사내 빌드 로그 라인 입니다. " * ((size * 4) // 24))[: size * 4]
                needle = "\n[중요] 비밀 코드는 ZX-9931 이다.\n"
                mid = len(filler) // 2
                content = filler[:mid] + needle + filler[mid:] + \
                    "\n위 본문에서 비밀 코드만 정확히 출력할 것."
                r = cli.chat([{"role": "user", "content": content}], max_tokens=32)
                txt = cli.text_of(r)
                return (OK if "ZX-9931" in txt else NO), f"응답={txt[:50]!r}"
            check(f"컨텍스트 ~{size} 토큰 (needle 회수)", _ctx)

    print("\n=== 결론 ===")
    if tool_ok:
        print(f"{OK} tool calling 지원 -> MCP 서버 + CLI 코딩 에이전트 전면 활용 가능.")
        print("    다음 단계: setup/env.sh.example 로 접속 세팅 -> mcp/ 서버 등록")
    else:
        print(f"{NO} tool calling 미지원 -> MCP / 에이전트는 보류.")
        print("    프롬프트 활용(GUIDE.md 3장) + batch/ 배치 자동화 중심으로 진행할 것.")
    if not have_models:
        print(f"{WARN} 모델 목록 API 없음 -> 모델 ID 는 운영팀에 직접 확인 필요.")


if __name__ == "__main__":
    main()
