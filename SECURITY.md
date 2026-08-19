# Security Policy

## Supported versions

Security fixes are delivered through the latest release of deep-model-router.
Check the current version with `jq -r .version .claude-plugin/plugin.json`.

## Reporting a vulnerability

Please report security issues **privately** via
[GitHub Security Advisories](https://github.com/Sungmin-Cho/deep-model-router/security/advisories/new)
rather than opening a public issue.

We aim to acknowledge reports within a few days and will coordinate a fix and a
disclosure timeline with you.

## Scope

deep-model-router ships a skill, a YAML policy, and two local Python CLIs.

- `route_task.py` is a deterministic scorer. It reads the bundled policy and
  the caller's classification; it does not call a model provider.
- `dispatch_agent.py` starts a local child process (a host CLI) under a
  wall-clock deadline and a TERM-then-KILL process-group ladder. Prompts enter
  the child by file on stdin; argv is executed without a shell. Receipt paths
  derived from `--attempt-id` must stay inside `--receipt-dir`.
- The locator refuses personal skill symlinks and sibling source checkouts so
  a nearby tree cannot shadow the installed plugin.
- The plugin does not ship `hooks/` and does not keep a network service.

When reporting, please indicate which runtime (Claude Code / Codex / Grok) is
affected, and whether the issue is in routing, location, or dispatch.
