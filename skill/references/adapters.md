# Runtime adapters

Read this when a model is unavailable, when you are invoking across the
provider bridge, or when you need the concrete command syntax.

## Contents

- [Two layers](#two-layers)
- [Capability preflight](#capability-preflight)
- [Effort mapping](#effort-mapping)
- [Transports](#transports)
- [Fallback matrices](#fallback-matrices)
- [Disclosing degradation](#disclosing-degradation)

## Two layers

**Layer A — routing policy.** Provider-neutral. Contains no command, no model
identifier, no effort string. Takes a classified task, returns a role, an
effort level, a review policy, an escalation policy, a rationale, and a
confidence.

**Layer B — this file.** Maps role aliases to concrete models and conceptual
effort to provider values. Owns invocation syntax, session management, and
isolation mechanics.

Keeping the layers apart is what lets the same policy drive both runtimes and
survive model renames. Any provider-specific string that leaks into Layer A is
a bug in the policy, not a shortcut.

## Capability preflight

Before the first route in a session, establish what you can actually invoke:

```yaml
capabilities:
  runtime: claude_code | codex | unknown
  available_families: [claude, openai]
  available_models: []          # resolved ids only
  effort_control: native | approximate | none
  effort_values: []             # values the CLI actually accepts
  cross_provider: available | unavailable | untested
  subagent_isolation: available | unavailable
```

Probe in this order, stopping at the first success per field:

1. **Runtime-native listing** — whatever the host exposes for enumerating models.
2. **Single cheap call** — one minimal-token request per candidate id; a
   resolution error marks the alias unavailable.
3. **Static allowlist** — the `verified: true` registry entries as a floor.

Rules:

- An alias whose model does not resolve is **unavailable** and falls back. It
  must never cause a hard failure.
- Cache the probe result for the session. Re-probe if any call fails with a
  model-resolution error.
- If `cross_provider` is `unavailable` or `untested`, use the single-provider
  binding. Never emit a route you cannot execute.
- If `subagent_isolation` is `unavailable`, degrade dual review per
  `review-policy.md` and say so in the rationale.

This step exists because the most consequential failure a router can have is
naming a model nobody verified exists. The route looks fine; it just cannot run.

## Effort mapping

Conceptual levels are ordered:

```
MINIMAL < LOW < MEDIUM < HIGH < VERY_HIGH < MAX
```

### Claude Code

Accepted values: `low | medium | high | xhigh | max`.

```yaml
MINIMAL:   low        # no distinct minimal tier; collapses upward
LOW:       low
MEDIUM:    medium
HIGH:      high
VERY_HIGH: xhigh
MAX:       max
```

Note the `MINIMAL → low` collapse: do not assume a distinct minimal tier exists
on this runtime.

### Codex

Accepted values: `none | low | medium | high | xhigh | max`. Verified by probing
each value against the installed CLI — **`minimal` is rejected**, `none` is
accepted.

```yaml
MINIMAL:   none
LOW:       low
MEDIUM:    medium
HIGH:      high
VERY_HIGH: xhigh
MAX:       max
```

Set it with `-c model_reasoning_effort=<value>`.

### When effort control is unavailable

If `effort_control: none`, approximate in this order of preference:

1. Route one model tier higher.
2. Give explicit reasoning instructions in the prompt.
3. Decompose the task into smaller verified steps.
4. Increase verification depth — more checks, not more retries.
5. Constrain the token budget.

Record `effort_control: approximate` in the metrics so later analysis can
account for it.

## Transports

Both runtimes have a native subagent mechanism and a CLI bridge to the other
family. Both bridges are verified working.

### Claude Code

**Native:** the `Agent` tool. Context isolation is the enforcement boundary for
independent review.

**To OpenAI models:**

```bash
codex exec -m <model-id> \
    -c model_reasoning_effort=<effort> \
    -s read-only|workspace-write \
    --skip-git-repo-check \
    "<prompt>"
```

Runs as a separate process with a fresh session — isolation holds by
construction.

### Codex

**Native:** the `multi_agent` feature (stable, enabled). Its context-isolation
semantics have **not** been verified to match the Claude `Agent` tool's. Until
they are, either confirm isolation on the first dual review of a session, or
run both reviewers as separate `codex exec` processes.

**To Claude models:**

```bash
claude -p --model <model-id> \
    --permission-mode <mode> \
    "<prompt>"
```

Also a separate process with a fresh session.

### Confirming a transport

Before setting `cross_provider: available`, confirm three things:

1. The transport exists and can execute a trivial round-trip.
2. You know which models it can address.
3. Reviewer isolation survives the bridge — a delegation that shares
   conversation state breaks independence.

**Never emit a route naming a model you cannot invoke.** A route that depends on
an unconfirmed transport must be rewritten through the fallback matrix before
emission.

### Permission asymmetry

Worth knowing: a bridged worker runs under the *other* runtime's sandbox and
approval settings, not the current session's. A `codex exec` spawned from
Claude Code obeys Codex's `sandbox_mode` and `approval_policy`. That is a real
operational difference — surface it rather than assuming the caller's
permission posture carries across.

## Fallback matrices

Fallbacks are mandatory, not advisory. A missing model degrades the route; it
never fails it.

### Claude Code runtime

| Unavailable | Fallback |
|---|---|
| `worker_fast` (OpenAI) | `claude_worker_fast`; if absent, `worker_balanced` |
| `worker_balanced` (Claude) | `worker_balanced_alt` (OpenAI middle tier) |
| `reasoning_specialist` (OpenAI) | `senior_engineer` at one effort level higher |
| `principal_architect` | `senior_engineer` at `MAX` + an additional independent second review |
| Cross-family reviewer | Strongest available same-family reviewer; set `cross_family_review: false` |

### Codex runtime

| Unavailable | Fallback |
|---|---|
| `worker_balanced` (Claude) | `openai_worker_balanced`, or `reasoning_specialist` if band ≥ `HIGH` |
| `senior_engineer` (Claude) | `reasoning_specialist` |
| `principal_architect` (Claude) | `reasoning_specialist` at `MAX` + a second independent review if available |
| Cross-family reviewer | Strongest available same-family reviewer; set `cross_family_review: false` |

### Degraded bindings

When a bridge is down entirely, fall back to the single-provider binding:

```yaml
claude_only:
  worker_fast:          claude-haiku-4-5-20251001
  worker_balanced:      claude-sonnet-5
  senior_engineer:      claude-opus-5
  reasoning_specialist: claude-opus-5      # at MAX effort
  principal_architect:  claude-fable-5

openai_only:
  worker_fast:          gpt-5.6-luna
  worker_balanced:      gpt-5.6-terra
  senior_engineer:      gpt-5.6-sol
  reasoning_specialist: gpt-5.6-sol
  principal_architect:  gpt-5.6-sol        # at MAX effort + second review
```

Both degraded bindings collapse the two frontier roles onto one model. That
means dual review loses its family diversity — the second reviewer is the same
model at the same tier, which catches far less. Set `cross_family_review: false`
and weigh the second verdict accordingly.

## Disclosing degradation

Every applied fallback appears in `fallbacks_applied` and is named in the
rationale. When a fallback reduces review independence, the degradation rules in
`review-policy.md` apply.

The rule underneath all of this: the metrics should let someone reconstruct not
just what was decided, but what was *available* when it was decided. A route
that looks weak in hindsight is a very different problem depending on whether
the strong option existed at the time.
