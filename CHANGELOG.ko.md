[English](./CHANGELOG.md) | **한국어**

# 변경 이력

이 프로젝트의 모든 주요 변경 사항은 이 파일에 기록됩니다.

형식은 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)를 따르며,
이 프로젝트는 [Semantic Versioning](https://semver.org/spec/v2.0.0.html)을 준수합니다.

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
