---
name: model-router
description: Choose which model and reasoning-effort level should do a piece of software-engineering work, and how thoroughly that work must be reviewed, based on complexity, uncertainty, blast radius, reversibility, task type, and prior failures. Use this whenever you are about to delegate implementation, debugging, refactoring, architecture, migration, investigation, or review work to a subagent or another model — and especially before touching authentication, authorization, payments, database schemas, data migrations, concurrency, or anything else where a mistake is expensive or hard to undo. Also use it when a first attempt has failed and you are deciding whether to retry or escalate, when two reviewers disagree, or when someone asks which model to use for a task.
---

# Model Router

You are deciding two things about a piece of work: **who should do it**, and
**how hard it should be checked**. Those are separate decisions, and keeping
them separate is the point of this skill.

The cheap model does the volume. Escalation happens on evidence, not on hunches.
Review depth tracks risk, not the worker you happened to pick. And you never
claim a safety property you did not actually enforce.

## When this applies

Route when you are about to **delegate** work — to a subagent, to another
model, to a fresh session. Delegation already costs you a context window, so
choosing well is nearly free at that point.

Do not route work you are simply going to do inline in your own turn. There is
no choice to make there: you are the executor. A one-line fix, a file read, a
question you can answer — just do it.

Skip routing entirely for trivial single-file edits with no ambiguity. The
routing overhead would exceed the task.

## Step 1 — Classify

This is the judgment part, and it is yours. Nothing downstream can be better
than this step, so spend real attention here.

**Task class** — exactly one:

```
MECHANICAL   IMPLEMENTATION  DEBUGGING    REFACTORING
ARCHITECTURE INVESTIGATION   MIGRATION    REVIEW
TESTING      DOCUMENTATION   OPERATIONS
```

**Four dimensions, each 0–3:**

| | 0 | 1 | 2 | 3 |
|---|---|---|---|---|
| **complexity** | rename a symbol | add a simple endpoint | refactor state across modules | redesign a distributed model |
| **uncertainty** | implementation is known | minor ambiguity | several plausible approaches | requirements or root cause unclear |
| **blast_radius** | isolated | one subsystem | broad product impact | critical system / user / business |
| **reversibility** | trivial rollback | easy rollback | difficult rollback | effectively irreversible |

Naturally high blast radius: authentication, authorization, payments, database
schema, save-data format, production deploys, concurrency, security,
irreversible migrations, shared protocols, public APIs.

**Route on uncertainty and blast radius, not on size.** Generating 20 similar
components is a large workload with near-zero uncertainty — cheap model.
Changing 5 lines in the auth path is a tiny workload with critical blast radius
— strong model, dual review. Token counts and file counts may inform how you
decompose the work; they must not drive who does it.

**Flags** — detect all that apply:

```
critical-domain (force overrides):
  security_sensitive  auth_sensitive  financial_sensitive  data_integrity_sensitive

elevating (each one has an observable effect — see the table below):
  concurrency_sensitive  migration  public_api_change
  production_hotfix  unknown_root_cause  review_disagreement

context (inform decomposition, never the band):
  unfamiliar_codebase  cross_service_change  long_horizon
  large_context  tool_heavy

operational (state of the runtime, not of the task):
  bridge_down
```

What each elevating flag actually does — a flag with no consumer is a promise
the system does not keep, so this table is enforced by test:

| Flag | Effect |
|---|---|
| `production_hotfix` | band floor `HIGH` |
| `concurrency_sensitive` | band floor `MEDIUM` |
| `public_api_change` | band floor `MEDIUM` |
| `migration` | with `data_integrity_sensitive` → band `CRITICAL` |
| `unknown_root_cause` | effort `MAX`, worker promotion, confidence penalty |
| `review_disagreement` | routes to the disagreement path and binds a judge |

**`reasoning_centric`** — one boolean that decides between the two frontier
roles:

- `true` when the bottleneck is deciding **what is correct**: logical
  verification, edge-case enumeration, spec consistency, concurrency and state
  reasoning, discriminating between root-cause hypotheses.
- `false` when the bottleneck is producing **correct code**: multi-file edits,
  API surface work, refactoring mechanics, framework idiom, tool orchestration.

Default to `false` when genuinely torn. Code-centric routing is cheaper and
recovers more gracefully from a wrong guess.

## Step 2 — Compute the route

Hand your classification to the scorer. It is deterministic, so the band, the
overrides, and the review policy come out the same every time:

```bash
python3 scripts/route_task.py --class DEBUGGING \
    --complexity 2 --uncertainty 3 --blast-radius 2 --reversibility 1 \
    --flags auth_sensitive,unknown_root_cause
```

Other inputs worth knowing:

| Flag | Use |
|---|---|
| `--format json` | machine-readable route |
| `--runtime codex` | the Codex effort spelling |
| `--prior-failures N` | after a failed attempt; `--prior-models` accepts role aliases *or* model ids |
| `--unavailable <role>` / `--unavailable-models <id>` | a specific role or model does not resolve |
| `--flags bridge_down` | the whole cross-provider transport is unreachable — switches to the degraded single-provider binding |
| `--isolation available\|unavailable` | whether you confirmed reviewer context isolation this session |

**`--isolation` matters more than it looks.** Left unset, the router reports
`review_independence: degraded` — it will not claim a safety property nobody
established. Pass `available` only after confirming isolation actually holds.

The script exits nonzero when the route reaches a terminal state. Those are
normal outcomes that need a human, not routes to execute:

| Terminal | Meaning |
|---|---|
| `HUMAN_REQUIRED` | the retry budget is spent; no executable route is emitted |
| `ESCALATE_ROUTING` | routing confidence fell below 0.60 — re-classify at higher effort or ask a human |

If you cannot run the script, compute it by hand from the tables below — the
script reads `config/model-routing.yaml`, and this file describes the same
policy, so the two must agree.

### The pipeline

Eight stages, and **no stage returns early**. That constraint is not stylistic:
an earlier version of this policy dispatched on task class with early returns
and checked critical-domain flags afterwards, so debugging an auth bug and
designing a payments architecture silently bypassed mandatory dual review. The
fix was to stop fusing "who does it" and "how it's reviewed" into one decision.

```
1 NORMALIZE  → class, 4 dimensions, flags, reasoning_centric
2 SCORE      → risk_score → band
3 OVERRIDE   → band adjusted by flags        [unconditional]
4 WORKER     → role, by class + band         [never returns]
5 EFFORT     → conceptual effort level
6 REVIEW     → policy, by BAND ONLY          [independent of stage 4]
7 RESOLVE    → aliases → available models, with fallbacks
8 EMIT       → route + rationale + confidence + metrics
```

Before you act on a route, check these five. They are also asserted by the
test suite, so if one looks false, something is genuinely broken:

- [ ] **I1** Every task reached review selection — no branch skipped it.
- [ ] **I2** A critical-domain flag put the review band at `HIGH` or above, for *every* task class.
- [ ] **I3** A critical-domain flag put the worker at `worker_balanced` or above.
- [ ] **I4** The route names only models confirmed available.
- [ ] **I5** The rationale names the band, the triggering flags, and any fallbacks.

### Score and bands

```
risk_score = complexity + 2×uncertainty + 2×blast_radius + reversibility     (0–18)
```

Uncertainty and blast radius carry double weight because complexity and
reversibility describe what the change *is*, while uncertainty and blast radius
together approximate what it might *cost*.

| Band | Score |
|---|---|
| `LOW` | 0 – 3 |
| `MEDIUM` | 4 – 7 |
| `HIGH` | 8 – 10 |
| `CRITICAL` | 11 – 18 |

Bands are contiguous and exhaustive, and every downstream rule is written in
bands. A raw-score comparison anywhere else is a bug — that ambiguity is what
made score 10 route differently depending on which branch you arrived through.

### Overrides

Applied **after** the band, **unconditionally**, for every task class:

```
any critical-domain flag                     → band = max(band, HIGH)
any critical-domain flag AND reversibility≥2 → band = CRITICAL
migration AND data_integrity_sensitive       → band = CRITICAL
production_hotfix                            → band = max(band, HIGH)
public_api_change                            → band = max(band, MEDIUM)
review_disagreement                          → disagreement path, regardless of band
```

Overrides only raise a band, never lower it.

### Worker by class and band

| Class | LOW | MEDIUM | HIGH | CRITICAL |
|---|---|---|---|---|
| `MECHANICAL` | worker_fast | worker_fast | worker_balanced | senior_engineer |
| `DOCUMENTATION` | worker_fast | worker_fast | worker_balanced | worker_balanced |
| `TESTING` | worker_fast | worker_fast | worker_balanced | senior_engineer |
| `IMPLEMENTATION` | worker_fast | worker_fast | worker_balanced | ‡ |
| `REFACTORING` | worker_fast | worker_balanced | worker_balanced | ‡ |
| `DEBUGGING` | worker_fast | worker_fast | worker_balanced | ‡ |
| `INVESTIGATION` | worker_fast | worker_balanced | reasoning_specialist | reasoning_specialist |
| `MIGRATION` | worker_balanced | worker_balanced | senior_engineer | principal_architect † |
| `ARCHITECTURE` | worker_balanced | worker_balanced | senior_engineer | principal_architect |
| `REVIEW` | worker_fast | worker_balanced | senior_engineer | senior_engineer + reasoning_specialist |
| `OPERATIONS` | worker_fast | worker_balanced | senior_engineer | senior_engineer |

**‡** `reasoning_specialist` if `reasoning_centric`, else `senior_engineer`.
**†** architecture phase only; implementation runs at worker_balanced / senior_engineer.

Then apply, in order:

```
ARCHITECTURE, uncertainty==3 or long_horizon  → principal_architect
DEBUGGING, unknown_root_cause & ≥2 failures   → at least the ‡ role
INVESTIGATION, unknown_root_cause             → at least worker_balanced
any critical-domain flag                      → at least worker_balanced
prior_failures ≥ 1                            → at least one tier above what failed
```

Role tiers, low to high: `worker_fast` → `worker_balanced` →
`senior_engineer` → `reasoning_specialist` → `principal_architect`.

Role profiles and what each is actually good at: `references/model-profiles.md`.

### Effort

```
MINIMAL < LOW < MEDIUM < HIGH < VERY_HIGH < MAX
```

| Work | Effort |
|---|---|
| formatting, rename, boilerplate | `LOW` |
| straightforward implementation | `MEDIUM` |
| multi-file feature, debugging, refactoring, architecture, standard review | `HIGH` |
| difficult debugging, multi-system refactoring | `VERY_HIGH` |
| complex architecture, unknown root cause, adversarial review | `MAX` |
| orchestration (default) | `HIGH` |

Floors override the table, never the reverse:

```
band HIGH                 → effort ≥ HIGH
band CRITICAL             → effort ≥ VERY_HIGH
any critical-domain flag  → effort ≥ HIGH
```

**As orchestrator, default to `worker_fast` at `HIGH`.** Classifying, building
the task graph, and detecting escalation conditions are well served by high
effort; `MAX` is for when the dependency graph is genuinely complex, five or
more subtasks interlock, requirements conflict, routing confidence is below
0.60, or failure would have `HIGH`+ blast radius.

## Step 3 — Review, by band alone

Review depth does not depend on which worker you picked. That independence is
exactly what makes I1 and I2 checkable: no worker-selection branch can quietly
weaken the review.

| Band | Reviewers | Effort | Independent |
|---|---|---|---|
| `LOW` | worker_fast | `MEDIUM` | no |
| `MEDIUM` | one stronger role, cross-family preferred | `HIGH` | yes |
| `HIGH` | senior_engineer + reasoning_specialist | `HIGH` | yes |
| `CRITICAL` | senior_engineer + reasoning_specialist | `MAX` | yes |

`CRITICAL` additionally requires every one of these to appear as an explicit
finding category — including when the answer is "checked, nothing found":

```
security   edge_cases   rollback   test_adequacy   specification_compliance
```

A `CRITICAL` review that silently omits one is invalid and must be re-run.

### Making independence real

Two reviews are independent only if reviewer B's input contains no token
derived from reviewer A's output. Stated as prose alone, this requirement is
violated by default — the natural implementation, asking one conversation for
two reviews in sequence, leaks the first into the second.

Each reviewer gets exactly: the diff, the task spec and acceptance criteria,
the relevant source, and the band's checklist. Not the other reviewer's
verdict, findings, confidence, or any paraphrase — and not even a hint that
another review is happening.

**In Claude Code:** dispatch each reviewer as a separate subagent via the
`Agent` tool, both in a single message so they run concurrently and neither can
observe the other.

**In Codex:** invoke each reviewer as a separate non-interactive execution with
a fresh session. Do not reuse a session id across reviewers.

**Either runtime, cross-family reviewer:** the bridge (`codex exec` /
`claude -p`) spawns a fresh process, so isolation holds by construction.

If you cannot achieve real isolation, **do not claim it**. Run sequentially with
the second reviewer forming its verdict before seeing anything else, record
`review_independence: degraded`, and treat `PASS + PASS` on `CRITICAL` as
`PASS_WITH_CHANGES` pending human confirmation. Claiming independence you did
not enforce is the most damaging thing this skill can produce, because it
converts a safety control into a false assurance.

### Reading verdicts

Reviewers return `verdict` (`PASS` / `PASS_WITH_CHANGES` / `FAIL`),
`confidence`, `findings`, `missing_tests`, `uncertainties`.

Reason about content, not the verdict token. A `PASS` carrying a
`critical`-severity finding is a contradiction — treat it as `FAIL` pending
clarification. A `PASS` with confidence below 0.5 is `PASS_WITH_CHANGES`.

Disagreement resolution and judge selection: `references/review-policy.md`.
The default judge is `principal_architect`; strongly code-local disputes may
use `senior_engineer` instead.

## Step 4 — Escalation and stopping

Escalate on **evidence**: a failed acceptance check, a stated low confidence,
an unstable plan, a reviewer finding. Not on a hunch, and not merely because a
stronger model exists.

A retry at the same tier must carry at least one of: new evidence (a log, a
repro, a bisect, a failing assertion), a materially different hypothesis, or a
stronger model. Asking the same model to try equivalent approaches again is the
dominant cost-overrun mode in agent systems — escalate instead.

```
same_model_same_effort:            1
same_model_higher_effort:          1
stronger_model:                    2
max_total_implementation_attempts: 4
max_review_rounds:                 3
max_judge_invocations:             1
```

**Re-score mid-task when scope grows.** A task that starts `MEDIUM` and drifts
into auth-adjacent territory must be re-scored and re-routed, review policy
included. This is mandatory, not optional.

Exhausting the retry budget is a **normal terminal state**, not an error. Stop
and tell the human what was tried, what evidence accumulated, and what you
believe the blocking uncertainty is. Silent looping is the error.

Emit your own routing confidence, 0.0–1.0: at 0.80+ execute as routed; at
0.60–0.79 execute but raise the review band one level; below 0.60 escalate the
routing decision itself — re-classify at higher effort or ask a human.

## Step 5 — Emit the route

Every route reports:

```yaml
task_class:  complexity:  uncertainty:  blast_radius:  reversibility:
reasoning_centric:
risk_score:  risk_band:   band_overrides_applied: []   critical_flags: []
route_path:                    # null, or "disagreement"
terminal:                      # null, HUMAN_REQUIRED, or ESCALATE_ROUTING
selected_role:  selected_model:  selected_effort:  selected_effort_native:
review:
  band:  reviewers: []  reviewer_models: []  effort:
  independence_required:       # what the band asks for
  review_independence:         # what the runtime actually got
  required_checks: []
  judge:  judge_model:
cross_family_review: true | false
fallbacks_applied: []          # only recorded when the model actually changed
unavailable_models: []
escalation_count:  retry_count:
routing_confidence:
requires_human_confirmation:
rationale:   # names the band, the triggering flags, and every fallback
```

Two pairs in there are deliberately not collapsed:

- **`independence_required` vs `review_independence`.** The first is policy; the
  second is evidence. Reporting policy as if it were evidence is how a control
  becomes a false assurance.
- **`selected_model` vs `terminal`.** A terminal state emits no model. A route
  you must not execute should not look executable.

Optimize expected **total** task cost — correctness, engineering quality,
latency, money, and human intervention together. Not the unit price of one
call.

```
avoid:   cheap model × endless retries
avoid:   frontier model for every trivial task
prefer:  cheap model → one evidence-based retry → stronger model
```

## Before the first route in a session

Establish what you can actually invoke. A route naming a model you cannot call
is worse than no route. Check the runtime, which model families are reachable,
whether effort control exists, whether the cross-provider bridge works, and
whether subagent isolation is available.

A model that does not resolve is unavailable — fall back per
`references/adapters.md` and record it. It must never be a hard failure.

## References

Read these when the situation calls for them; they are not needed for a routine
route.

- **`references/routing-policy.md`** — dimensions, bands, overrides, worker and
  effort selection in full, with the reasoning behind each weight.
- **`references/model-profiles.md`** — the five roles, what each is for, what
  each must not be the sole authority on, and the current bindings.
- **`references/review-policy.md`** — review by band, independence mechanics per
  runtime, the reviewer output contract, and disagreement resolution.
- **`references/control-loop.md`** — escalation triggers, retry limits, routing
  confidence, and the full observability schema.
- **`references/adapters.md`** — runtime differences, effort mapping, transports,
  and the complete fallback matrices. Read this when a model is unavailable or
  you are working across the provider bridge.
- **`references/examples.md`** — worked routing decisions, including the four
  cases an earlier version of this policy got wrong.

Configuration lives in `config/model-routing.yaml`. Model identifiers appear
there and nowhere else — when models or prices change, update the registry and
recalibrate; the role system stays fixed. That separation is what keeps this
portable across runtimes and alive across model generations.
