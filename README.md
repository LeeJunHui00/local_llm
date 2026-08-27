# local_llm

폐쇄망 사내 LLM 을 **C/C++ 네이티브 개발 업무**에 붙이기 위한 개인용 툴킷.

사내에 도입되는 LLM 이 CLI 코딩 에이전트 / MCP / OpenAI 호환 API 를 지원한다는 전제로,
스펙 점검부터 MCP 서버, 배치 자동화까지 실제로 돌아가는 형태로 구성함.

**모든 코드가 Python 표준 라이브러리만 사용함.** 폐쇄망에 패키지 wheel 을 반입하지 않아도
그대로 동작하는 것을 최우선 제약으로 뒀음. MCP 프로토콜도 SDK 없이 직접 구현함.

## 무엇이 들어 있나

| 구성 | 내용 |
|---|---|
| **스펙 점검** | tool calling / 컨텍스트 / system prompt / streaming 지원 여부를 실측 |
| **MCP 서버 3종** | 코어덤프 분석(gdb), 코드 검색·git 이력, 빌드 에러 구조화 |
| **활용 가이드** | C/C++ 실전 패턴 8종, 약한 모델을 전제로 한 사용법 |
| **템플릿** | 접속 환경변수(프록시·사설 CA 포함), 저장소 규칙 파일 |
| **배치 자동화** | diff 기반 코드리뷰 (API 직접 호출) |

## 빠른 시작

```bash
# 0. 접속 세팅
cp setup/env.sh.example ~/.llm-env
$EDITOR ~/.llm-env && source ~/.llm-env

# 1. 사내 LLM 스펙 점검 - tool calling 지원 여부가 여기서 갈림
python3 setup/capability-check.py

# 2. MCP 서버 단독 검증 (에이전트에 붙이기 전)
export LLM_REPO_ROOT=~/work/myproject
python3 mcp/smoke_test.py repo_mcp.py --call build_tags '{}'
bash mcp/verify_gdb.sh

# 3. 에이전트에 등록  ->  mcp/README.md
```

**tool calling 이 미지원이면** MCP 와 에이전트는 의미가 없음. 그 경우
[`GUIDE.md`](GUIDE.md) 4장의 프롬프트 패턴과 `batch/` 배치 자동화만 사용할 것.

## 설계 원칙

- **의존성 0** — 표준 라이브러리만. 폐쇄망 반입 절차를 타지 않음
- **읽기 전용 우선** — gdb 는 실행 제어 없이 정적 분석만, 저장소 접근은 조회만
- **토큰 절약** — 원문 로그 대신 구조화된 진단, 파일 통째 대신 줄 범위
- **약한 모델 전제** — 컨텍스트가 좁고 환각이 잦다고 가정하고 설계

## 문서

- [`GUIDE.md`](GUIDE.md) — 활용 가이드 본문. 먼저 읽을 것
- [`mcp/README.md`](mcp/README.md) — MCP 등록·검증·문제 해결
- [`setup/PROJECT_RULES.md`](setup/PROJECT_RULES.md) — 저장소 규칙 파일 템플릿

## 구조

```
GUIDE.md                    활용 가이드 본문
setup/env.sh.example        접속 환경변수 템플릿 (프록시·사설 CA 포함)
setup/PROJECT_RULES.md      저장소용 규칙 파일 템플릿
setup/capability-check.py   LLM 스펙 점검
mcp/_mcplib.py              의존성 없는 MCP stdio 구현
mcp/gdb_mcp.py              코어덤프 분석 (읽기 전용)
mcp/repo_mcp.py             코드 검색·심볼·git 이력 (읽기 전용)
mcp/build_mcp.py            빌드 실행·에러 구조화
mcp/smoke_test.py           MCP 서버 단독 검증
mcp/verify_gdb.sh           gdb MCP 실동작 검증
batch/review_diff.py        diff 코드리뷰 배치
```

## 요구 사항

- Python 3.8+
- 선택: `gdb`(코어덤프 분석), `ctags`(심볼 인덱스), `ripgrep`(없으면 `grep` 폴백)

## 참고

문서의 호스트명(`llm.example.internal`)과 모델 ID(`<모델ID>`)는 자리표시자임.
실제 값은 `setup/capability-check.py` 로 확인해 채울 것.
