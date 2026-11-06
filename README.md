# experiment-30 후속 수정 제안

정리: 박천웅 (IRV_Lab) · 2026-11-06
대상 코드: `unified-packages` (engine `be41210`, Agent_Reasoning `4abf80f`, GM `66a0230`) — experiment-30을 만든 바로 그 버전

> 이 폴더는 **제안**입니다. 원본 저장소는 건드리지 않았습니다. 각 담당자가 검토 후 자기 저장소에 적용해 주세요.

---

## 1. 한 줄 요약

experiment-30의 30판은 **같은 에이전트의 30번째 게임이 아니라, 서로 다른 에이전트들의 첫 게임 30번**이었습니다. 게다가 에이전트는 실제 마감·자기 스킬·자기 행동 이력을 모른 채 판단했습니다. 엔진과 GM에는 영속 풀이 이미 구현돼 있으므로, **실행기 한 곳과 B의 기억 기능만 연결하면** "풀의 같은 에이전트가 매 시나리오 다시 불리고 이전 경험을 인지"하게 됩니다.

## 2. 무엇이 문제였나 (experiment-30 근거)

| 관찰 | 수치 | 원인 |
|---|---|---|
| 30판 전부 `time_out`, 고발 0건 | 12~15사이클에서 종료 | 1시간 제한이 드라이버에만 있고, 에이전트에게는 `remaining_cycles` 106~108로 보임 |
| 판마다 에이전트가 다름 | 시드 간 같은 에이전트 **0/16** | `run_batch.py`가 시드마다 새 DB → 엔진이 매번 새 풀(새 ID, 빈 기록)을 만듦 |
| 이전 경험을 모름 | 프롬프트에 경험 필드 없음 | B가 엔진의 `history`·`pool/learning`을 한 번도 읽지 않음 |
| 정보 부족 | 종료 시 남은 조합 평균 536/768 | 행동 이력 없음 → 후반 탐색의 약 40%가 이미 다 뒤진 방 |
| 스킬 미활용 | 대령 협박 0회 등 | 프롬프트 기준선이 `8−스탯`만 계산, 스킬·방·관계 보정 누락 |
| MBTI 효과 없음 | 4축 행동 차이 ≈ 0 | MBTI가 라벨 한 줄. 기획서 B7 경향표 미구현 |
| 간접 단서 효과 미미 | 간접 단서로 줄어든 후보 에이전트당 0.25개 | 추론 규칙은 belief에 구현돼 있으나 모델은 규칙을 모르고, 질문을 어디로 할지 안내가 없음 |

### 핵심 확인 실험: 영속 풀은 이미 동작한다

수정 없이 원본 코드로 3판을 **같은 DB**에 돌리면, 같은 17개 에이전트가 유지되고 16명이 2~3판씩 순환하며 판마다 다른 캐릭터를 맡습니다. 엔진은 판마다 `history`를 쌓습니다. 즉 끊긴 곳은 ① 실행기가 DB를 공유하지 않은 것, ② B가 기록을 읽지 않은 것, 두 군데입니다.

## 3. 담당자별 변경 사항

코드 안의 `[FIX-xx]` 주석으로 위치를 찾을 수 있습니다.

### A. Game-engine (`patches/Game-engine.patch`)

| ID | 파일 | 변경 | 이유 |
|---|---|---|---|
| A1 | competitive.py | 규칙 `time_limit_seconds`(기본 0=끔) 추가. 관측에 `time_budget` 노출: 경과·남은 초, 평균 사이클 시간, **실제 남은 사이클 추정치**, 어떤 제한이 먼저 오는지, 만료 시 전원 패배 | 에이전트가 106사이클 남았다고 믿은 채 끝남 |
| A2 | competitive.py | 기준선 계산을 `threshold()`로 분리해 주사위 판정과 관측이 같은 함수를 씀. 관측에 `thresholds` 노출: 탐색, 같은 방 인물별·접근 방식별 기준선 | 스킬이 엔진에만 있고 플레이어는 몰랐음 |
| A3 | competitive.py | 관측에 `scoring`(실제 점수표) 노출. 규칙 `unsolved_score`(기본 0 = 기존 v1.3과 동일) 추가 | 점수표가 플레이어에게 안 보였음. 옵션은 6장 참고 |
| — | test_fixes.py | 위 세 항목 테스트 6개 | |

기존 동작은 바뀌지 않습니다. 기본값이 기존과 같고, 기존 엔진 테스트 160개가 그대로 통과합니다.

### B. Agent_Reasoning (`patches/Agent_Reasoning.patch`)

| ID | 파일 | 변경 |
|---|---|---|
| **B1** | memory.py(신규), adapter.py, agent.py | **판 간 경험.** 판 시작 시 `pool/learning/get`과 로컬 기억을 읽어 프롬프트 `experience`에 넣음(지난 판 직업·결과·점수·사이클·고발 여부·남은 조합·헛탐색, 스스로 쓴 교훈, 엔진 전술·위험 가중치). 판 종료 시 통계·공식 피드백·규칙 기반 교훈을 저장하고 엔진 `strategy_state`를 갱신함. 다음 판 첫 결정 전에 LLM 회고 1회로 교훈을 다듬음 |
| B2 | agent.py | 프롬프트에 `time_budget`·`scoring`·`accuse_math`(남은 조합 수, 무작위 고발 성공 확률) 추가. "확신이 없으면 단서를 더 모아라" 한 줄을 "성공 확률·점수표·실제 남은 사이클을 비교해 정하라"로 교체 |
| B3 | agent.py | 기준선은 엔진 값을 우선 사용. 구버전 엔진이면 스탯 기반 값을 쓰되 그렇다고 명시 |
| B4 | agent.py | 판 안의 기억: `recent_actions`(내 행동·결과), `room_notes`(방별 탐색 횟수·발견 수·소진 여부). 단서 `type` 필드 보존 |
| B5 | agent.py | 기획서 B7 MBTI 경향표를 `me.tendencies`로 제공. 지시가 아닌 경향으로 |
| B6 | agent.py | 서브 미션에 카탈로그의 이름·조건 첨부(기존에는 `"M20"` 같은 ID만 전달) |
| B7 | agent.py | `belief.remaining()`을 인자 없이 부르던 3곳 수정(호출되면 TypeError) |
| B8 | schema.py, agent.py | 간접 단서: 자정 규칙을 프롬프트에 명시. belief 요약에 `indirect_leverage`(장소 하나가 제외될 때 함께 빠지는 인물·흉기) 추가 → 질문할 장소를 고르는 나침반. 제외 판정 자체는 바꾸지 않아 엔진 `infer()`와의 1:1 대응 유지 |
| B9 | agent.py, adapter.py | 모델 호출 실패로 규칙 기반 대체 행동을 한 횟수를 집계. 기존에는 모든 호출이 실패해도 `player_fallbacks = 0`으로 보였음 |

### C. GM_Agent_Society (`patches/GM_Agent_Society.patch`)

| ID | 파일 | 변경 |
|---|---|---|
| C1 | agent_runtime.py | 플레이어 팩토리에 그 풀 멤버 자신의 풀 자격 증명(`session["pool_credentials"]`) 전달. 팩토리 시그니처는 그대로 |
| C2 | run_scenario.py | `--time-limit`을 엔진 규칙 `time_limit_seconds`로도 전달 |
| C3 | run_scenario.py | 판 종료 훅: 공식 종료 후 각 플레이어 런타임의 `on_game_end(state)` 호출. 실패해도 공식 기록에는 영향 없음 |
| C4 | run_scenario.py | `--data` 상대 경로를 절대 경로로 변환(엔진이 `cwd=Game-engine`에서 돌아 엉뚱한 곳에 DB가 생겼음) |
| C5 | run_scenario.py | 요약에 `model_calls`, `model_fallbacks` 추가 |
| — | competitive_settings.json | 엔진 새 규칙 2개 등록. **A와 동시에 반영해야 함**: GM의 설정 불일치 검사가 엔진 시작을 거부함 |

### D. unified-packages 실행기 (`patches/unified-packages-root.patch`)

| ID | 파일 | 변경 |
|---|---|---|
| **I1** | run_batch.py | **배치 전체가 풀 DB 하나(`<batch>/pool.sqlite3`)를 공유**(기본값). 기존 방식은 `--isolated` |
| I2 | run_batch.py, run_game.py | B의 에이전트별 기억 위치 `GAME_MEMORY_DIR = <batch>/agent-memory` |
| I3 | offline_llm.py(신규), run_game.py | API 없이 검증하는 백엔드: `--fake-llm`(규칙 기반, 무료·즉시), `--codex`(로컬 Codex CLI, 임시). 요약에 `llm_backend` 표기 |
| I4 | verify_persistence.py(신규) | 배치가 아래 4장의 P1~P3을 만족하는지 자동 판정 |
| — | tests/test_agent_experience.py | B 수정 단위 테스트 13개 |

## 4. 검증

### 판정 기준

- **P1 같은 에이전트:** 모든 판의 플레이어가 하나의 영속 풀(16명, MBTI 고정)에서 나옴
- **P2 서로 다른 워커:** 한 판 안의 12명이 서로 다른 에이전트·캐릭터·MBTI이고, 에이전트의 MBTI가 풀의 고정값과 일치
- **P3 경험 인지:** 각 에이전트가 k번째 판에서 받는 첫 프롬프트가 그 전까지 자기가 플레이한 판 수를 정확히 보고하고 교훈을 포함. 신규 에이전트는 "첫 게임"

### 결과 (`evidence/`)

| 실행 | 결과 |
|---|---|
| 기존 테스트 | 엔진 160 → 수정 후 166(신규 6) 통과. 루트 19(신규 13) 통과. GM 88개 중 기존 `gm_coordinator` import 오류 1건 외 통과 |
| `--fake-llm` 3판, 공유 풀 | P1·P2·P3 전부 통과. 2판째: 재등장 8 + 신규 4, 3판째: 재등장 12. 판마다 종료 훅 12개 정상, 엔진 `strategy_state` 갱신 |
| `--fake-llm --isolated` 2판(대조군, 기존 방식) | 검증 실패. 시드 간 같은 에이전트 **0/16** = experiment-30에서 일어난 일 |
| `--codex` 2판(실제 모델) | 7장 참고 |

재현 방법:

```bash
python run_batch.py --fake-llm --count 3 --deadline 3 --batch-dir runs/check
python verify_persistence.py runs/check        # 종료 코드 0 = 통과
```

## 5. 적용 순서

1. **A와 C의 settings를 함께** 반영합니다. 엔진에 규칙이 추가되면 GM의 설정 검사가 일치를 요구합니다.
2. C1·C3 (B의 기억 기능 전제)
3. B
4. unified-packages 실행기(I1~I4)

각 저장소 루트에서:

```bash
git apply --check <patch>   # 먼저 확인
git apply <patch>
```

줄바꿈(CRLF/LF) 차이로 거부되면 `git apply --ignore-whitespace`를 사용하세요. `modified/`에는 수정된 파일 전체가 같은 경로로 들어 있습니다.

## 6. 팀 합의가 필요한 설계 결정

1. **`unsolved_score` (가설 1: 실패 패널티 > 성공 보상).** 현재 점수는 정답 100/70/50/30, 오답 사망 −20, 미해결 생존 0입니다. 고발의 기대 점수가 양수가 되려면 성공 확률이 1/6 초과, 즉 남은 조합 6개 이하여야 합니다. `unsolved_score = −20`으로 두면 "끝까지 미해결 = 오답 사망"이 되어, 마감 직전 고발이 약하게 우월해집니다. 일찍 죽으면 남은 기회와 미션을 잃으므로 정보를 모으는 동안은 여전히 기다리는 게 낫습니다. 기본값은 기존과 같은 0으로 두었습니다.
2. **역방향 추론.** "남은 용의자의 자정 위치를 모두 알면 범행 장소는 그 방들 중 하나"는 논리적으로 타당합니다. 하지만 엔진 `infer()`와 B `schema.py`는 규칙을 1:1로 맞추게 설계돼 있어서(H2 환각 측정 기준) 양쪽을 함께 바꿔야 합니다. 이번 패치에는 넣지 않았습니다.
3. **교훈 저장 위치.** 엔진은 일반화된 전술 enum과 숫자 가중치만 받습니다(사건 비밀 유출 방지). 자연어 교훈은 B가 배치 폴더의 `agent-memory/<agent_id>.json`에 저장합니다. 엔진에 자유 텍스트 필드를 둘지 결정이 필요합니다.
4. **LLM 병목.** 워커를 직렬로 돌리므로 호출당 7초면 사이클당 약 4.6분이 걸립니다. 이번 패치는 병목을 그대로 두고, 대신 에이전트가 실제 남은 시간을 알게 했습니다. 병렬화(워커별 스레드)는 엔진이 게임별 락을 가진 ThreadingHTTPServer라 가능해 보이지만, 제공자 rate limit을 확인한 뒤 결정할 일입니다.
5. **느린 모델과 질문 응답 제한(30초).** 직렬 실행에서 호출이 느리면 질문이 응답 전에 만료됩니다. 느린 백엔드를 쓸 때는 `question_timeout` 조정을 검토해야 합니다.

## 7. Codex 실모델 2판 결과

(실행 완료 후 기입)

## 8. 알려진 한계·기존 이슈

- **회고 시점.** 엔진 게이트웨이는 종료된 게임의 LLM 호출을 거부합니다. 그래서 LLM 회고는 다음 판 시작 시에 하고, 판 종료 시에는 규칙 기반 교훈만 즉시 저장합니다. 다음 판에 뽑히지 않은 에이전트는 다시 뽑힐 때 회고합니다.
- **`--codex` 실행은 임시 확인용입니다.** 엔진 게이트웨이를 거치지 않아 `llm_records`에 남지 않습니다(`runs/<game>/codex-calls.jsonl`에 따로 기록). 모델도 Codex 기본 모델이므로 API 모델(gpt-6-luna) 결과로 보고하면 안 됩니다.
- **기존 테스트 이슈(이번 수정과 무관).**
  - `test_full_game_with_scripted_runtime_reaches_official_ending`은 확률적으로 실패합니다. 원본에서도 4회 중 2회 실패했습니다(무작위 `game_id`가 스크립트 플레이어 시드가 됨).
  - `test_required_statements_meet_every_tier`는 옛 패키지명 `gm_coordinator`를 import합니다.
  - Windows에서는 루트 테스트 2개가 cp949 인코딩 때문에 실패하므로 `PYTHONUTF8=1`이 필요합니다.
- **DB 크기.** 공유 풀 DB에는 모든 판의 LLM 기록이 쌓입니다(experiment-30 기준 판당 약 23MB, 30판이면 약 700MB). 판별 `full-record.json` 내보내기는 기존과 같습니다.
