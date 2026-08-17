# deep-model-router — Agent Guide

Deterministic model / effort / review router. Claude Code, Codex, and Grok
share this file. The skill name is `model-router`; the plugin key is
`deep-model-router`.

Read the version from `.claude-plugin/plugin.json`. Do not hard-code it.

📄 Documentation in this repo follows `docs/DOCS_RULE.md` (local maintainer guide).

## Layout

```
skills/model-router/
  SKILL.md
  config/model-routing.yaml
  scripts/route_task.py
  scripts/dispatch_agent.py
  references/
  tests/
```

Every path in the skill is `$SKILL_DIR`-relative. `$SKILL_DIR` is the
directory that contains `SKILL.md`.

## Invocation

- Claude: `/deep-model-router:model-router`
- Codex: `$deep-model-router:model-router`
- Repeated decisions: `python3 "$SKILL_DIR/scripts/route_task.py" --format json`
  or `--request-json <file>` (RouteRequestV1).

## Locator

Consumers must not import `../deep-model-router` or a personal
`~/.claude/skills/model-router` symlink. Resolve the CLI with
`DEEP_MODEL_ROUTER_CLI`, then `DEEP_MODEL_ROUTER_ROOT`, then the host
plugin cache. See `docs/locator.md`.

## Tests

```bash
python3 -m pytest skills/model-router/tests/ -q
claude plugin validate .
```
