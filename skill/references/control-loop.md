# Control loop — escalation, retries, confidence, observability

## Escalation triggers

Escalate when any of these occurs:

1. The worker reports confidence below the configured threshold.
2. The worker cannot produce a stable implementation plan.
3. The same test fails after two **materially different** attempts.
4. The root cause remains unknown after reasonable investigation.
5. Requirements appear contradictory.
6. An architectural decision has multiple high-impact alternatives.
7. A critical-domain flag is detected.
8. A reviewer reports a `high` or `critical` correctness finding.
9. Independent reviewers disagree.
10. The worker requests scope beyond the original task.
11. The proposed change increases blast radius beyond the original estimate.
12. Required context exceeds the worker's reliable handling capacity.

Every one of these is **evidence**. None of them is "a stronger model exists and
I feel uneasy." That distinction is what keeps cost bounded: escalating on
availability rather than evidence means always escalating.

### Trigger 11 deserves its own paragraph

**Re-scoring mid-task is mandatory, not optional.** A task that begins at
`MEDIUM` and grows into auth-adjacent territory must be re-scored and re-routed
— including its review policy. The original classification was correct for the
task as understood at the time; it stops being correct the moment the task
changes shape.

This is the most commonly skipped rule in the whole policy, because by the time
scope has grown you are already deep in the work and re-routing feels like
losing progress. It isn't. Shipping an auth change through a `MEDIUM` review is
losing progress.

## Retry and loop limits

```yaml
same_model_same_effort:            1
same_model_higher_effort:          1
stronger_model:                    2
max_total_implementation_attempts: 4
max_review_rounds:                 3
max_judge_invocations:             1
require_new_evidence_on_same_tier: true    # "tier" = capability_tier of the
                                          # model that RAN, not the role label
                                          # and not what the role resolves to
                                          # once the failure is excluded
```

**A second attempt at the same tier must carry a changed hypothesis or new
evidence.** Without one, the attempt is not permitted and the router must
escalate instead.

The reason this rule is stated as a hard constraint rather than advice:
repeatedly asking the same model to try equivalent approaches is the dominant
cost-overrun mode in agent systems, and it does not feel like looping from the
inside. Each attempt looks like a fresh idea. The check is external and
mechanical on purpose — *what new information does this attempt have that the
last one didn't?* If the answer is "none", escalate.

**Exhausting the limits is a normal terminal state, not an error.** Stop and
surface the situation to a human with three things:

- what was tried,
- what evidence accumulated,
- what you believe the blocking uncertainty is.

That third item is the valuable one. A human picking up a stalled task wants to
know where the wall is, not to re-derive the attempt history.

Silent looping is the error. Silent stopping is nearly as bad.

## Routing confidence

Emit your own confidence in the **routing decision**, 0.0–1.0. This is separate
from the worker's confidence in its output.

| Confidence | Action |
|---|---|
| `>= 0.80` | Execute as routed |
| `0.60 – 0.79` | Execute, but raise the review band one level |
| `< 0.60` | Escalate the *routing decision itself* — re-classify at higher effort, or ask a human |

Low routing confidence must never be silently ignored. Both the value and the
reason for it belong in the emitted rationale, because "the router wasn't sure"
is exactly the context a human needs when the route turns out wrong.

The scorer computes a conservative default: confidence drops with maximum
uncertainty, with repeated prior failures, with unknown root cause, and when
fallbacks were applied. Those are the conditions under which a confidently
wrong route costs the most.

## Human-in-the-loop

These situations stop or gate rather than proceeding:

| Situation | Why |
|---|---|
| Retry budget exhausted | Four attempts without success means the task is not what the classification said it was |
| Any `CRITICAL` review | The router cannot verify an isolation receipt's provenance, so it never treats one as proof; a human confirms |
| Independence could not be established | **Terminal.** Disclosure is not a control — a route whose reviewers cannot hold distinct models is one where the implementer reviews itself |
| No adjudicator could be seated | **Human confirmation.** The route is still dispatchable; what a human takes over is adjudicating a disagreement, should one arise |
| Routing confidence below 0.60 | The router does not trust its own classification, and classification errors propagate everywhere downstream |

## Observability

Every route emits:

```yaml
routing_metrics:
  task_class:
  complexity:
  uncertainty:
  blast_radius:
  reversibility:
  risk_score:
  risk_band:
  band_overrides_applied: []
  critical_flags: []
  selected_role:
  selected_model:
  selected_effort:
  review_band:
  reviewers: []
  review_independence: enforced | planned | degraded | unavailable | not_applicable
  independence_compromised: true | false
  judge_unavailable: true | false
  review_depth_reduced: []   # reviewers seated below the tier their band asks for
  band_floor_unsatisfiable:  # that tier is unreachable under the binding in force
  cross_family_review: true | false
  fallbacks_applied: []
  escalation_count:
  retry_count:
  review_count:
  routing_confidence:
  final_success:
  rationale:            # names band + flags + fallbacks, in prose
```

Recommended additions where the runtime exposes them: `input_tokens`,
`output_tokens`, `latency_ms`, `estimated_cost`.

Without per-route cost visibility, every tuning decision downstream is guesswork
— you cannot tell an escalation that paid for itself from one that didn't.

## Cost guardrail

Optimize expected **total task cost**: correctness, engineering quality,
latency, money, and human intervention combined. Not the unit price of any
single call.

```
avoid:   cheap model × endless retries
avoid:   frontier model for every trivial task
prefer:  cheap model → one evidence-based retry → stronger model
```

The first failure mode is the more expensive one and the harder to notice,
because each individual call looks cheap. Six failed attempts on the cheap
model plus the human time to untangle the result costs far more than routing
correctly once.
