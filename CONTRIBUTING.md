# Contributing to deep-model-router

Thanks for improving **deep-model-router**, the shared decision plane for the
[Deep Suite](https://github.com/Sungmin-Cho/claude-deep-suite) plugin family
across Claude Code, Codex, and Grok.

This plugin ships one skill, a YAML policy, and two Python CLIs. Keep routing
deterministic: classification stays with the caller, and `route_task.py` must
emit the same RouteDecisionV1 for the same inputs.

## Requirements

- Python 3
- `pytest` (`python3 -m pytest`)
- Git
- POSIX for `dispatch_agent.py` (process-group control)

## Getting started

```text
git clone https://github.com/Sungmin-Cho/claude-deep-model-router.git
cd claude-deep-model-router
```

There are no package dependencies to install for the router itself.

## Local checks

Run both commands from the repository root:

```text
python3 -m pytest skills/model-router/tests/ -q
claude plugin validate .
```

`npm test` and `npm run verify` wrap the same pytest suite. Everything must be
green before a pull request.

## Conventions

- **Documentation** follows [`docs/DOCS_RULE.md`](docs/DOCS_RULE.md), the local
  maintainer source of truth for README, CHANGELOG, and agent-guide
  synchronization.
- **Version triple-sync**: `.claude-plugin/plugin.json`,
  `.codex-plugin/plugin.json`, and `package.json` must carry the same version.
- **CHANGELOG**: maintain matching English and Korean Keep a Changelog
  structures. Do not add test counts, review narration, commit hashes, or
  internal function names to release notes.
- **Never hard-code the version** in `AGENTS.md` or `CLAUDE.md`. Read it from
  `.claude-plugin/plugin.json`.
- **Locator**: consumers must not import `../deep-model-router` or a personal
  skill symlink. Keep [`docs/locator.md`](docs/locator.md) and
  `skills/model-router/scripts/locate_router.py` in agreement.
- **One home per fact**: model identifiers live only in
  `skills/model-router/config/model-routing.yaml`. Documented invocations of
  `route_task.py` must be `$SKILL_DIR`-prefixed.

## Pull requests

1. Branch from `main`.
2. Keep changes focused and update both bilingual documentation surfaces when
   behavior changes.
3. Run the local checks above.
4. Explain what changed, why, and which checks prove it.

## Reporting issues

Open a GitHub issue. For security reports, see [`SECURITY.md`](SECURITY.md).
