[English](./CHANGELOG.md) | **한국어**

# 변경 이력

이 프로젝트의 모든 주요 변경 사항은 이 파일에 기록됩니다.

형식은 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)를 따르며,
이 프로젝트는 [Semantic Versioning](https://semver.org/spec/v2.0.0.html)을 준수합니다.

## [1.2.0] — 2026-08-19 (증거 연결)

### 추가됨

- 모든 route가 `request_sha256`과 결정적 `decision_fingerprint`를 방출한다. dispatch receipt가 이 값을 실을 수 있고(`--decision-fingerprint`, `--policy-sha256`, `--transport-id`, `--host-cli-version`), `verify-evidence`가 `--expect-fingerprint` / `--expect-models`로 대조한다.
- Receipt가 요청된 모델과 실제 서빙된 모델의 구분을 명시한다: `observed_model_id` / `observed_model_source` 자리(정직한 기본값 — 아직 어떤 transport도 서빙된 모델을 관측할 수 없다).
- `routing_confidence_kind`가 confidence 값이 보정된 확률이 아니라 heuristic gate 점수임을 라벨로 밝힌다.
- 레지스트리가 OpenAI GPT-5.6의 272K 장문맥 구간을 명시적 경계 어휘와 함께 기록하고, 모든 좌석의 표준 cached-input 요율과, 가격 경계 및 Fable 5의 공급자 측 모델 대체에 대한 원장 항목을 남긴다.
- 서빙 모델 caveat가 선언된 모델이 좌석에 앉은 route는 대체 가능성을 notes에 공시한다.

### 수정됨

- `policy_sha256`이 실제로 사용된 정책을 해시한다. 라이브러리 API로 주입한 config가 더 이상 디스크의 해시로 보고되지 않고, route 사이에 그 자리에서 변경된 config도 캐시가 아니라 새 내용으로 다시 읽힌다 — 하나의 fingerprint는 하나의 결정을 가리킨다.
- 모델 프로파일이 더 이상 워커 바인딩의 품질을 미측정으로 서술하지 않는다. effort 표에서 정책이 삭제한 작업 유형이 사라졌고, xAI 장문맥 경계는 "200K 이상"으로 읽힌다.

## [1.1.1] — 2026-08-18 (바인딩 품질 측정)

### 변경됨

- worker_fast·worker_balanced 바인딩의 품질을 가정이 아닌 측정으로 확인했다: 변별력 있는 446-노드 숨긴 테스트 헤드투헤드에서 haiku가 luna를 근소하게 앞섰고, grok-4.6 대 sonnet-5는 천장에서 동률이었다. 두 바인딩과 모든 `capability_tier`는 변경 없음 — 여전히 검증된 가격 우위에 근거하며, 검증 원장(verification ledger)에 점수·실패 유형·지연 시간이 기록됐다.

## [1.1.0] — 2026-08-18 (설계·가격 감사)

### 추가됨

- `large_context`가 바인딩을 결정한다: 이 플래그가 붙은 작업은 `worker_balanced`를 Claude balanced 좌석으로 보낸다. xAI 좌석은 입력 200K 토큰을 넘기면 요청 전체를 두 배 요율로 청구하고, 컨텍스트 창도 500K 더 일찍 끝나기 때문이다.
- 레지스트리가 xAI의 long-context 가격 구간과 컨텍스트 창을 기록하고, 모델 프로파일도 이 비교가 조건부라는 사실을 그대로 적는다.

### 수정됨

- `local_policy`의 키 이름뿐 아니라 값도 검증한다. 알 수 없는 effort 값은 적용된 하한으로 보고되면서 실제로는 조용히 무시됐고, 숫자가 아닌 tier는 내부 오류용 상태로 크래시했다. 둘 다 이제 잘못된 입력(exit 2)이다.
- CLI locator가 문서화된 순서(env → root → Claude 캐시 → Codex 캐시)대로 해석한다. 알파벳 순서 때문에 `.codex`가 이기던 문제, 문자열 정렬로 `1.9.0`이 `1.10.0`을 이기던 문제, 소스 체크아웃 거부가 첫 단계에서만 걸리던 문제를 함께 고쳤다.
- 알 수 없는 attempt id에 대한 `dispatch_agent.py status`가 traceback 대신 `cancel`과 같은 한 줄 메시지와 exit 2로 답한다.
- 정책 파일과 어긋나 있던 문서: REVIEW/CRITICAL 워커, 빠져 있던 `concurrency_sensitive` 오버라이드, 빠져 있던 `termination_unconfirmed` 운영 플래그.
- README가 PyYAML 요구 사항과, human-gate exit status가 3..255 범위에서 설정 가능하다는 사실을 명시한다.

### 제거됨

- `review.MEDIUM.reviewer_count`와 `review.MEDIUM.prefer_cross_family`: 둘 다 읽히기만 하고 아무 효과가 없었다. 이들이 서술하던 동작은 그대로이며, 처음부터 상수였다는 사실을 문서에 적었다.
- 아무것도 선택하지 않던 `effort_by_work` 항목 3개와, 쓰이지 않던 `implementation_role` 작업 필드.

## [1.0.1] — 2026-08-17

### 수정됨

- Claude Sonnet 5 정식 가격이 $2 / $10으로 확정된 뒤, 레지스트리에 남아 있던 $3 / $15 수치를 고쳤다.

## [1.0.0] — 2026-08-17

### 추가됨

- Claude Code, Codex, Grok를 위한 공유 결정 평면의 첫 공개 릴리스.
- 스킬 기반 분류와 RouteDecisionV1(밴드, 워커, effort, 리뷰 정책, 정직한 독립성)을 방출하는 결정적 채점기.
- RouteRequestV1 파일 입력, local-policy 병합, HIGH/CRITICAL 하한을 내리지 않는 가용성 인식 폴백.
- wall-clock deadline, 프로세스 그룹 kill ladder, 격리 증거와 구분되는 완료 receipt를 가진 백그라운드 dispatch supervisor.
- 형제 소스 트리와 개인 스킬 심링크를 거부하는 호스트 중립 CLI locator.
- 나머지 deep-suite와 같은 공개 플러그인 문서: 한/영 README와 CHANGELOG, CONTRIBUTING, SECURITY, LICENSE.
