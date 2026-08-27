# MCP 서버

C/C++ 네이티브 업무용 MCP 서버 3종. 표준 라이브러리만 사용하므로 폐쇄망에
`mcp` SDK wheel 을 반입하지 않아도 동작함. 프로토콜 구현은 `_mcplib.py` 에 있음.

| 서버 | 역할 | 우선순위 |
|---|---|---|
| `gdb_mcp.py` | 코어덤프/바이너리 분석 (읽기 전용) | 1 — 네이티브 업무에서 효과 최대 |
| `repo_mcp.py` | 코드 검색, 심볼 조회, git 이력 (읽기 전용) | 2 |
| `build_mcp.py` | 빌드 실행, 컴파일 에러 구조화 | 3 |

## 등록

에이전트 설정 파일(`.mcp.json` 등)에 추가:

```json
{
  "mcpServers": {
    "gdb": {
      "command": "python3",
      "args": ["/절대경로/local_llm/mcp/gdb_mcp.py"],
      "env": { "LLM_GDB": "gdb", "LLM_GDB_TIMEOUT": "60" }
    },
    "repo": {
      "command": "python3",
      "args": ["/절대경로/local_llm/mcp/repo_mcp.py"],
      "env": { "LLM_REPO_ROOT": "/home/me/work/myproject" }
    },
    "build": {
      "command": "python3",
      "args": ["/절대경로/local_llm/mcp/build_mcp.py"],
      "env": {
        "LLM_BUILD_DIR": "/home/me/work/myproject/build",
        "LLM_REPO_ROOT": "/home/me/work/myproject",
        "LLM_BUILD_CMD": "cmake --build . -j"
      }
    }
  }
}
```

`LLM_REPO_ROOT` 를 `repo` 와 `build` 양쪽에 동일하게 주는 것이 중요함. 그래야
컴파일 에러 경로와 파일 읽기 경로 표기가 일치해서, 모델이 에러 위치를 바로
열어볼 수 있음.

전체 환경변수는 `../setup/env.sh.example` 참고.

## 단독 검증

에이전트에 붙이기 전에 서버 자체가 정상인지 먼저 확인할 것. 에이전트 쪽에서는
실패 원인이 안 보임.

```bash
# 핸드셰이크 + 도구 목록
python3 mcp/smoke_test.py repo_mcp.py

# 도구 실제 호출
export LLM_REPO_ROOT=~/work/myproject
python3 mcp/smoke_test.py repo_mcp.py --call build_tags '{}'
python3 mcp/smoke_test.py repo_mcp.py --call find_symbol '{"name":"main"}'
python3 mcp/smoke_test.py repo_mcp.py --call search_code '{"pattern":"strcpy","glob":"*.c"}'

export LLM_BUILD_DIR=~/work/myproject/build LLM_REPO_ROOT=~/work/myproject
python3 mcp/smoke_test.py build_mcp.py --call build '{}'

# gdb 는 전용 검증 스크립트 사용 (크래시 바이너리 + 코어덤프 자동 생성)
bash mcp/verify_gdb.sh
```

## 설계상 의도적으로 뺀 것

- **gdb 실행 제어** (`run`/`continue`/`attach`/`set var`): 에이전트가 프로세스를
  붙잡거나 메모리를 바꾸는 사고를 막기 위함. 정적 분석만으로 크래시 원인의
  대부분은 잡힘. 라이브 디버깅은 사람이 직접 할 것.
- **파일 쓰기**: `repo_mcp` 는 읽기 전용임. 코드 수정은 에이전트의 기본 편집 도구가
  담당하며, 그래야 diff 검토 흐름을 탈 수 있음.
- **임의 셸 실행**: `build_mcp` 는 미리 지정된 `LLM_BUILD_CMD` 만 실행함.

## 안전장치

- `repo_mcp`: 저장소 루트 밖 경로 차단(심볼릭 링크 탈출 포함), `LLM_REPO_DENY` 로
  추가 차단, git 리비전 문자열 형식 검증
- `gdb_mcp`: `shell`/`pipe`/`python`/`run`/`attach` 등 gdb 내장 실행 명령 차단,
  개행 인젝션 차단, 표현식 대입 차단, 호출당 타임아웃
- `build_mcp`: 타깃 이름 형식 검증, 빌드 타임아웃
- 공통: 도구 출력 길이 상한(`LLM_MCP_MAX_OUTPUT`), 도구 예외는 서버를 죽이지 않고
  `isError` 결과로 반환 (모델이 스스로 복구 가능)

## 문제 해결

| 증상 | 원인 / 조치 |
|---|---|
| `실행 파일을 찾을 수 없음: rg` | ripgrep 미설치. 자동으로 `grep` 폴백되지만, 셸 함수/별칭으로만 존재하는 `rg` 는 인식 안 됨. 실제 바이너리를 설치하거나 `LLM_RG` 로 경로 지정 |
| `ctags 인덱스가 없음` | `build_tags` 를 한 번 호출. 코드가 크게 바뀌면 재호출 |
| 백트레이스가 `??` 만 나옴 | 배포 바이너리가 strip 됨. 심볼 포함본을 `load_core` 의 `binary` 로 지정. `info_libs` 로 라이브러리 심볼 적재 상태 확인 |
| 크로스 타깃 코어가 안 열림 | `LLM_GDB` 를 크로스 gdb 로, `LLM_GDB_SYSROOT`/`LLM_GDB_SOLIB_PATH` 설정 |
| 도구 출력이 잘림 | `LLM_MCP_MAX_OUTPUT` 상향. 단, 모델 컨텍스트를 넘기면 오히려 정확도가 떨어짐 |
| 서버가 즉시 종료됨 | `smoke_test.py` 로 stderr 확인. 보통 환경변수 경로 오타 |
