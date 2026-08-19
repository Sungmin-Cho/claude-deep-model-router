**English** | [한국어](./CHANGELOG.ko.md)

# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] — 2026-08-19 (evidence linkage)

### Added

- Every route now emits `request_sha256` and a deterministic `decision_fingerprint`; dispatch receipts can carry them (`--decision-fingerprint`, `--policy-sha256`, `--transport-id`, `--host-cli-version`) and `verify-evidence` checks them with `--expect-fingerprint` / `--expect-models`.
- Receipts declare requested-vs-served identity explicitly: `observed_model_id` / `observed_model_source` slots (honest defaults — no transport can observe the served model yet).
- `routing_confidence_kind` labels the confidence value as a heuristic gate score, not a calibrated probability.
- The registry records the OpenAI GPT-5.6 272K long-context tier with an explicit boundary vocabulary, standard cached-input rates for all seats, and ledger entries for the pricing boundary and Fable 5's provider-side substitution.
- Routes seating a model with a declared served-model caveat disclose the substitution possibility in their notes.

### Fixed

- `policy_sha256` now digests the policy actually in use, so a config injected through the library API no longer reports the on-disk digest, and a config mutated in place between routes is re-read rather than answered from a stale cache — one fingerprint identifies one decision.
- Model profiles no longer describe worker bindings' quality as unmeasured; the effort table no longer lists work types the policy removed; the xAI long-context boundary reads "at or above 200K".

## [1.1.1] — 2026-08-18 (binding quality probed)

### Changed

- The worker_fast and worker_balanced bindings' quality is now measured, not merely assumed: a discriminating 446-node hidden-test head-to-head found haiku marginally ahead of luna and a tie at ceiling for grok-4.6 vs sonnet-5. Both bindings and every `capability_tier` are unchanged — they continue to rest on the verified price advantage — and the verification ledger records the scores, failure modes, and latency.

## [1.1.0] — 2026-08-18 (design and pricing audit)

### Added

- `large_context` now decides a binding: a task carrying it routes `worker_balanced` to the Claude balanced seat, because the xAI seat re-bills the whole request at double rate past 200K input tokens and its window ends 500K earlier.
- The registry records the xAI long-context price tier and context window, and the model profile states the comparison as the conditional one it is.

### Fixed

- `local_policy` values are validated, not only their key names: an unknown effort level was reported as an applied floor while being silently ignored, and a non-numeric tier crashed with the status reserved for internal errors. Both are invalid input (exit 2) now.
- The CLI locator resolves in its documented order — env, root, Claude cache, Codex cache — instead of letting `.codex` win by alphabetical accident, picks the highest version by number rather than by string (`1.9.0` used to beat `1.10.0`), and refuses a source checkout on every tier rather than only the first.
- `dispatch_agent.py status` on an unknown attempt id answers with one sentence and exit 2, like `cancel`, instead of a traceback.
- Documentation drift against the policy file: the REVIEW/CRITICAL worker, the missing `concurrency_sensitive` override, and the missing `termination_unconfirmed` operational flag.
- README now states the PyYAML requirement and that the human-gate exit status is configurable over 3..255.

### Removed

- `review.MEDIUM.reviewer_count` and `review.MEDIUM.prefer_cross_family`: both were read and neither had any effect. The behaviour they described is unchanged and now documented as the constant it always was.
- Three `effort_by_work` entries nothing selected, and an unused `implementation_role` task field.

## [1.0.1] — 2026-08-17

### Fixed

- Corrected Claude Sonnet 5 list price to $2 / $10 after that introductory rate became the permanent published price.

## [1.0.0] — 2026-08-17

### Added

- First public release of the shared decision plane for Claude Code, Codex, and Grok.
- Skill-driven classification plus a deterministic scorer that emits a RouteDecisionV1 (band, worker, effort, review policy, honest independence).
- RouteRequestV1 file input, local-policy merge, and availability-aware fallbacks that never drop a HIGH or CRITICAL floor.
- Background dispatch supervisor with a wall-clock deadline, process-group kill ladder, and a completion receipt distinct from isolation evidence.
- Host-neutral CLI locator that refuses sibling source trees and personal skill symlinks.
- Public plugin surface matching the rest of deep-suite: bilingual README and CHANGELOG, CONTRIBUTING, SECURITY, and LICENSE.
