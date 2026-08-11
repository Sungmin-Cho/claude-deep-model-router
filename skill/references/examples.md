# Worked routing decisions

Every output below is real — produced by `scripts/route_task.py` against
`config/model-routing.yaml`, not written by hand. If you change the policy and
these stop matching, the examples are wrong, not the policy.

## Contents

- [The cheap path](#the-cheap-path)
- [Small task, critical domain](#small-task-critical-domain) ← regression case
- [Debugging with an unknown root cause](#debugging-with-an-unknown-root-cause) ← regression case
- [Payments architecture](#payments-architecture) ← regression case
- [Score exactly 10](#score-exactly-10) ← regression case
- [Reasoning-centric investigation](#reasoning-centric-investigation)
- [Save-data migration](#save-data-migration)
- [After a failed attempt](#after-a-failed-attempt)
- [When the bridge is down](#when-the-bridge-is-down)
- [What these cases are meant to teach](#what-these-cases-are-meant-to-teach)

---

## The cheap path

**Task:** rename a symbol across 12 files.

```
complexity 0   uncertainty 0   blast_radius 0   reversibility 0
```

```
score=0  band=LOW   worker=worker_fast / LOW
review=LOW: worker_fast              independent=false
```

Twelve files sounds like a lot, and it routes to the cheapest model at the
lowest effort — correctly. The workload is large; the *task* is trivial. Nothing
about it is uncertain and nothing breaks if it is wrong in a way tests won't
catch.

If your router sends this to a frontier model, it is routing on size.

---

## Small task, critical domain

**Task:** add a scope check to an authorization path. A few lines, well
understood, easy to revert.

```
complexity 1   uncertainty 0   blast_radius 1   reversibility 0
flags: auth_sensitive
```

```
score=3  band=LOW → HIGH             overrides=[critical_domain]
worker=worker_balanced / HIGH
review=HIGH: senior_engineer, reasoning_specialist   independent=true
cross_family_review=true
```

**The raw score says `LOW`. The route is `HIGH` with dual independent review.**

This is the shape of the regression. Every dimension is honestly small — it
really is a simple, reversible, well-understood change — and it still must not
ship on a single lightweight review, because the consequence of being wrong in
an auth path is not proportional to the size of the diff.

An earlier version of this policy dispatched on task class first and checked
critical-domain flags afterwards, so `IMPLEMENTATION` returned before the
override ran and this task got a `LOW` review. The fix was structural:
overrides are unconditional and run before any class dispatch, and review depth
is computed from the band alone so no worker-selection branch can undercut it.

---

## Debugging with an unknown root cause

**Task:** users are intermittently logged out. Nobody knows why yet.

```
complexity 2   uncertainty 2   blast_radius 1   reversibility 0
flags: auth_sensitive, unknown_root_cause
```

```
score=8  band=HIGH                   overrides=[critical_domain,
                                                low_routing_confidence_raised_review_to_CRITICAL]
worker=worker_balanced / MAX
review=CRITICAL: senior_engineer, reasoning_specialist   independent=true
routing_confidence=0.77
```

Three things worth noticing.

**Effort is `MAX`, above the band's `HIGH` floor.** `unknown_root_cause` maps to
`MAX` effort directly — when you do not know what is wrong, thinking harder is
the only lever that reliably helps. Guessing faster does not.

**Review is `CRITICAL`, one band above the risk band.** Routing confidence came
out at 0.77, below the 0.80 threshold, so the review band was raised
automatically. The router is saying: *I am not fully confident I classified
this correctly, so check it harder than my own classification suggests.* That
is exactly the right response to self-doubt in a system that cannot afford to
be quietly wrong.

**This is the case the earlier policy dropped entirely.** `DEBUGGING` returned
early, the auth flag was never examined, and an auth bug of unknown cause went
out on a single cheap review.

---

## Payments architecture

**Task:** design the payment-processing subsystem. Several viable approaches,
each hard to walk back.

```
complexity 3   uncertainty 3   blast_radius 3   reversibility 2
flags: financial_sensitive
```

```
score=17 band=CRITICAL               overrides=[critical_domain, critical_irreversible]
worker=principal_architect / MAX
review=CRITICAL: senior_engineer, reasoning_specialist   independent=true
required_checks=[security, edge_cases, rollback, test_adequacy, specification_compliance]
judge=principal_architect
routing_confidence=0.75
```

`critical_irreversible` fired because a critical-domain flag met
`reversibility >= 2`. That combination is the worst case the policy knows how
to describe: expensive to get wrong, and you cannot simply undo it.

Confidence is 0.75 — maximum uncertainty costs 0.20 — but the review band is
already `CRITICAL` and cannot go higher, so the confidence penalty has nowhere
to push. It still belongs in the rationale: a human reading this route should
know the router itself was unsure.

The earlier policy missed this one for the same reason it missed the auth bug:
`ARCHITECTURE` dispatched before the flag check.

---

## Score exactly 10

**Task:** any task whose dimensions sum to exactly 10.

```
complexity 2   uncertainty 2   blast_radius 2   reversibility 0
```

```
score=10  band=HIGH   — for all eleven task classes, without exception
```

This looks like a non-example, and it is in the test suite for a reason. The
earlier policy compared `score >= 10` in one section and configured
`high_risk_max: 10` in another, so a score of exactly 10 banded differently
depending on which branch reached it. Boundary bugs of that kind are invisible
until the one task that lands on the boundary goes wrong.

The test asserts all eleven classes agree at exactly 10. Bands are defined once,
contiguously, and every downstream rule reads the band rather than the score.

---

## Reasoning-centric investigation

**Task:** prove whether a lock-ordering change can deadlock.

```
complexity 2   uncertainty 2   blast_radius 2   reversibility 0
reasoning_centric: true
```

```
score=10  band=HIGH   worker=reasoning_specialist / HIGH
review=HIGH: senior_engineer, reasoning_specialist
```

Same score as the previous example, different worker. The dimensions do not
distinguish these two tasks — `reasoning_centric` does. Here the hard part is
not writing code, it is establishing what is true, so the work goes to the
frontier reasoning role rather than the frontier engineering role.

Set `reasoning_centric` honestly. A task routed to the reasoning specialist
that actually needed careful multi-file editing will produce a well-argued
answer to the wrong question.

---

## Save-data migration

**Task:** migrate the user save-data format. Irreversible once users have
written new-format data.

```
complexity 3   uncertainty 2   blast_radius 3   reversibility 3
flags: migration, data_integrity_sensitive
```

```
score=16 band=CRITICAL   overrides=[critical_domain, critical_irreversible,
                                    migration_data_integrity]
worker=principal_architect / MAX
review=CRITICAL: senior_engineer, reasoning_specialist
required_checks=[security, edge_cases, rollback, test_adequacy, specification_compliance]
```

Three overrides fire independently and agree. That redundancy is intentional —
each encodes a different reason this task is dangerous, and any one of them
alone would still produce the right band.

`rollback` is a required check, not an optional finding. For a migration this
irreversible, "we have a rollback plan" is part of the deliverable.

The architect routing covers the *design* phase. Implementing the migration
runs at `worker_balanced` / `senior_engineer` once the design is settled —
there is no reason to spend architect-tier tokens on writing the transform.

---

## After a failed attempt

**Task:** an ordinary feature. The fast worker already tried and failed once.

```
complexity 1   uncertainty 1   blast_radius 1   reversibility 0
prior_failures: 1   prior_models: [worker_fast]
```

```
score=5  band=MEDIUM   worker=worker_balanced / MEDIUM
review=MEDIUM: reasoning_specialist
note: escalated above failed tier worker_fast
```

Without the failure this routes to `worker_fast`. With it, the router refuses
to hand the task back to the tier that already failed.

That refusal is the point. Retrying the same tier is only permitted with new
evidence or a materially different hypothesis; absent either, the policy
escalates rather than letting the loop spin. Re-running the same model on the
same input is the most common way agent systems burn budget without noticing,
because every individual retry looks cheap and looks like a fresh idea.

Note also that the reviewer is `reasoning_specialist`, not `worker_balanced` —
the reviewer table picks a reviewer *stronger than the implementer* and from a
different family, and the implementer just moved up a tier.

---

## When the bridge is down

**Task:** a security-sensitive change, with the cross-provider bridge
unavailable so the OpenAI reasoning model cannot be reached.

```
complexity 2   uncertainty 1   blast_radius 2   reversibility 1
flags: security_sensitive          unavailable: reasoning_specialist
```

```
score=9  band=HIGH   overrides=[critical_domain]
worker=worker_balanced / HIGH
review=HIGH: senior_engineer, reasoning_specialist
fallbacks=[reasoning_specialist: unavailable -> claude_senior]
cross_family_review=FALSE
```

The route still emits. A missing model degrades the route; it never fails it.

But look at `cross_family_review`. Both reviewer roles now resolve to the same
Claude model, so the "two independent reviewers" are the same model asked
twice. The review still runs and still catches things — it just catches
considerably less than the metric name suggests, because the second reviewer
shares every failure mode of the first.

The flag exists so that nobody reads this route later and believes a
cross-family review happened. Degradation you disclose is a managed risk;
degradation you hide is a false assurance, and a false assurance is worse than
no assurance because it stops anyone from looking.

---

## What these cases are meant to teach

**Size is not risk.** The rename touches 12 files and routes cheapest. The auth
scope check touches a handful of lines and routes to dual independent review.

**Overrides must be unconditional.** Three of these examples are cases an
earlier policy version got wrong, and all three failed the same way: a
class-specific branch returned before the flag check ran. Structure, not
diligence, is what prevents that.

**Review depth is not the worker's business.** Because review is a function of
the band alone, no worker-selection path can weaken it. That is what makes the
invariants testable across every class and every dimension combination rather
than on a few hand-picked examples.

**Disclose every degradation.** A route that looks weak in hindsight is a very
different problem depending on whether the strong option was available at the
time. The metrics should let someone reconstruct not just what was decided, but
what could have been.
