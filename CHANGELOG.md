**English** | [한국어](./CHANGELOG.ko.md)

# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — 2026-08-17

### Added

- First public release of the shared decision plane for Claude Code, Codex, and Grok.
- Skill-driven classification plus a deterministic scorer that emits a RouteDecisionV1 (band, worker, effort, review policy, honest independence).
- RouteRequestV1 file input, local-policy merge, and availability-aware fallbacks that never drop a HIGH or CRITICAL floor.
- Background dispatch supervisor with a wall-clock deadline, process-group kill ladder, and a completion receipt distinct from isolation evidence.
- Host-neutral CLI locator that refuses sibling source trees and personal skill symlinks.
- Public plugin surface matching the rest of deep-suite: bilingual README and CHANGELOG, CONTRIBUTING, SECURITY, and LICENSE.
