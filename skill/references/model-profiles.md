# Roles and bindings

Routing logic addresses models by **role alias**. Concrete identifiers live in
`config/model-routing.yaml` and are repeated here only for orientation — if the
two disagree, the config wins.

The point of the indirection: when models, prices, or names change, you update
one registry and recalibrate thresholds. The role vocabulary stays fixed. That
separation is what keeps this policy alive across model generations and
portable between runtimes.

## The five roles

These are the permanent interface. They do not change when models change.

| Role | Meaning |
|---|---|
| `worker_fast` | High-volume worker and default orchestrator |
| `worker_balanced` | Balanced senior worker; the first meaningful escalation |
| `senior_engineer` | Senior implementation, difficult debugging, code-review judgment |
| `reasoning_specialist` | Frontier logical verification and adversarial analysis |
| `principal_architect` | System architecture, ambiguity resolution, final judge |

Two more names appear in the policy — `reviewer` and `judge` — but they are
**capacities, not models**. They bind to one of the five above at routing time.

### `worker_fast` — default worker and orchestrator

Use for: task decomposition, routine routing decisions, straightforward coding,
scoped bug fixes, test generation, mechanical refactoring, repetitive edits,
documentation, implementation from a clear specification, low-risk review.

Must not be the sole authority when architecture is highly ambiguous,
requirements conflict, the root cause is unknown after reasonable
investigation, blast radius is critical, any critical-domain flag is set, or
several architectural approaches carry major tradeoffs.

### `worker_balanced` — first escalation

Use for: moderately ambiguous implementation, multi-file changes with real
interaction between the parts, difficult debugging, unfamiliar codebases, work
where code quality outweighs throughput, tool-heavy agent work, medium-risk
review, complex refactoring that does not justify the frontier tier.

This is the **first code-centric escalation target**. If `worker_fast` is
unavailable in the active runtime, this becomes the default worker.

### `senior_engineer` — senior engineering and code review

Use for: complex debugging, difficult code semantics, maintainability review,
architecture-compliance review, high-risk implementation review, complex
refactoring, and review of `worker_fast` / `worker_balanced` output.

Prefer this over `principal_architect` for routine high-end engineering. The
architect tier is for judgment about *systems*, not about code.

### `reasoning_specialist` — frontier reasoning and verification

Use for: difficult logical verification, edge-case analysis, specification
consistency, concurrency and state reasoning, hard root-cause investigation,
adversarial and critical review.

Especially valuable as the independent reviewer of `senior_engineer`-family
output, because it is drawn from a different model family — see below.

### `principal_architect` — architecture and adjudication

**Must not be the default worker.**

Reserve for: system-level architecture, major new subsystems, highly ambiguous
requirements, unfamiliar architecture with many valid solutions, long-horizon
planning, major migrations, reviewer disagreement requiring adjudication, and
severe uncertainty that survived all prior analysis.

## Why two frontier roles

`senior_engineer` and `reasoning_specialist` are both frontier-tier. Either
could, in principle, do the other's job. The pair exists for two reasons, and
the first is much more important than the second.

**1. Family diversity at the top tier.** Different model families have
different failure modes, and that difference is the entire value of a second
review. Bind both frontier roles to the same family and dual review degrades
into asking one model twice — expensive, and much less likely to catch the
thing the first pass missed. The `HIGH` and `CRITICAL` bands pair these two
roles specifically so the two reviewers come from different families.

**2. Task fit.** `reasoning_centric` routes work to whichever role suits the
bottleneck. This is a real but secondary effect.

If you ever find yourself binding both to one family — because a bridge is
down, say — that is a degradation. Record `cross_family_review: false` and
treat the second review as weaker than it looks.

## Current bindings

One binding table, not one per runtime. Roles are filled by whichever model
best fits the job regardless of provider, because both cross-provider bridges
are verified working. Provider choice is nearly free at delegation time — you
have already paid for a fresh context window — so it should be made on merit.

```
worker_fast           gpt-5.6-luna       (openai)
worker_balanced       claude-sonnet-5    (claude)
worker_balanced_alt   gpt-5.6-terra      (openai)  — same-family fallback only
senior_engineer       claude-opus-5      (claude)
reasoning_specialist  gpt-5.6-sol        (openai)
principal_architect   claude-fable-5     (claude)
```

The escalation ladder alternates families by construction:
`luna → sonnet-5 → opus-5 → sol → fable-5`. The first escalation crossing a
family boundary is deliberate — if the cheap worker's failure mode is
family-specific, staying in the family reproduces it.

**`worker_balanced_alt` exists for bridge failures, not to shorten the ladder.**
Use `gpt-5.6-terra` for the first escalation only when `claude-sonnet-5` is
unreachable. Reaching for it by default would put `worker_fast` and
`worker_balanced` in the same family and throw away the diversity that makes
the first escalation worth taking.

Degraded bindings for when a bridge is down are in `adapters.md`.

## Why `worker_fast` is bound to `gpt-5.6-luna`

Worth recording, because it is the one binding decision that rests on
measurement rather than structure.

**Price — verified.** `gpt-5.6-luna` is $0.20 / $1.20 per million input/output
tokens against `claude-haiku-4-5`'s $1.00 / $5.00: five times cheaper on input,
4.2× on output, and $0.02 vs $0.10 on cached input. For the role that carries
the volume, that difference compounds across every routine task.

**Quality — not established.** A five-task Tier 0/1 head-to-head (spec
implementation with edge cases, a scoped bug fix, a multi-file API migration,
test generation graded by mutants killed, and structural boilerplate — 47
hidden assertions plus 5 mutants, scored only on test results) returned 100%
for both models and 308s vs 304s wall time. The task set did not discriminate.
No primary source puts the two on the same benchmark.

So the binding rests on the verified price advantage, not on a demonstrated
quality advantage — which is exactly what the "cheapest capable model carries
the volume" principle asks for. If a harder eval later separates them, change
one line in the registry; the policy does not move.

`claude-haiku-4-5` remains bound as the `claude_only` fallback, so a dead
bridge costs latency and money, not capability.
