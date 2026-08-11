# Worked routing decisions

Every output below is real — produced by `scripts/route_task.py` against
`config/model-routing.yaml`, not written by hand. Roles are shown rather than
model ids, because concrete identifiers live in the registry and nowhere else;
resolve them there.

If you change the policy and these stop matching, the examples are wrong, not
the policy. Regenerate rather than edit by hand.

## Contents

- [The cheap path](#the-cheap-path)
- [Small task, critical domain](#small-task-critical-domain) ← regression case
- [Debugging with an unknown root cause](#debugging-with-an-unknown-root-cause) ← regression case
- [Payments architecture](#payments-architecture) ← regression case
- [Score exactly 10](#score-exactly-10) ← regression case
- [Reasoning-centric investigation](#reasoning-centric-investigation)
- [Save-data migration](#save-data-migration)
- [After a failed attempt](#after-a-failed-attempt)
- [When a model is unreachable](#when-a-model-is-unreachable)
- [When the retry budget is spent](#when-the-retry-budget-is-spent)
- [What these cases are meant to teach](#what-these-cases-are-meant-to-teach)

---

## The cheap path

**Task:** rename a symbol across 12 files. `c0 u0 b0 r0`

```
score=0  band=LOW   worker=worker_fast / LOW
review=LOW: worker_fast
  independence_required=false   review_independence=not_applicable
```

Twelve files sounds like a lot, and it routes to the cheapest model at the
lowest effort — correctly. The workload is large; the *task* is trivial.

Note `not_applicable` rather than `degraded`: a `LOW` band does not ask for
independence, so there is nothing to fail to enforce.

---

## Small task, critical domain

**Task:** add a scope check to an authorization path. `c1 u0 b1 r0`,
`auth_sensitive`

```
score=3  band=LOW → HIGH             overrides=[critical_domain]
worker=worker_balanced / HIGH
review=HIGH: senior_engineer, reasoning_specialist
  independence_required=true   review_independence=degraded
cross_family_review=true
```

**The raw score says `LOW`. The route is `HIGH` with dual independent review.**

Every dimension is honestly small — it really is a simple, reversible,
well-understood change — and it still must not ship on a single lightweight
review, because the consequence of being wrong in an auth path is not
proportional to the size of the diff.

`review_independence=degraded` here is not a failure; it is the honest default.
Isolation was never confirmed for this session, so the router declines to claim
it. Pass `--isolation available` once you have verified it and this becomes
`enforced`.

---

## Debugging with an unknown root cause

**Task:** users are intermittently logged out; nobody knows why.
`c2 u2 b1 r0`, `auth_sensitive`, `unknown_root_cause`

```
score=8  band=HIGH   overrides=[critical_domain,
                                low_routing_confidence_raised_review_to_CRITICAL]
worker=worker_balanced / MAX
review=CRITICAL: senior_engineer, reasoning_specialist   judge=principal_architect
routing_confidence=0.77          requires_human_confirmation=true
```

**Effort is `MAX`,** above the band's `HIGH` floor: `unknown_root_cause` maps
to `MAX` directly, because when you do not know what is wrong, thinking harder
is the only lever that reliably helps.

**Review is `CRITICAL`, one band above the risk band.** Confidence came out at
0.77, below the 0.80 threshold, so the review band was raised. The router is
saying: *I am not fully confident I classified this, so check it harder than my
own classification suggests.*

**And a judge is bound.** The judge follows the *review* band, not the risk
band — a review promoted to `CRITICAL` needs adjudication just as much as one
that scored there. An earlier version keyed the judge off the risk band and
produced a `CRITICAL` review with all five required checks and no adjudicator:
half a control, which is worse than none because it looks whole.

---

## Payments architecture

**Task:** design the payment-processing subsystem. `c3 u3 b3 r2`,
`financial_sensitive`

```
score=17 band=CRITICAL   overrides=[critical_domain, critical_irreversible]
worker=principal_architect / MAX
review=CRITICAL: senior_engineer, reasoning_specialist   judge=principal_architect
required_checks=[security, edge_cases, rollback, test_adequacy,
                 specification_compliance]
routing_confidence=0.75          requires_human_confirmation=true
```

`critical_irreversible` fired because a critical-domain flag met
`reversibility >= 2` — the worst case the policy knows how to describe:
expensive to get wrong, and you cannot simply undo it.

---

## Score exactly 10

**Task:** anything whose dimensions sum to 10. `c2 u2 b2 r0`

```
score=10  band=HIGH   — for all eleven task classes, without exception
```

This looks like a non-example and is in the test suite for a reason. An earlier
policy compared `score >= 10` in one section and configured `high_risk_max: 10`
in another, so a score of exactly 10 banded differently depending on which
branch reached it. Boundary bugs of that kind stay invisible until the one task
that lands on the boundary goes wrong.

---

## Reasoning-centric investigation

**Task:** prove whether a lock-ordering change can deadlock.
`c2 u2 b2 r0`, `reasoning_centric=true`

```
score=10  band=HIGH   worker=reasoning_specialist / HIGH
review=HIGH: senior_engineer, reasoning_specialist
```

Same score as the previous example, different worker. The dimensions do not
distinguish these two tasks — `reasoning_centric` does. Here the hard part is
establishing what is true, not writing code.

---

## Save-data migration

**Task:** migrate the user save-data format. `c3 u2 b3 r3`, `migration`,
`data_integrity_sensitive`

```
score=16 band=CRITICAL   overrides=[critical_domain, critical_irreversible,
                                    migration_data_integrity]
worker=principal_architect / MAX
review=CRITICAL: senior_engineer, reasoning_specialist   judge=principal_architect
```

Three overrides fire independently and agree. Each encodes a different reason
this is dangerous, and any one alone would produce the right band.

`rollback` is a required check, not an optional finding. For a migration this
irreversible, "we have a rollback plan" is part of the deliverable.

---

## After a failed attempt

**Task:** an ordinary feature; the fast worker already failed once.
`c1 u1 b1 r0`, `prior_failures=1`

```
score=5  band=MEDIUM   worker=worker_balanced / MEDIUM
review=MEDIUM: reasoning_specialist
note: escalated above failed tier worker_fast
```

Without the failure this routes to `worker_fast`. With it, the router refuses
to hand the task back to the tier that already failed.

**This escalation now fires even when no model was named.** It used to require
`prior_models` and silently lapse without it — the loop-prevention control
switching itself off on a missing optional field, exactly when it mattered.
`--prior-models` also accepts concrete model ids now, since `selected_model` is
emitted as an id and feeding it back has to work.

---

## When a model is unreachable

**Task:** a security-sensitive change with the OpenAI reasoning model
unavailable. `c2 u1 b2 r1`, `security_sensitive`,
`--unavailable reasoning_specialist`

```
score=9  band=HIGH   overrides=[critical_domain]
worker=worker_balanced / HIGH
review=HIGH: senior_engineer, reasoning_specialist
fallbacks=[reasoning_specialist: <openai reasoning> unavailable -> <claude senior>]
cross_family_review=FALSE
```

The route still emits — a missing model degrades the route, never fails it.

Two things to read carefully. First, the fallback **actually changed the
model**: five of ten (role × runtime) fallback paths used to substitute the
same model back and record a downgrade that never happened, which is a recorded
degradation with no degradation — the most misleading state a metric can be in.
A fallback is now recorded only when the emitted model differs.

Second, `cross_family_review=FALSE`. Both reviewer roles resolve to the same
family now, so the "two independent reviewers" share every failure mode. The
review still catches things; it catches considerably less than the count
suggests. The flag exists so nobody reads this route later and believes a
cross-family review happened.

---

## When the retry budget is spent

**Task:** anything, after four failed attempts. `prior_failures=4`

```
score=6  band=MEDIUM
TERMINAL=HUMAN_REQUIRED       selected_model=null
requires_human_confirmation=true
```

**No executable route is emitted, and the CLI exits nonzero.**

Exhausting the retry budget is a normal terminal state, not an error — but a
terminal state that still hands back a runnable model is not a stop, it is a
suggestion. The same applies below 0.60 routing confidence, which terminates as
`ESCALATE_ROUTING`.

The stop must carry three things to the human: what was tried, what evidence
accumulated, and what the blocking uncertainty is. The third is the valuable
one — someone picking up a stalled task wants to know where the wall is, not to
re-derive the attempt history.

---

## What these cases are meant to teach

**Size is not risk.** The rename touches 12 files and routes cheapest. The auth
scope check touches a handful of lines and routes to dual independent review.

**Overrides must be unconditional.** Three of these are cases an earlier policy
version got wrong, and all three failed the same way: a class-specific branch
returned before the flag check ran. Structure, not diligence, prevents that.

**Review depth is not the worker's business.** Because review is a function of
the band alone, no worker-selection path can weaken it — which is what makes
the invariants testable across every class and dimension combination rather
than on a few hand-picked examples.

**Say what you enforced, not what you asked for.** `independence_required` is
policy; `review_independence` is evidence. `fallbacks_applied` records only
real substitutions. `terminal` withholds the model rather than emitting one you
must not run. Every one of those pairs exists because collapsing it produced a
claim the system could not back.
