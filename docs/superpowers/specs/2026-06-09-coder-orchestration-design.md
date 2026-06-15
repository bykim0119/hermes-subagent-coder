# 코더 오케스트레이션 확장 — 설계

- **날짜**: 2026-06-09
- **대상**: `hermes-subagent-coder` plugin (stock hermes-agent + 독립 설치형 plugin)
- **상태**: 설계 승인 대기 → writing-plans

## 1. 목적

메인 에이전트(gpt-5.5, `openai-codex` provider)가 **프로젝트 총괄 설계자**로서 서브 코더들을
관찰·관리하고, 작업 간 **의존성에 따라 유동적으로 스케줄링**할 수 있게 한다.

- 보스(사람)는 요구사항을 자연어로 지시한다.
- 메인은 요구를 작업으로 분해하고, **독립 작업은 병렬로, 의존 작업은 직렬로** 코더에게 위임한다.
- 코더가 끝나면 메인이 그 결과를 받아 다음 단계를 판단한다 (기본 체크포인트, 지시 시 자율).

현재는 `delegate_task_background`가 fire-and-forget이라, 메인이 코더를 스폰한 뒤로는
완료 여부·결과를 알 수 없다. 이 확장은 그 "메인의 시야"와 "완료 후 재개" 경로를 채운다.

## 2. 범위 / 비범위

**범위**
- 메인이 호출하는 신규 도구 2개: `coder_status`(관찰), `cancel_coder`(관리).
- 코더 완료 시 메인을 새 턴으로 깨우는 **완료 알림(push)** 경로.
- 코더 과정 로그를 진단용으로 보관하는 **캡처 탭**.

**비범위 (이번 작업 아님)**
- DAG/워크플로 엔진을 plugin에 구현하지 않는다. 의존성 판단·스케줄링은 **메인의 추론**이 담당한다.
- 코더 무한 행(hang) 자동 타임아웃.
- `/code` 슬래시 경로의 stale-status 버그 수정 (우리 도구가 그 경로에 닿지 않으므로 무관).
- 메인 provider 설정·secrets 재입력 등 환경 설정.

## 3. 설계 원칙

1. **지능은 메인에, 도구는 plugin에.** plugin은 스폰·조회·취소·완료알림 primitive만 제공한다.
   무엇이 무엇에 의존하는지, 무엇을 병렬로 돌릴지는 메인(gpt-5.5)의 추론이 결정한다.
2. **stock 완료-알림 패턴 미러링.** 코더를 "백그라운드 프로세스"로 보고, 게이트웨이가 이미 가진
   백그라운드 프로세스 완료 알림(`gateway/run.py:12458–12510`: synthetic 내부 `MessageEvent` +
   `adapter.handle_message`)과 동일 구조로 메인을 깨운다.
3. **전부 외부화 (stock diff 0).** 신규 도구는 `registry.register` + 기존 toolset membership
   헬퍼로 등록하고, 완료 알림은 코더 완료 지점에서 plugin이 주입한다. stock 파일은 수정하지 않는다.

## 4. 아키텍처

```
보스 (Discord 자연어)
   │
   ▼
메인 에이전트 (gpt-5.5) ◀──────────── [코더 완료 알림: synthetic 내부 MessageEvent]
   │  분해·스케줄링(추론)                          ▲
   │                                              │ (코더 완료 시 plugin이 주입)
   ├─ delegate_task_background(goal) ×N  ─────┐   │
   ├─ coder_status(coder_run_id?, include?)   │   │
   └─ cancel_coder(coder_run_id)              │   │
                                             ▼   │
                              코더 A   코더 B   코더 C  (codex 데몬 스레드, 게이트웨이 프로세스 내)
                                 │        │        │
                                 └── 각자 Discord 스레드로 진행 스트리밍 (기존, 사람용)
                                     완료 시 registry에 status/result 기록 (기존)
                                     + 완료 시 메인 깨우기 + 로그 tail 캡처 (신규)
```

**두 가지 "보기"의 구분**

| 보는 주체 | 무엇을 | 상태 |
|---|---|---|
| 보스(사람) | 코더의 실시간 작업 과정 | 기존 — 각 코더의 Discord 스레드 |
| 메인 에이전트 | 코더 완료·결과 → 다음 스케줄링 | 신규 — 본 설계 |

## 5. `/code` 코더 vs 에이전트 코더 — 깔끔한 분리

두 경로는 같은 registry·Discord 스레드 장치를 공유하지만 운전자가 다르다.

| | `/code` 슬래시 | 에이전트 도구 (`delegate_task_background`) |
|---|---|---|
| registry 등록 | O (`_register_coder_run`) | O |
| 실행 메커니즘 | raw codex 서브프로세스 (`_spawn_codex_coder`) | child AIAgent (`_spawn_detached_coder`→`delegate_task`) |
| 완료 시 status/result 기록 | X (계속 "running") | O |
| 메인 오케스트레이션 대상 | **아니오** | 예 |

**결정: 깔끔한 분리.** 신규 도구와 완료 알림은 **에이전트가 띄운 코더만** 대상으로 한다.
`/code` 코더는 메인 시야에서 완전히 제외된다.

- **단일 게이트 기준** = 레코드에 **메인 세션 라우팅 메타데이터**가 있는지.
  이 메타데이터는 에이전트 스폰 경로에서만 저장된다(완료 알림을 메인 세션으로 보내기 위해 어차피 필요).
  그 존재가 곧 "오케스트레이션 대상"을 의미한다.
- `/code` 코더는 이 메타데이터가 없으므로 `coder_status` 목록에서 제외되고, 완료해도 메인을 깨우지 않는다.
- `/code` 코더의 취소·follow-up은 기존대로 Discord 스레드 경로(`!cancel` 등)로만 다룬다.

## 6. 컴포넌트 — 도구 인터페이스

### 6.1 `delegate_task_background` (기존, 변경 없음)
- 입력: `goal`, `context?`
- 반환: `{coder_run_id, status: "spawned", goal}`
- 메인이 작업을 스폰. 독립 작업이면 한 턴에 여러 번 호출(병렬).

### 6.2 `coder_status` (신규, read)
- 입력: `coder_run_id?` (생략 시 전체), `include?` (예: `["result"]`, `["log"]`)
- 반환:
  - `coder_run_id` 생략 → 오케스트레이션 대상 코더 전체 요약 + 용량.
    예: `active 2/3 (여유 1)` + 각 런 `{coder_run_id, goal, status, started_at}`.
  - `coder_run_id` 지정 → 해당 런 상세. `include`에 `result`/`log` 있으면 결과 전문/로그 tail 포함.
- 용도: ① 용량 확인(self-throttle) ② 보스가 "어떻게 돼가?" 물을 때 ③ 과거 결과·로그 재참조.
- 구현: `get_coder_run` + `_CODER_RUN_REGISTRY` 순회. **라우팅 메타데이터 없는 런은 필터 제외.**

### 6.3 `cancel_coder` (신규, action)
- 입력: `coder_run_id`
- 반환: `{cancelled: bool}`
- 용도: 방향이 틀렸거나 멈춰야 할 코더를 메인이 프로그래밍적으로 중단.
- 구현: 기존 `cancel_coder_run` 래핑. **오케스트레이션 대상 코더에만 적용.**
- 참고: 현재 메인은 코더를 취소할 수 없다(취소는 Discord `!cancel`로 사람만 가능). 이는 **새 능력**이다.
  기존 사람 경로(`!cancel`)는 그대로 유지된다.

### 6.4 완료 알림 (도구 아님 — 자동 push)
코더 완료 시 plugin이 메인에게 synthetic 내부 메시지를 주입한다. 결과를 **이미 담아서** 보내므로
흔한 경우 pull 도구 호출이 필요 없다.

- 성공: `[코더 {id} 완료] 작업:{goal} 결과:{result}`
- 실패: `[코더 {id} 실패] 에러:{error} 최근 로그:{log tail}`
- 취소: `[코더 {id} 취소됨]`

## 7. 데이터 흐름 — 완료 깨우기

```
[코더 데몬 스레드]  _spawn_detached_coder._runner
   │ ① delegate_task() 종료 → finally에서 registry에 status/result 기록 (기존)
   │ ② 완료 요약 텍스트 빌드 (성공/실패/취소)
   │ ③ synthetic 내부 MessageEvent(internal=True) 생성
   │      source = 저장해 둔 "메인 세션" 라우팅(chat_id/thread_id/...)  ← 코더 스레드 아님
   ▼
[게이트웨이 이벤트 루프]  run_coroutine_threadsafe(adapter.handle_message(synth_event), loop)
   │      (S3/S6의 _gateway_runner_ref 브리지 재사용)
   ▼
adapter.handle_message → 메인 세션의 정상 턴으로 처리
   │      busy면 _pending_messages 슬롯 + _queued_events FIFO 오버플로에 큐잉 (동시 완료 순서 보장)
   ▼
메인 에이전트 깨어남 → 요약 읽고 다음 단계 판단 (기본 체크포인트 / 지시 시 자율)
```

**핵심 배선 (유일한 신규 부분)**: 스폰 시점에 **메인 세션 라우팅 메타데이터를 registry 레코드에 저장**,
완료 runner가 그것으로 synth 이벤트 `source`를 빌드한다. 스폰 시 메인 source는 이미 가용하다
(S3 `coder_spawn_callback`이 그 컨텍스트로 코더 스레드를 연다). 나머지(스레드→루프 브리지,
대기열, synth 이벤트 처리)는 전부 기존 자산.

## 8. 에러 처리 / 엣지케이스

| 케이스 | 처리 |
|---|---|
| 메인 idle 시 완료 | `adapter.handle_message`가 idle에서도 새 턴 시작(stock `agent_notify` 동작). 정상 깨어남 |
| 메인 busy 시 완료 | `_pending_messages` + FIFO 오버플로에 큐잉 → 현재 턴 후 소화 |
| 동시 완료 (A·C) | 같은 FIFO 대기열로 순서대로 1턴씩 처리. 유실·혼선 없음 |
| 코더 실패 | 알림에 `status=failed` + 에러 + 로그 tail 동봉 |
| 코더 취소 | `[코더 X 취소됨]`으로 깨워 무한 대기 방지. status=cancelled 기록 |
| 중복 알림 방지 | 레코드 `notified` 플래그 → 완료당 정확히 1회 주입 (stock `is_completion_consumed` 패턴) |
| 라우팅/어댑터 분실 | 경고 로그 후 drop(stock `run.py:12480` 미러). 결과는 registry에 남아 `coder_status`로 조회 가능 |
| CLI 모드 | 깨우기 no-op(Discord 없음). 도구 조회는 동작. 기존 게이팅과 일관 |
| 코더 무한 행 | 완료 미발생 → 알림 미발생. 메인/보스가 `coder_status` 확인 후 `cancel_coder`로 정리 (알려진 한계) |

## 9. 로그 캡처 탭

- `_build_coder_progress_sink`에 캡처 추가: 각 이벤트를 `_CODER_RUN_REGISTRY[id]["log"]`
  (bounded deque, 예: 마지막 200개)에 append.
- 현재 sink는 이벤트를 이벤트버스로 흘려보내기만 하고 보관하지 않는다(검증됨).
- `coder_status(..., include=["log"])`가 tail을 반환. 실패 시 완료 알림에 tail을 싣는 데도 사용.

## 10. 테스트 전략

기존 plugin 테스트 패턴(`tests/test_coder_*.py`, fake 어댑터 + 인프로세스 registry 조작 +
stash-compare 회귀)을 따른다.

**단위 테스트 (신규 1~2 파일)**
- 도구 등록: `coder_status`·`cancel_coder`가 registry + delegation toolset에 등록되는지.
- `coder_status`: 가짜 런으로 목록·용량·`include` 플래그 반환 확인.
- `/code` 분리: 라우팅 메타데이터 없는 런이 목록·깨우기에서 제외되는지.
- `cancel_coder`: `cancel_coder_run` 호출·결과 반환, 오케스트레이션 대상에만 적용.
- 완료 깨우기: fake 어댑터로 성공/실패/취소 3종 요약 + 올바른 메인 라우팅으로 `handle_message`
  1회 호출 확인.
- 중복 방지: 완료 두 번 → 주입 1회.
- 로그 캡처: sink가 deque에 쌓고 `include=log`가 tail 반환.

**회귀 / 무결성**
- stash-compare: 신규 wrap 적용 전후 기존 코더 + plugins/tools/toolsets baseline 동일(회귀 0).
- stock diff 0: `git diff <fork base> -- <stock 파일들>` 빈 출력.

**라이브 스모크 (마지막, 수동·secrets 필요)**
- Discord에서 의존성 있는 작업 지시 → 메인이 병렬+직렬 스케줄 → 완료 깨우기로 이어감 → 종합까지
  end-to-end. `~/.hermes/logs/agent.log`로 깨우기 체인 모니터.

## 11. 미해결 / 구현 시 확정할 항목

- `coder_status` 반환 포맷의 정확한 필드·문자열 형태 (구현 중 메인이 잘 파싱하는 형태로 확정).
- 완료 요약 텍스트의 정확한 문구 (메인이 체크포인트/자율을 잘 구분하도록 프롬프트 톤 조정).
- `log` deque 크기(기본 200) 및 tail 기본 길이 — 라이브에서 토큰량 보며 조정.
- 메인 세션 라우팅 메타데이터를 레코드에 싣는 정확한 키/경로 (스폰 컨텍스트 실측 후 확정).
