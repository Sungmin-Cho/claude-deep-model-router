# Runtime adapters

Read this when a model is unavailable, when you are invoking across the
provider bridge, or when you need the concrete command syntax.

## Contents

- [Two layers](#two-layers)
- [Capability preflight](#capability-preflight)
- [Effort mapping](#effort-mapping)
- [Transports](#transports)
- [Dispatch contract](#dispatch-contract)
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

Keeping the layers apart is what lets the same policy drive every runtime and
survive model renames. Any provider-specific string that leaks into Layer A is
a bug in the policy, not a shortcut.

## Capability preflight

Before the first route in a session, establish what you can actually invoke:

```yaml
capabilities:
  runtime: claude_code | codex | grok | unknown
  available_families: [claude, openai, xai]
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

The map is keyed by the **model's family**, not by the host runtime. The value
is the token that family's CLI will accept. A session hosted on one runtime
routinely dispatches a model of another family; looking the spelling up under
the host used to emit a token the target CLI would reject.

### claude family

Accepted values: `low | medium | high | xhigh | max`.

```yaml
MINIMAL:   low        # no distinct minimal tier; collapses upward
LOW:       low
MEDIUM:    medium
HIGH:      high
VERY_HIGH: xhigh
MAX:       max
```

### openai family

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

### xai family

Accepted values: `low | medium | high | xhigh`. There is no minimal or none
tier — reasoning cannot be disabled — and the CLI rejects `max`. The family
tops out at `xhigh`.

```yaml
MINIMAL:   low
LOW:       low
MEDIUM:    medium
HIGH:      high
VERY_HIGH: xhigh
MAX:       xhigh      # unreachable: the clamp runs before this lookup and
                      # the only xai model's ceiling is VERY_HIGH
```

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

Each runtime has a native subagent and CLI bridges to the other families.
Verification is per direction, not a blanket "the bridges work":

- Claude Code → openai (`codex exec`) — verified
- Codex → claude (`claude -p`) — verified
- Claude Code → xai (`grok -p`) — verified
- Codex → xai, grok → claude, grok → openai — assumed (same mechanisms, the
  hosted direction was not probed)

**Passing the prompt.** A reviewer's prompt contains the diff, and diffs
contain quotes, backticks, `$`, and newlines. Build argv programmatically —
never interpolate a prompt into a shell string. Prefer file delivery: write
the prompt to a file and feed it to the child's stdin (`codex exec` reads
the instruction from stdin when the prompt argument is `-`;
`scripts/dispatch_agent.py --prompt-file` does this for any transport). A
CLI that only takes a positional prompt gets it as a single argv element.

**Pin a non-interactive permission mode before a background launch.** A
background bridge has no TTY, so an approval prompt is a hang that looks
exactly like a slow model. Decide the mode up front, pass it explicitly
(`--permission-mode` / `-s <sandbox>` / the grok approval flags), and record
it in the dispatch receipt. The far side of a bridge keeps its own
sandbox/approval config — verify the effective mode at preflight.

**Fences mirror the YAML.** Every command fence in this section is Layer B's
prose rendering of the matching `transports` mechanism in
`config/model-routing.yaml` — the same argv shape, token-for-token (modulo
line wrapping for readability); a `codex exec` fence carries `-s <sandbox>`
and `--skip-git-repo-check` wherever the YAML mechanism does, and a mismatch
between the two is a bug, not an intentional variant.

### Claude Code

**Native:** the `Agent` tool. Context isolation is the enforcement boundary for
independent review.

**To openai models:**

```bash
codex exec -m <id> \
    -c model_reasoning_effort=<effort> \
    -s <sandbox> \
    --skip-git-repo-check \
    "<prompt>"
```

`<sandbox>` is `read-only` or `workspace-write`.

Runs as a separate process with a fresh session — isolation holds by
construction.

**To xai models:**

```bash
grok --no-auto-update -p "<prompt>" -m <id> \
    --effort <native-effort> \
    --output-format plain -s <fresh-uuid>
```

`<native-effort>` is one of `low`, `medium`, `high`, `xhigh` — the family's
native token from the effort map above, never the conceptual level.

Also a separate process with a fresh session.

### Codex

**Native:** the `multi_agent` feature (stable, enabled). Its context-isolation
semantics have **not** been verified to match the Claude `Agent` tool's. Until
they are, either confirm isolation on the first dual review of a session, or
run both reviewers as separate `codex exec` processes.

**To claude models:**

```bash
claude -p --model <id> \
    --effort <effort> \
    --permission-mode <mode> \
    "<prompt>"
```

Also a separate process with a fresh session. `--effort` is required: without
it the band's level does not cross the bridge.

**To xai models:** same `grok -p` command as above. The mechanism is verified
from Claude Code; the Codex-hosted direction is assumed.

### grok

**Native:** the CLI's `--agents` / `--no-subagents` surface. Isolation
semantics have **not** been verified. Until they are, treat a grok-native dual
review as degraded unless both reviewers run as separate processes.

**To claude models:** the `claude -p --effort` command above. Assumed from this
host; the flag itself is verified.

**To openai models:**

```bash
codex exec -m <id> \
    -c model_reasoning_effort=<effort> \
    -s <sandbox> \
    --skip-git-repo-check \
    "<prompt>"
```

Assumed from this host; the same mechanism is verified from Claude Code.

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

## Dispatch contract

Layer B owns the time axis after a route is emitted. Every background
dispatch that uses a subprocess/CLI bridge transport (`codex exec`,
`claude -p`, `grok -p`) — worker or reviewer — runs under
`scripts/dispatch_agent.py`, which enforces the rules below. A foreground
dispatch a human is actively watching may skip the supervisor; it may never
skip the rules.

Native in-process transports (the Claude Code Agent tool, Codex
`multi_agent`) are outside `dispatch_agent.py`'s mechanical scope — the
supervisor's only execution primitive is `subprocess.Popen(argv)`, and an
in-process host mechanism cannot be spawned as an argv subprocess. A native
seat's supervision (deadline, cancellation) belongs to the host runtime that
launched it, not to `dispatch_agent.py`. The same receipt-state vocabulary
still classifies a native seat's outcome: a native seat that returns nothing
is `NO_RESPONSE`, exactly as for a bridged seat, but no receipt is
fabricated for a native dispatch that `dispatch_agent.py` never ran.

### Launch is not completion

A background spawn returns a handle. The result exists only when the
attempt's receipt reaches a terminal state:

```
STARTING --start failure--> START_FAILED
   |
   v
RUNNING --exit 0, output valid----> SUCCEEDED
   |    \--exit != 0--------------> FAILED
   |     \--exit 0, output bad----> INVALID_OUTPUT
   |
   +--deadline--> TERM -> grace -> KILL -> confirmed? --yes--> TIMED_OUT
   |                                                   \-no--> TERMINATION_UNCONFIRMED
   +--cancel----> (same ladder) -> CANCELLED | TERMINATION_UNCONFIRMED
```

One empty poll is not a failure: while the receipt says `RUNNING` and the
deadline has not expired, keep waiting. From outside, a model reasoning
silently is indistinguishable from a hang — which is why the deadline is
wall-clock and generous, never inactivity-based and clever.

### Per-seat deadlines

| Seat | Effort | Deadline |
|---|---|---|
| worker | up to HIGH | 10 min |
| worker | VERY_HIGH / MAX | 20 min |
| reviewer | HIGH | 10 min |
| reviewer | MAX | 20 min |
| judge | any | 10 min |

Defaults, not law — scale by task size, tool use, and write permission.
What is law: every dispatch names a deadline (`--deadline-seconds`), and
expiry means TERM, a grace period, KILL, then confirmation of the whole
process group. `TERMINATION_UNCONFIRMED` blocks every write-capable retry:
re-route with `route_task.py --flags termination_unconfirmed` and the
route holds for a human.

### Invoking the supervisor

```bash
python3 "$SKILL_DIR"/scripts/dispatch_agent.py run \
    --attempt-id r1-a7f3 --receipt-dir receipts/ \
    --deadline-seconds 600 --grace-seconds 15 \
    --seat reviewer-1 --runtime claude_code \
    --model-id <resolved-id> --effort-native <native-effort> \
    --permission-mode read-only \
    --prompt-file r1-prompt.txt --output-schema review \
    -- codex exec -m <resolved-id> -c model_reasoning_effort=<native-effort> \
       -s read-only --skip-git-repo-check -
```

`--prompt-file` feeds the child's stdin (here `codex exec`'s `-` prompt);
with no prompt file, stdin is /dev/null, so a stdin wait is structurally
impossible. `status --attempt-id <id> --receipt-dir <dir>` polls;
`cancel` kills from outside with the same confirmation ladder;
`verify-evidence` checks receipt ids before they become
`--isolation-evidence` (see `review-policy.md`, "Where the evidence id
comes from").

### Output is a contract

`--output-schema review` requires a parseable `verdict:` line. Exit 0 with
empty stdout *inside the deadline* is `INVALID_OUTPUT`, not success. Output
written after the deadline is never graded regardless of content: a
truncated `PASS` after a timeout kill is `TIMED_OUT`, never `INVALID_OUTPUT`
— that label is reserved for an in-deadline exit-0 attempt that failed to
produce a parseable verdict. Either way the review "did not run" —
re-dispatch it per `review-policy.md` ("A seat that returns no verdict");
never grade its fragments.

## Fallback matrices

Fallbacks are mandatory, not advisory. A missing model degrades the route; it
never fails it.

### Claude Code runtime

| Unavailable | Fallback |
|---|---|
| `worker_fast` (openai) | `claude_worker_fast`, then `claude_worker_balanced` |
| `worker_balanced` (xai) | `claude_worker_balanced`, then `openai_worker_balanced`, then `claude_senior` |
| `senior_engineer` (claude) | `openai_reasoning`, then `claude_architect` |
| `reasoning_specialist` (openai) | `claude_senior`, then `claude_architect` |
| `principal_architect` (claude) | `claude_senior`, then `openai_reasoning` |
| Cross-family reviewer | Strongest available same-family reviewer; set `cross_family_review: false` |

### Codex runtime

| Unavailable | Fallback |
|---|---|
| `worker_fast` (openai) | `openai_worker_balanced`, then `claude_worker_fast` |
| `worker_balanced` (xai) | `claude_worker_balanced`, then `openai_worker_balanced`, then `openai_reasoning` |
| `senior_engineer` (claude) | `openai_reasoning`, then `claude_senior` |
| `reasoning_specialist` (openai) | `openai_reasoning`, then `claude_senior` |
| `principal_architect` (claude) | `openai_reasoning`, then `claude_architect` |
| Cross-family reviewer | Strongest available same-family reviewer; set `cross_family_review: false` |

### grok runtime

| Unavailable | Fallback |
|---|---|
| `worker_fast` (openai) | `claude_worker_fast`, then `openai_worker_balanced` |
| `worker_balanced` (xai) | `claude_worker_balanced`, then `openai_worker_balanced` |
| `senior_engineer` (claude) | `claude_senior`, then `openai_reasoning` |
| `reasoning_specialist` (openai) | `openai_reasoning`, then `claude_senior` |
| `principal_architect` (claude) | `claude_architect`, then `claude_senior` |
| Cross-family reviewer | Strongest available same-family reviewer; set `cross_family_review: false` |

### Degraded bindings

When a bridge is down entirely, fall back to the single-provider binding:

Registry keys, not model ids — resolve them through `config/model-routing.yaml`,
which is the only place a concrete identifier appears:

```yaml
claude_only:
  worker_fast:          claude_worker_fast
  worker_balanced:      claude_worker_balanced
  senior_engineer:      claude_senior
  reasoning_specialist: claude_senior          # at MAX effort
  principal_architect:  claude_architect

openai_only:
  worker_fast:          openai_worker_fast
  worker_balanced:      openai_worker_balanced
  senior_engineer:      openai_reasoning
  reasoning_specialist: openai_reasoning
  principal_architect:  openai_reasoning       # at MAX effort + second review

xai_only:
  worker_fast:          xai_frontier
  worker_balanced:      xai_frontier
  senior_engineer:      xai_frontier
  reasoning_specialist: xai_frontier          # at VERY_HIGH; the model's ceiling
  principal_architect:  xai_frontier          # at VERY_HIGH; the model's ceiling
```

`claude_only` and `openai_only` collapse the two frontier roles onto one model,
so dual review loses family diversity — set `cross_family_review: false` and
weigh the second verdict accordingly. They still have enough distinct models
to seat independent reviewers, so a band that requires independence stays
executable (at reduced depth under `openai_only`).

`xai_only` is not that pattern. One model fills every role, so any band that
requires independent review is `INDEPENDENCE_UNAVAILABLE` — terminal, not
merely thin. That is the honest answer: there is no way to run an independent
review against a single model. Only `LOW` (independence not required) stays
executable.

## Disclosing degradation

Every applied fallback appears in `fallbacks_applied` and is named in the
rationale. When a fallback reduces review independence, the degradation rules in
`review-policy.md` apply.

The rule underneath all of this: the metrics should let someone reconstruct not
just what was decided, but what was *available* when it was decided. A route
that looks weak in hindsight is a very different problem depending on whether
the strong option existed at the time.
