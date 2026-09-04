# Design decisions

Every decision of consequence in this project, why it was made, what was
rejected, and what it costs. Code comments reference these by id (`D-07`).

`docs/decisions.md` is the five-paragraph version for someone with two minutes.
This is the long version. `docs/TESTING_GUIDE.md` says how to attack each of
these and find out whether it holds.

**Format.** Each entry states the decision, the reasoning, the alternative that
was rejected and why, and the cost — because a decision with no cost is usually
a decision that wasn't made.

---

## Contents

- [Architecture](#architecture) — D-01 to D-08
- [The agent](#the-agent) — D-09 to D-19
- [The evaluation harness](#the-evaluation-harness) — D-20 to D-33
- [Operations and security](#operations-and-security) — D-34 to D-42
- [Decisions deliberately deferred](#decisions-deliberately-deferred)

---

## Architecture

### D-01 — Lambda for the agent

**Decision.** The triage agent runs as a single Lambda function behind API
Gateway.

**Why.** Alerts arrive sporadically and unpredictably — that is the definition
of an alert. The workload is bursty, idle most of the time, and each unit of
work is independent. That is the shape Lambda is for. A container running
24/7 to handle a handful of alerts a day is paying for idle.

**Rejected.** ECS/Fargate service — real cost while idle, and a load balancer to
maintain. EC2 — everything above plus patching.

**Cost.** Cold starts (see D-16), a 15-minute hard ceiling that the eval runner
cannot live within (D-03), and no local state between invocations.

**Where.** `agent/handler.py`, `infra/setup.sh` step `lambda`.

---

### D-02 — API Gateway HTTP API, not REST API

**Decision.** HTTP API (`apigatewayv2`).

**Why.** This endpoint needs one route, one Lambda integration, and nothing
else. HTTP APIs are roughly 3.5x cheaper per million requests and materially
lower latency.

**Rejected.** REST API — buys request validation, WAF integration, usage plans,
API keys, and request/response transformation. Every one of those is unused
here. A Lambda function URL was also considered and rejected: it would work, but
it offers no route to add an authorizer later without changing the front door.

**Cost.** No built-in request validation, so validation is explicit in the
handler (D-18). No usage plans, so no per-caller rate limiting.

**Where.** `infra/setup.sh` step `api`.

---

### D-03 — ECS Fargate for the batch eval runner

**Decision.** The full 38-case sweep runs as a Fargate task, not a Lambda.

**Why.** A concrete constraint, not a preference. A sweep with a real model
makes roughly 6 Converse calls per case plus a judge call — around 230 model
calls, at 5–40 seconds each. Even with concurrency that is comfortably past
Lambda's 15-minute ceiling. Fargate has no execution time limit and the task
runs rarely enough that per-second billing is cheap.

**Rejected.** Step Functions fanning out to Lambdas — would work, and adds a
state machine to maintain plus per-case cold starts, for a job that is one
long-running process. Splitting the sweep into chunks that each fit in 15
minutes — a workaround for the wrong constraint.

**Cost.** A container image to build and push, two IAM roles instead of one
(task role vs task execution role), and a VPC to run in.

**Status.** Designed and IAM'd, not deployed. `infra/iam-policies/eval-runner-*.json`
are written; the task definition is not. Marked optional in the project plan and
the sweep runs locally today with `python -m eval.run --mode aws`.

---

### D-04 — DynamoDB, not RDS

**Decision.** DynamoDB for runbooks, deploy history, incidents, and eval results.

**Why.** Every access pattern here is a key lookup or a key-range scan:

| Need | Access pattern |
|---|---|
| Recent deploys for a service | `PK = DEPLOY#<service>`, `SK` between two timestamps |
| All runbooks | `PK = RUNBOOK` |
| Is there an open incident for this alarm? | `PK = INCIDENT#<fingerprint>`, `SK = ACTIVE` |
| Results of one eval run | `PK = RUN#<run_id>` |

None of these needs a join, an aggregate, or an ad-hoc query. They need
single-digit-millisecond point reads from a Lambda that may be cold. DynamoDB
also gives the conditional write the dedupe depends on (D-07) as a primitive,
and on-demand billing means the idle cost is storage alone.

**Rejected.** RDS/Postgres — better at exactly the things this workload never
does, and comes with a VPC, connection pooling from Lambda (a genuine problem),
an instance billing hourly whether or not anything queries it, and patching.

**Cost.** Access patterns had to be designed before the schema, and adding a new
one later may need a GSI or a migration. No ad-hoc SQL for exploring the data.

---

### D-05 — SNS to email, not PagerDuty or Slack

**Decision.** Paging publishes to an SNS topic with an email subscription.

**Why.** The project is about *whether* to page, not about the transport. SNS
proves the integration end to end and takes half an hour. A real PagerDuty
integration is a day of OAuth and webhook work that demonstrates nothing this
doesn't.

**Rejected.** PagerDuty Events API, Slack incoming webhook.

**Cost.** No acknowledgement, no escalation policy, no on-call schedule. The
agent cannot tell whether a page was seen.

---

### D-06 — Single-table design

**Decision.** One DynamoDB table, composite `PK`/`SK`, no secondary indexes.

**Why.** All six entity types (runbook, deploy, incident, incident audit, eval
run, eval case) are addressed by a partition key with an optional sort-key
range. There is no query that needs to reach an item by anything other than the
key it was written under, so there is nothing for a GSI to do. One table also
means one ARN in the IAM policy (D-34) and one thing to delete.

**Rejected.** A table per entity — more resources, more IAM statements, no
benefit at this scale.

**Cost.** The key scheme is a convention that lives in code rather than in the
schema. `PK = "RUNBOOK"` and `PK = "DEPLOY#checkout-api"` are only meaningful
because the code agrees they are. This is the standard single-table trade and it
gets worse as entity count grows.

**Where.** `eval/backends.py:create_table`, key layout documented in
`docs/decisions.md`.

---

### D-07 — Dedupe with a conditional write, not a transaction

**Decision.** `create_incident` does one `PutItem` with

```
ConditionExpression = "attribute_not_exists(PK) OR opened_at_epoch < :cutoff"
```

on `PK = INCIDENT#<fingerprint>`, `SK = ACTIVE`.

**Why.** There is exactly one item whose consistency matters. A single-item
conditional write is atomic at the item level, which is all the atomicity this
problem has. It costs one WCU and one round trip.

The condition is a genuine **sliding window**, not a time bucket. With fixed
buckets, an alert at 14:59 and another at 15:01 fall in different buckets and
both open an incident — the storm gets through the dedupe precisely at the
boundary. The `opened_at_epoch < :cutoff` form compares against when the
incumbent incident was opened, so it holds regardless of where the clock is.

`ReturnValuesOnConditionCheckFailure="ALL_OLD"` returns the incumbent in the
error, so the losing writer recovers the live incident id without a second read.

**Rejected.** `TransactWriteItems` — 2x the write units, a second failure mode
(`TransactionCanceledException` with its own reason codes to interpret), and it
protects a multi-item invariant that does not exist here. A read-then-write —
the classic race: two Lambdas both read "no incident" and both write.

**Cost.** Only one item is protected. The audit copy written afterwards is
best-effort by design: if it fails, the incident still exists and the triage
still completes. Failing the whole triage because an audit row did not land
would be the wrong trade.

**Subtlety worth knowing.** The item returned in the condition-failure error
arrives in raw DynamoDB wire format (`{"S": "INC-..."}`) even when the write
went through the resource-layer `Table.put_item` — the resource layer
deserialises successful responses only, not exceptions. Reading it directly
yields a dict where a string is expected, and the dedupe silently returns an
unusable incident id. `agent/tools.py:deserialise_item` handles both shapes;
`tests/test_dedupe.py` pins it.

**Where.** `agent/tools.py:create_incident`, `tests/test_dedupe.py`.

---

### D-08 — Runbook search: one Query, ranked in Python

**Decision.** All runbooks live under `PK = "RUNBOOK"`. `search_runbook` reads
the whole partition and ranks by weighted token overlap in Python.

**Why.** The corpus is ten entries and bounded — it grows when someone writes a
runbook, not with traffic. One Query returns all of it for a fraction of an RCU.
Ranking in Python is deterministic, unit-testable, and explainable: the tool
returns which terms matched and the score, so a bad match is diagnosable rather
than mysterious.

**Rejected.** A keyword GSI — real infrastructure to maintain and keep
consistent, for a corpus that fits in one read. OpenSearch or a Bedrock
Knowledge Base — a vector store with an embedding pipeline, ongoing cost, and
non-deterministic retrieval that would make the groundedness metric harder to
reason about. That is a different project, and the brief explicitly excludes it.

**Cost.** This stops scaling somewhere in the low hundreds of runbooks, at which
point the whole partition no longer fits comfortably in one read and keyword
overlap starts returning nonsense. The fix at that point is a real search index,
and this decision would be revisited.

**Where.** `agent/tools.py:search_runbook`.

---

## The agent

### D-09 — Five tools, split into investigation and action

**Decision.** Three read-only investigation tools (`get_service_metrics`,
`get_recent_deploys`, `search_runbook`) and two side-effecting action tools
(`create_incident`, `page_oncall`).

**Why.** The split is not cosmetic — it is what makes the evaluation metrics
independent. Metric 1 scores investigation tools only; metric 4 scores the
action. If `page_oncall` counted in both, two supposedly independent metrics
would move together and the harness would be double-weighting escalation while
appearing to measure two things (D-21).

It also matches how the risk is distributed: read-only calls are cheap and
recoverable, the two writes are not.

**Cost.** A sixth "submit verdict" tool would give cleaner structured output.
Rejected because the verdict is already inferable from the side effects (D-11)
and the brief specifies five tools.

---

### D-10 — Tool failures are returned to the model, not raised

**Decision.** `dispatch` never raises. Bad arguments, unknown tools, and AWS
errors all come back as a `toolResult` with `status="error"` and a description.

**Why.** A model that passes `window_minutes: "thirty"` has made a formatting
slip it can correct if told. Raising out of the loop loses the entire
investigation — every tool call already made, and the reasoning built on them —
because of a typo. Returning the error keeps the loop alive and gives the model
the information it needs.

The failed call is still appended to the trace, so the eval sees it happened.

**Rejected.** Raising and returning a 500 — turns a recoverable slip into an
outage. Silently coercing bad input — hides real problems and corrupts the
tool-parameter metric.

**Cost.** A model can loop retrying the same broken call. The iteration cap
(D-13) bounds that.

**Where.** `agent/tools.py:dispatch`.

---

### D-11 — The decision is read from side effects, never from the model's report

**Decision.** `TriageResult.paged` is true because a `page_oncall` call
succeeded — not because the model wrote `"decision": "PAGE"`.

**Why.** Models misreport their own behaviour. An eval that scores self-reports
is measuring the model's honesty about its actions, which is a different and
less useful thing than measuring the actions. In a system where "paged" means a
human's phone rang, only the side effect is real.

The self-report is still captured and compared, and the gap between the two is
reported as `decision_consistent` — a free extra signal costing one boolean, and
one that catches a specific real failure: the agent that says it escalated but
whose page was blocked by the guardrail (D-12).

**Cost.** The decision is coarser than a self-report could be: three outcomes
inferred from which tools ran, rather than whatever nuance the model expressed.

**Where.** `agent/agent.py:_apply_side_effects`, `_apply_self_report`.

---

### D-12 — `page_oncall` requires an incident id from this investigation

**Decision.** Paging against an id that `create_incident` did not return in this
run is rejected as a recoverable tool error.

**Why.** Without it, a model can hallucinate an incident id and page a human
about a record nobody can look up. That is the worst possible page: it wakes
someone and gives them nothing to act on. Returning it as a recoverable error
rather than a hard failure lets the model call `create_incident` and try again.

**Cost.** An agent that genuinely should page cannot do so until it opens an
incident — one extra tool call on every real escalation. That is the right
ordering anyway: there should be a record before there is a page.

**Where.** `agent/tools.py:page_oncall`, exercised by the `phantom_incident`
scripted policy.

---

### D-13 — Iteration cap with a forced final decision

**Decision.** The loop runs at most `max_tool_iterations` (default 8). If the
model still wants tools at the cap, the tool config is dropped and it is asked
once more for a verdict on the evidence already gathered.

**Why.** The cap alone is not enough. Without the forced turn, a model that
never stops produces no decision — and "no decision" scores as "did not page",
silently inflating the false-negative rate with what is really a harness
failure. A missed page caused by the harness looks identical in the numbers to a
missed page caused by the agent, and that is the most dangerous kind of
measurement error.

8 leaves room for the five tools plus a retry after a validation error. A full
investigation uses six; `tests/test_harness_e2e.py` asserts no case hits the cap.

**Where.** `agent/agent.py:run_triage`, the `for...else` clause.

---

### D-14 — Temperature 0

**Decision.** `temperature: 0.0` for the agent and the judge.

**Why.** Two of the four metrics are deterministic given a trace. If the model
samples, the trace itself varies run to run, and a score change could be either
a real regression or a reroll. Temperature 0 removes one source of variance so
that when a number moves, the prompt or the code moved it. For the judge it is
non-negotiable: a judge that scores the same trace differently on Tuesday is not
a measurement.

**Cost.** Real production traffic would likely run non-zero. Nothing here
measures the variance a real deployment would see — a limitation stated in the
README rather than hidden.

---

### D-15 — The alert fingerprint excludes the reading

**Decision.** `sha256(service | environment | alarm_name | metric)`. Not the
value, not the duration.

**Why.** An alert storm is the *same* alarm re-firing with a slightly different
reading each time: 94%, then 96%, then 93%. If the value were part of the
identity, every re-fire would produce a new fingerprint, the dedupe would never
match, and the storm would open an incident per firing — which is exactly the
bug the dedupe exists to prevent.

`environment` *is* included, so a dev alarm and a prod alarm on the same service
and metric are correctly separate incidents.

**Cost.** Two genuinely different problems that happen to share an alarm name,
service, and metric would dedupe together within the window. Acceptable: that is
what an alarm name is for.

**Where.** `agent/tools.py:alert_fingerprint`.

---

### D-16 — boto3 clients constructed at module scope

**Decision.** Clients are built once per container and cached, not per
invocation.

**Why.** Constructing a boto3 client parses a JSON service model from disk,
resolves the endpoint, and loads credentials — tens to hundreds of milliseconds.
Doing it inside the handler pays that on every invocation; at module scope it is
paid once per cold start and every warm invocation reuses it. It is the single
largest easy win on Lambda p50.

**Cost.** Anything cached at module scope survives between invocations, so it
must be genuinely immutable. That is why `Config` is a frozen dataclass and why
nothing else is cached. `agent/aws.py:reset_cache` exists for tests.

**Where.** `agent/aws.py`.

---

### D-17 — CloudWatch EMF, not PutMetricData, from the request path

**Decision.** The Lambda emits custom metrics as Embedded Metric Format log
lines. The eval harness uses `PutMetricData` directly.

**Why.** `PutMetricData` is a synchronous network call inside the request. It
adds latency, it can throttle, and it introduces a failure mode you then have to
decide how to handle — either it breaks triage or you swallow it and lose the
metric silently. EMF is a specially-shaped line on stdout, which Lambda already
ships to CloudWatch Logs. CloudWatch extracts the metrics asynchronously. Zero
added latency, zero new failure mode, no extra API cost.

The harness is the exception because it runs as a batch job off the hot path and
wants its scores at a timestamp it chooses.

**Cost.** Metrics appear with log-ingestion latency rather than immediately, and
malformed EMF fails silently — it just looks like a log line.

**Where.** `agent/obs.py:emf_metrics`, `eval/run.py:emit_cloudwatch_scores`.

---

### D-18 — Validate the alert at the edge

**Decision.** The handler rebuilds the alert field by field, coercing and
validating, before anything reaches the model.

**Why.** A missing `service` produces a confident investigation of the wrong
thing, which is far worse than a 400. Validating at the edge also means the
model's context contains only fields we constructed — an unexpected key in the
incoming payload cannot smuggle text into the prompt, because unknown fields are
dropped rather than passed through (`tests/test_handler.py` pins this).

Environment synonyms are collapsed (`production` → `prod`) so one spelling
reaches the fingerprint. Without that, `prod` and `production` are different
fingerprints and the same alarm opens two incidents.

**Cost.** A new alert field requires a code change to reach the model.

**Where.** `agent/handler.py:validate_alert`.

---

### D-19 — Prompt variants differ on exactly one axis

**Decision.** `v1_baseline` and `v2_investigate_first` are assembled from shared
blocks. Only the investigation-policy block differs.

**Why.** The experiment's whole claim is that *this change* produced *that
delta*. If the variants also differed in wording, role framing, or output
format, the delta would be unattributable and the headline number would be
worthless.

`get_system_prompt` raises `KeyError` on an unknown variant rather than falling
back to a default. A silent fallback would be the worst bug in this project:
both arms of the experiment would run the same prompt and report that the
treatment had no effect — which is exactly what a real null result looks like.

`tests/test_dataset.py` asserts every shared block appears verbatim in both.

**Where.** `agent/prompts.py`.

---

## The evaluation harness

### D-20 — Two deterministic metrics, two judged

**Decision.** Tool selection and tool parameter accuracy are computed from the
trace with no model. Groundedness needs a judge. Escalation correctness is
deterministic.

**Why.** Three arguments, in order of importance:

1. **Drift.** A deterministic scorer returns the same number for the same trace
   forever. When it moves, the agent moved. A judge can drift with a model
   update and you would not know.
2. **Cost.** A full sweep scores three of the four metrics for zero tokens.
3. **Latency.** Deterministic scoring is instant, so the suite can run on every
   change rather than nightly.

Only groundedness asks a genuinely subjective question — "is this claim
supported by that evidence" — and only groundedness pays for a judge.

**Cost.** The deterministic metrics measure what was *specified* in the dataset.
An agent could do something reasonable that the dataset did not anticipate and
score badly for it. That is a real limitation of ground-truth trajectories, and
it is why `expected_tools` lists the minimum required rather than the exact set
(D-22).

---

### D-21 — Action tools are excluded from tool selection

**Decision.** `expected_tools` contains only investigation tools. The loader
rejects a dataset that puts an action tool there.

**Why.** Whether the agent called `page_oncall` is metric 4's entire job.
Counting it in metric 1 as well would make two "independent" metrics move
together, so a trigger-happy agent would be penalised twice and a careful one
rewarded twice — inflating the apparent spread between them and hiding that both
numbers were really measuring the same thing.

**Where.** `eval/scorers.py`, `eval/run.py:load_cases`.

---

### D-22 — Over-investigation is a cost signal, not a correctness failure

**Decision.** `exact_match` = every required investigation tool was called, and
no *forbidden* tool was called. Calling an investigation tool the case did not
require is recorded as `extra_calls` and reported next to token counts.

**Why.** All three investigation tools are read-only and cost one DynamoDB read
or one CloudWatch query. An agent that checks the deploy history on a case where
the metrics alone settle it has not made a mistake — it has spent a few hundred
tokens. Grading that as incorrect would push the agent towards investigating
*less*, which is the exact failure this whole project exists to measure. It
would be an eval that rewards the behaviour it is meant to catch.

Under-investigation *is* a correctness failure and `exact_match` catches it.
`forbidden_tools` remains available per case for a call that would be genuinely
wrong rather than merely unnecessary; no case currently uses it.

**Cost.** Precision on metric 1 is structurally lenient. It is still reported,
for diagnosis, but `exact_match` is the headline and it is recall-shaped.

---

### D-23 — Parameter accuracy is conditional on selection

**Decision.** A tool that was never called contributes nothing to metric 2.
Coverage is reported separately.

**Why.** If an uncalled tool scored 0 on its parameters, one mistake (not
calling it) would be counted twice, and metric 2 would become a noisy
restatement of metric 1. Reporting coverage alongside means a high parameter
score on two calls cannot be mistaken for a high score on ten.

Where a tool was called more than once — legitimately, after a validation error,
or when widening a window on a second look — the best attempt is scored. Grading
the first would punish an agent for recovering from an error.

**Where.** `eval/scorers.py:score_tool_parameters`.

---

### D-24 — Undefined ratios are `None`, never `0.0`

**Decision.** Precision with no positive predictions is `None`. The report
prints `n/a`. `emit_cloudwatch_scores` omits the metric entirely.

**Why.** A run over only the noise bucket makes no positive predictions, so
precision is undefined. Reporting it as `0.00` would make a perfect run — every
case correctly silent — look like total failure, and would trip the quality
alarm on a run that had nothing wrong with it.

**Where.** `eval/scorers.py:_safe_div`, `eval/report.py:fmt`.

---

### D-25 — Escalation as precision and recall, with stated asymmetric weights

**Decision.** Report precision, recall, F1, false-page rate, missed-page rate,
and a weighted cost with `miss = 5.0`, `false_page = 1.0`. Never a single
accuracy number.

**Why.** The two errors have different costs. A missed page means an outage runs
longer. A false page burns an engineer's night and, repeated, causes the alert
fatigue that makes every future page less likely to be acted on — you degrade
the system by over-paging, not only by under-paging. A single accuracy figure
averages those together and hides the thing that matters.

False-page rate is `FP / (FP + TN)` — of the alerts that should *not* have
paged, what fraction did. It is deliberately not `1 - precision`: precision moves
with how many true pages there were, false-page rate does not. It is the number
that predicts alert fatigue, and it is the one the alarm is on.

The 5:1 weight is a judgement, not a measurement. It is exposed as a CLI flag
(`--miss-weight`) precisely so it can be argued with. What matters is that the
weights are *stated* rather than buried inside an accuracy number.

**Where.** `eval/scorers.py:aggregate_escalation`.

---

### D-26 — Two groundedness judges, and report their agreement

**Decision.** A deterministic heuristic judge and an LLM judge run together. The
LLM score is the headline; the heuristic travels beside it; the agreement rate
between them is reported.

**Why.** They catch different things. The heuristic answers one narrow question
extremely well and for free: *did the agent state a number no tool gave it?*
Every numeric literal in the reasoning is checked against the numbers in the
tool results and the alert. The LLM judge catches what the heuristic cannot — an
unsupported causal claim, a paraphrased runbook step that was never returned, a
confident narrative built on one datapoint.

The agreement rate is the point of running both. It is the closest thing
available to a validity check on the LLM judge. **A judge nobody has checked is
just a second opinion with better formatting.** A run where agreement collapses
is a run where the judge needs looking at before its score is believed.

**Calibration.** The heuristic has to fail in neither direction, and getting
there took two corrections found by following `docs/TESTING_GUIDE.md`:

- It originally flagged *correct arithmetic* — "CPU is 14 points over the 80
  threshold" — as hallucinated, because 14 appears in no tool output. A judge
  that calls correct subtraction a fabrication is worse than no judge, so
  differences and ratios of the **salient** figures (alert value, threshold,
  duration, and each metric summary) now count as grounded.
- The first version of that fix also allowed percentage changes, which cost the
  judge its teeth: `(80 - 40.1) / 40.1` is 99.5, close enough to ground an
  invented "pinned at 99.7%". Percentage derivations spread densely across
  0–200, which is exactly where fabricated percentages live. Removed.
  Derivations are also taken only from the dozen salient figures, never from the
  hundreds of raw datapoints, and ratios only in the larger-over-smaller
  direction — sub-1 ratios cluster tightly enough to ground any small number.

The two numbers that say the calibration is still in its window: `hallucinator`
must score below ~0.1 and `good` must score 1.000. Both are asserted in
`tests/test_harness_e2e.py`.

**Cost.** Two judges to maintain. Two known false positives, both accepted: a
percentage-change phrasing now reads as ungrounded, and reasoning that cites no
numbers at all scores as grounded. The second is documented rather than fixed —
penalising unquantified reasoning would conflate "vague" with "fabricated", and
those need different responses. The LLM judge covers both gaps.

**Where.** `eval/judge.py`.

---

### D-27 — The judge never sees the expected answer

**Decision.** Neither judge is shown `expected_page` or `reference_reasoning`.

**Why.** A judge told the right answer rationalises towards it. Grading
groundedness has to be independent of whether the decision was correct: reasoning
that argues correctly from the evidence to a decision you disagree with is still
grounded, and reasoning that reaches the right answer using an invented number is
not. `tests/test_judge.py` asserts neither field appears in the judge prompt.

---

### D-28 — The dataset is the world

**Decision.** Each case in `alerts.jsonl` carries a `scenario` block describing
its metric shapes and deploy history. `eval/world.py` turns that into concrete
data, and exactly two callers use it: `infra/seed_data.py` (real AWS) and
`eval/backends.py` (moto).

**Why.** One source of truth. Because both paths call the same functions, an
offline sweep and an against-AWS sweep reason over identical data, which is what
makes the two scores comparable. If the offline harness invented its own
fixtures, an offline number would say nothing about the deployed agent and
having two modes would be pointless.

Series are generated from a seed derived from `case_id + metric`, so a case
generates byte-identical metrics tomorrow. Non-reproducible fixtures make a
regression indistinguishable from a reroll.

**Cost.** The dataset file is larger and denser than a plain list of alerts.

---

### D-29 — Offline mode is moto; only Bedrock is faked

**Decision.** `--mode offline` runs real `boto3` calls against in-process moto
backends. Only Bedrock is scripted.

**Why.** moto evaluates real `ConditionExpression`s, so the dedupe — the one
piece of genuinely subtle DynamoDB logic here — is tested against the same
semantics production gets. A hand-rolled dict mock would have to reimplement
condition evaluation, at which point the test is checking the mock.

It also caught a real bug: CloudWatch treats `EndTime` as **exclusive**, so a
datapoint seeded at exactly `now` is never returned and the second-newest value
silently becomes "latest". That inverted every `single_spike` and `flapping`
case and produced four false pages that were a fixture bug rather than agent
behaviour. A mock that returned whatever was seeded would have hidden it.

**Cost.** moto is not AWS. It lags on newer API behaviour and its error messages
differ. Anything load-bearing gets confirmed against real AWS before it is
believed — see the testing guide's AWS section.

---

### D-30 — Scripted policies include deliberately broken agents

**Decision.** `eval/fake_bedrock.py` ships `trigger_happy`, `lazy`,
`hallucinator`, `bad_params`, `never_stops`, `no_json`, and
`phantom_incident` alongside `good`.

**Why.** A harness that has only ever seen a well-behaved agent has never
demonstrated it can detect a badly-behaved one — it might be returning 1.0 for
structural reasons. Running each broken agent through the whole pipeline and
asserting that the *right metric* catches it is what makes the numbers mean
anything:

| Policy | tool_sel | params | grounded | false-page | recall |
|---|---|---|---|---|---|
| `good` | 1.000 | 0.993 | 1.000 | 0.053 | 1.000 |
| `trigger_happy` | 0.000 | n/a | 1.000 | **1.000** | 1.000 |
| `lazy` | 0.000 | n/a | 1.000 | 0.000 | **0.000** |
| `hallucinator` | 1.000 | 0.993 | **0.020** | 0.053 | 1.000 |
| `bad_params` | 1.000 | **0.507** | 1.000 | 0.000 | 0.000 |

`hallucinator` is the row that justifies paying for metric 3: everything else
looks healthy and the reasoning is fiction. `bad_params` justifies metric 2:
right tools, wrong arguments, invisible to metric 1. And the `trigger_happy` /
`lazy` pair shows why neither escalation number is safe alone — one has perfect
recall, the other a perfect false-page rate.

**Where.** `tests/test_harness_e2e.py:TestDiscrimination`.

---

### D-31 — Two result files per run

**Decision.** A full payload with every tool call and datapoint (~800KB per
38-case run, git-ignored) and a `.summary.json` with the scores and no traces
(~75KB, committed).

**Why.** They have different jobs. The full payload is the audit record: when a
score looks wrong, it is the only thing that can settle why. An eval you cannot
audit after the fact is an eval you cannot trust. The summary is what belongs in
git, so a score from three weeks ago is still there to compare against without
carrying megabytes of metric series in the history.

Run ids carry a random suffix as well as a timestamp. An offline sweep takes
about two seconds, so running the baseline and the treatment back to back lands
them in the same second — after which the results loader, which deduplicates by
run id, silently drops one of the two runs being compared.

---

### D-32 — 38 cases, four buckets, balanced 19/19

**Decision.** 10 clear incidents, 9 needing investigation, 9 multi-step
correlation, 10 noise. 19 expect a page, 19 do not.

**Why.** The balance matters: a lopsided set lets a constant predictor look good
on one metric, and makes precision and recall hard to read against each other.
The noise bucket is the one almost nobody builds and the one the most
interesting number comes from — its correct behaviour is to investigate and then
stay quiet.

**Cost.** 38 cases is a small sample and the confidence intervals are wide. One
case moves the false-page rate by 0.053. The comparison report prints that
caveat automatically next to every headline, so the number cannot be quoted
without it.

---

### D-33 — No two cases share a (service, metric) pair

**Decision.** Enforced by `tests/test_dataset.py`.

**Why.** CloudWatch keys a series by namespace, metric name, and dimensions. Two
cases sharing a `(service, metric)` pair would write into the same series, so
each would score against data meant for the other — and the failure would look
like a mysterious agent regression rather than a dataset collision. This
constraint is also what makes a full sweep against real AWS possible without
per-case reseeding.

---

## Operations and security

### D-34 — IAM scoped to specific ARNs, checked into the repo

**Decision.** `infra/iam-policies/` holds the real policies with `${...}`
placeholders substituted at apply time. No wildcard actions anywhere.

**Why.** Checking in the policies is only worth anything if they are correct, so
tests assert they are: no `Action: "*"`, no `service:*`, every ARN scoped to the
account, and `Resource: "*"` only where AWS genuinely has no resource-level
permissions — CloudWatch metric reads and the ECR auth token — justified by the
statement's own `Sid`, since JSON has no comments.

Two details worth having got right:

- **Bedrock needs both** the inference-profile ARN in your account *and* the
  foundation-model ARN in every region the profile can route to. Granting only
  the profile is the most common Bedrock `AccessDenied` and the error does not
  tell you that.
- A **`cloudwatch:namespace` condition was removed** from the metric-read
  statement. An unsupported condition key makes the statement match nothing, so
  a policy that looked tighter would have denied every metric read.

The policy is inline on the role rather than managed: it is meaningless outside
this role, and inline means it cannot be left attached to something else after
teardown.

**Where.** `infra/iam-policies/`, `tests/test_infra.py`.

---

### D-35 — A shared-secret API key, and saying so

**Decision.** An optional `x-api-key` header compared with `hmac.compare_digest`.

**Why.** It is the right amount of auth for a portfolio project and explicitly
the wrong amount for production. `compare_digest` rather than `==` so the
comparison does not leak the key's prefix through response timing — cheap, and
getting it wrong is the kind of thing worth not getting wrong.

In production this endpoint would sit behind an API Gateway JWT authorizer, or
be invoked over EventBridge with no public surface at all. Saying that in the
README is worth as much as building it, and pretending otherwise would be worse
than either.

**Cost.** One secret, no rotation, no per-caller identity, no rate limiting.

---

### D-36 — Pages are suppressed by default in the harness

**Decision.** `Config.dry_run` defaults to true in the eval runner. `--send-pages`
opts in.

**Why.** A 38-case sweep would otherwise send 19 real emails every run — which
is both absurd and precisely the alert fatigue this project exists to measure.
The page is still recorded in the trace, so escalation scoring is unaffected.

---

### D-37 — Log retention and DynamoDB TTL

**Decision.** 14-day CloudWatch log retention; `ttl` on incidents (30 days) and
eval results (90 days).

**Why.** Both default to forever, and both are slow silent bills. TTL deletion
is free; a lifecycle policy you have to remember to run is not.

---

### D-38 — Quality alarms, with `treat-missing-data missing`

**Decision.** Four alarms: two operational (Lambda errors, tool failures) and
two on agent quality (tool selection accuracy < 0.85, false-page rate > 0.10).
The quality alarms use `treat-missing-data missing`.

**Why.** Once the eval scores are CloudWatch metrics, an alarm on them fires
exactly like an alarm on p99 latency — **a quality regression pages you the same
way a latency regression does.** That reframes agent quality as an operational
metric rather than a vibe, which is the whole point of publishing the scores.

`missing` rather than `notBreaching` because a suite that stopped running should
not look like a suite that is passing. The alarm sits in `INSUFFICIENT_DATA`,
which is visible; `notBreaching` would show green forever after the last run.

The `FailedToolCalls` alarm covers a specific and nasty failure: the agent is
running fine, its tools are failing (an expired permission, a deleted table), and
it still reaches a decision — on no evidence.

---

### D-39 — Metric dimensions are low-cardinality only

**Decision.** Agent metrics are dimensioned by `Service` and `Environment`. Eval
metrics by `PromptVariant` and `Suite`. `alert_id`, `correlation_id`, and
`run_id` are plain log fields.

**Why.** Every distinct dimension combination is a separately billed custom
metric at ~$0.30/month. An `alert_id` dimension is a real way to turn a
$0.30/month bill into a $300 one. The unbounded fields are still queryable in
Logs Insights, which is where you actually want them.

---

### D-40 — Structured single-line JSON logs

**Decision.** One JSON object per line on stdout, with a `correlation_id` on
every line.

**Why.** Lambda ships stdout to CloudWatch Logs for free, and single-line JSON
is what makes Logs Insights able to query fields directly. Multi-line pretty
JSON breaks Insights because each line becomes its own event.

The correlation id is the API Gateway request id where one exists, so a log line
can be joined to an access log entry.

Logging is wrapped so a serialisation failure can never take down a request. An
observability failure that causes an outage is a self-inflicted wound.

---

### D-41 — Lambda at 512MB with a 120-second timeout

**Decision.** 512MB, 120s.

**Why.** Memory is not the constraint — Lambda scales CPU proportionally with
memory, and at 128MB the boto3 import and JSON handling alone add seconds to
every cold start. 512MB is roughly the knee of that curve for this workload.
120s because a Converse loop with five tool calls can genuinely take a minute,
and a timeout that fires mid-investigation loses everything.

**Cost.** Higher per-millisecond price. At this invocation volume it is noise,
and the shorter duration partly offsets it.

---

### D-42 — Only `agent/` ships in the deployment package

**Decision.** `function.zip` contains the `agent` package and nothing else.

**Why.** boto3 and botocore are already in the Lambda Python runtime. Bundling
them adds ~15MB to the artifact and slows every cold start for no benefit. The
zip is built with Python's `zipfile` rather than the `zip` command so the build
works identically on Windows, where `zip` is often absent.

**Cost.** The runtime's boto3 version is AWS's choice, not ours. If a feature
needed a newer botocore, this would have to change — a layer, or bundling.

---

## Decisions deliberately deferred

Things considered and consciously not done, so that "not built" is
distinguishable from "not thought about".

| Not built | Why not | What it would cost to add |
|---|---|---|
| ECS Fargate eval runner | Optional in the plan; the sweep runs locally today (D-03) | Task definition, image, VPC. IAM already written. |
| AgentCore Runtime deploy | Same | A day, and it would replace the Lambda rather than add to it |
| A frontend | Nobody grades a UI and it eats a day | — |
| Multi-turn conversation | One alert, one investigation. Out of scope by design | Session state in DynamoDB, and every metric here would need redefining per turn |
| RAG / Bedrock Knowledge Base | The runbook corpus is ten entries (D-08) | A vector store, an embedding pipeline, ongoing cost, non-deterministic retrieval |
| JWT authorizer | An API key is honest about what this is (D-35) | An hour, plus an identity provider |
| Real PagerDuty | SNS proves the integration (D-05) | A day of webhook work demonstrating nothing new |
| Concurrency in the sweep | Bedrock throttling would need careful backoff; the sweep is not slow enough to need it | A thread pool and a shared rate limiter |
| Statistical significance testing | 38 cases will not support it, and reporting a p-value on this sample would be worse than reporting none | A much larger dataset first |
| Human-labelled groundedness | No second annotator available | The right next step, and the honest fix for D-26's validity gap |
