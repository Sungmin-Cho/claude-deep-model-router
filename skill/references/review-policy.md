# Review policy

## Contents

- [Depth follows the band, and only the band](#depth-follows-the-band-and-only-the-band)
- [The four bands](#the-four-bands)
- [Reviewer independence](#reviewer-independence)
- [Enforcing isolation per runtime](#enforcing-isolation-per-runtime)
- [Reporting what you actually got](#reporting-what-you-actually-got)
- [Reviewer output contract](#reviewer-output-contract)
- [Reading verdicts](#reading-verdicts)
- [Disagreement resolution](#disagreement-resolution)

## Depth follows the band, and only the band

Review depth does not depend on which worker was selected.

This is worth stating flatly because the alternative is so tempting: "we used
the strong model, so a lighter review is fine." That reasoning inverts the
control. The whole reason to review is that the implementer might be wrong, and
a strong implementer being wrong is not less dangerous than a weak one being
wrong — it is more dangerous, because the output is more plausible.

Making review a pure function of the band also makes the safety invariants
checkable. If review depended on the worker, then every branch in worker
selection would be a branch that could weaken review, and no test could cover
them all.

## The four bands

### `LOW`

```yaml
reviewers: [worker_fast]
effort: MEDIUM
independent: false
```

Formatting, isolated UI, mechanical refactor, generated tests.

### `MEDIUM`

```yaml
reviewer_count: 1
candidates: [worker_balanced, senior_engineer, reasoning_specialist]
prefer_cross_family: true
effort: HIGH
independent: true
```

Exactly one independent reviewer, from a different family where available, and
at least as strong as the implementer.

"At least as strong", not "stronger": the two frontier roles are peers, so once
the implementer is already at the top of the ladder the reviewer can only be a
sibling. `reasoning_specialist` reviewing `senior_engineer` work — and the
reverse — is the intended pairing, and it is the *family* difference doing the
work there, not a tier difference. The same applies when a `MEDIUM`-band task
gets promoted to `principal_architect` by the architecture rule: its reviewer
is a peer, not a superior.

Pick the first candidate that is both available and a different family from the
implementer:

| Implementer | Preferred reviewer |
|---|---|
| `worker_fast` | `worker_balanced` |
| `worker_balanced` | `reasoning_specialist` |
| `worker_balanced_alt` | `senior_engineer` |
| `senior_engineer` | `reasoning_specialist` |
| `reasoning_specialist` | `senior_engineer` |
| `principal_architect` | `reasoning_specialist` |

If no cross-family reviewer is available, use the strongest available
same-family reviewer and record `cross_family_review: false`. A same-family
review is worth having; it is just worth less, and the metric should say so.

### `HIGH`

```yaml
reviewers: [senior_engineer, reasoning_specialist]
effort: HIGH
independent: true      # must be structurally enforced
```

### `CRITICAL`

```yaml
reviewers: [senior_engineer, reasoning_specialist]
effort: MAX
independent: true
required_checks:
  - security
  - edge_cases
  - rollback
  - test_adequacy
  - specification_compliance
disagreement_judge: principal_architect
```

Every `required_check` must appear as an explicit finding category in the
reviewer's output, **including when the verdict is `PASS`** — recorded as
"checked, no finding". A silent omission is indistinguishable from a check that
never ran, so a `CRITICAL` review missing one is invalid and must be re-run.

## Reviewer independence

Two reviews are independent if and only if reviewer B's input contains no token
derived from reviewer A's output, transitively.

Each reviewer's input is exactly:

- the diff or change under review,
- the task specification and acceptance criteria,
- the relevant source context,
- the review checklist for the band.

And explicitly excludes: the other reviewer's verdict, findings, confidence, or
any paraphrase or summary of them — and any hint that another review is in
progress at all. Even knowing a second opinion exists changes how a reviewer
calibrates.

**Stated as prose alone, this requirement is violated by default.** The natural
implementation — asking one conversation for two reviews in sequence — leaks
the first review into the second's context. Independence has to be a property
of the mechanism, not an instruction.

## Enforcing isolation per runtime

Both runtimes have the same two mechanisms available: a native subagent for the
same-family reviewer, and a CLI bridge for the cross-family one.

| Runtime | Reviewer 1 (native) | Reviewer 2 (bridged) |
|---|---|---|
| Claude Code | `Agent` tool subagent | `codex exec` |
| Codex | `multi_agent` subagent | `claude -p` |

**Claude Code:** dispatch each reviewer as a separate subagent via the `Agent`
tool, and issue both dispatches **in a single message** so they run
concurrently and neither can observe the other's result. Never include reviewer
A's returned text in reviewer B's prompt. Subagent context isolation is the
enforcement boundary.

**Codex:** invoke each reviewer as a separate non-interactive execution with a
fresh session. Do not reuse a session identifier across reviewers, and do not
pipe reviewer A's stdout into reviewer B's prompt construction.

**The bridge is isolated by construction.** `codex exec` and `claude -p` each
spawn a fresh process with a fresh session, so the cross-family reviewer cannot
see the other's context even in principle. That is a nice property to have on
the reviewer that matters most.

**Codex native subagents carry an unverified assumption.** The `multi_agent`
feature is enabled, but whether its context isolation matches the Claude
`Agent` tool's has not been empirically confirmed. Until it has, verify
isolation on the first dual review of a session, or run both Codex reviewers as
separate `codex exec` processes, which is isolated for certain.

## Reporting what you actually got

`independence_required` is what the band asks for. `review_independence` is
what was established. Keeping them apart is the whole point — collapsing them
converts a safety control into a false assurance, and false assurance is worse
than no assurance because nobody goes looking for the problem.

| State | Meaning |
|---|---|
| `not_applicable` | the band does not require independence |
| `degraded` | nobody established whether isolation is possible |
| `unavailable` | positive evidence that it cannot be achieved here |
| `planned` | attested achievable, not yet demonstrated |
| `enforced` | one distinct session identifier per reviewer, supplied after dispatch |

Two distinctions in that table are load-bearing.

**`unavailable` is not `degraded`.** One is a confirmed gap, the other is an
unchecked assumption, and they call for different responses. The config's own
ledger records Codex native subagent isolation as flag-verified but
semantics-unverified — exactly the case that needs its own state rather than
being folded into "unknown".

**`planned` is not `enforced`.** A route is computed before any reviewer runs,
so nothing available at routing time can prove isolation happened. A caller's
attestation that isolation is *achievable* is a capability claim; distinct
per-reviewer session identifiers, supplied afterwards, are the closest thing to
evidence the interface has.

**But `enforced` is still only a report.** The router cannot verify a receipt's
provenance — it is a caller-supplied string, unbound to any actual dispatch. A
`CRITICAL` review therefore always requires human confirmation regardless of
what independence reports. Anything else would let the policy's strongest
control be opened by typing two words, which is precisely the false assurance
this section exists to prevent.

**Independence is also a property of the resolved models, not the roles.** Two
reviewer roles that resolve to the same model are one reviewer with two labels.
Under a degraded single-provider binding this is the normal case, so the router
substitutes on resolved-model collisions and reports
`independence_compromised` when no distinct model is available for a slot.

When real isolation cannot be achieved, run the reviewers sequentially with the
second explicitly instructed to form its verdict *before* being shown anything
else, record the honest state, and treat `PASS + PASS` at `CRITICAL` as
`PASS_WITH_CHANGES` pending a human.

## Reviewer output contract

```yaml
verdict: PASS | PASS_WITH_CHANGES | FAIL
confidence: 0.0 - 1.0
findings:
  - severity: critical | high | medium | low
    category: correctness | security | architecture | maintainability |
              performance | testing | specification
    description: <what is wrong>
    evidence: <file:line, a repro, or the reasoning chain>
    required_fix: <what must change>
missing_tests:
  - <description>
uncertainties:
  - <what the reviewer could not determine, and why>
```

The `uncertainties` field earns its place: a reviewer that could not evaluate
something is giving you different information from a reviewer that evaluated it
and found nothing, and collapsing the two loses the signal.

## Reading verdicts

**Reason about content, not the verdict token.** Models are quite capable of
writing `PASS` above a paragraph describing a serious problem.

- `PASS` carrying a `critical`-severity finding is a contradiction. Treat it as
  `FAIL` pending clarification.
- `PASS` with `confidence < 0.5` is `PASS_WITH_CHANGES`.
- `PASS` with unresolved entries in `uncertainties` at band `HIGH`+ is not a
  clean pass — resolve the uncertainty or escalate it.

## Disagreement resolution

Given two independent reviews R1 and R2:

| R1 | R2 | Resolution |
|---|---|---|
| `PASS` | `PASS` | Accept — unless an unresolved `critical`/`high` finding or an unresolved `uncertainties` entry exists |
| `PASS` | `PASS_WITH_CHANGES` | Apply changes, then one lightweight re-review |
| `PASS_WITH_CHANGES` | `PASS_WITH_CHANGES` | Apply the union of changes, then one lightweight re-review |
| `PASS` | `FAIL` | **Judge** |
| `FAIL` | `PASS` | **Judge** |
| `PASS_WITH_CHANGES` | `FAIL` | Judge if the `FAIL` cites `critical`/`high` severity; otherwise return for reimplementation |
| `FAIL` | `FAIL` | Return for reimplementation — no judge needed, they agree |

### Judge selection

```
default                      → principal_architect
strongly code-local dispute  → senior_engineer      (when architect cost/latency is undesirable)
formal-reasoning dispute     → reasoning_specialist / MAX
```

The judge receives **both** reviews plus the original independent input, and
must produce a verdict *plus* an explicit statement of which reviewer was
correct on each contested finding. That second requirement matters: a judge
that only issues a verdict gives you no way to tell whether it engaged with the
disagreement or just picked a side.

The judge's verdict is final for that round, and it counts against
`max_review_rounds`.
