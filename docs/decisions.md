# Architecture decision records

Five decisions, one paragraph each. These are the ones an interviewer is most
likely to ask about, and the ones where the alternative was genuinely arguable.

The exhaustive version — 42 decisions with rejected alternatives and costs — is
[`DESIGN_DECISIONS.md`](DESIGN_DECISIONS.md). How to attack any of them is
[`TESTING_GUIDE.md`](TESTING_GUIDE.md).

---

## ADR-1 — DynamoDB rather than RDS

Every access pattern in this system is a key lookup or a key-range scan: recent
deploys for one service, all runbooks, is there an open incident for this alarm,
the results of one eval run. Nothing here needs a join, an aggregate, or an
ad-hoc query — it needs single-digit-millisecond point reads from a Lambda that
may be cold, and it needs a conditional write as a primitive. DynamoDB gives
both, and on-demand billing means the idle cost is storage alone. Postgres is
better at exactly the things this workload never does, and would bring a VPC,
connection pooling from Lambda (a real problem, not a theoretical one), an
instance billing hourly whether or not anything queries it, and patching. The
cost of the choice is real: the access patterns had to be designed before the
schema, and adding an unanticipated one later means a GSI or a migration.

Full detail: [D-04](DESIGN_DECISIONS.md#d-04--dynamodb-not-rds),
[D-06](DESIGN_DECISIONS.md#d-06--single-table-design).

**Key layout** — one table, composite `PK`/`SK`, no secondary indexes:

| Entity | PK | SK |
|---|---|---|
| Runbook | `RUNBOOK` | `SYMPTOM#<runbook_id>` |
| Deploy | `DEPLOY#<service>` | `<iso8601 timestamp>` |
| Incident (live, deduped) | `INCIDENT#<fingerprint>` | `ACTIVE` |
| Incident (immutable audit) | `INCIDENT#<incident_id>` | `META` |
| Eval run summary | `RUN#<run_id>` | `SUMMARY` |
| Eval case result | `RUN#<run_id>` | `CASE#<case_id>` |

---

## ADR-2 — ECS Fargate for the batch eval runner

The agent is a Lambda because alerts are bursty, independent and idle most of
the time — exactly Lambda's shape. The eval runner is not, and it cannot be: a
sweep with a real model makes roughly six Converse calls per case across 38
cases, plus a judge call each, at 5–40 seconds per call. That is comfortably
past Lambda's 15-minute ceiling even with concurrency. This is a real constraint
producing a real decision rather than a preference: Fargate has no execution
limit, per-second billing, and the task runs rarely. Step Functions fanning out
to Lambdas would also work and adds a state machine plus per-case cold starts to
a job that is fundamentally one long-running process. The cost is a container
image, a VPC, and two IAM roles instead of one — task role versus task execution
role, which are genuinely different things: one is what the container may do,
the other is what ECS may do on its behalf to start it.

Status: designed and IAM'd, not deployed. The sweep runs locally today with
`python -m eval.run --mode aws`. Full detail:
[D-03](DESIGN_DECISIONS.md#d-03--ecs-fargate-for-the-batch-eval-runner).

---

## ADR-3 — Two deterministic metrics, two judged

Tool selection and tool parameter accuracy are computed from the trace with no
model involved. Groundedness needs a judge because "is this claim supported by
that evidence" is genuinely subjective. Escalation correctness is deterministic
again. The split is a cost argument — a full sweep scores three of four metrics
for zero tokens — but the stronger argument is drift: a deterministic scorer
returns the same number for the same trace forever, so when it moves, the agent
moved. A judge can drift silently with a model update and you would not know. It
is also a latency argument: deterministic scoring is instant, so the suite runs
on every change rather than nightly. The cost is that the deterministic metrics
measure what the dataset *specified*; an agent could do something reasonable
that the dataset did not anticipate and score badly for it. That is the real
limitation of ground-truth trajectories, and it is why `expected_tools` lists
the minimum required investigation rather than an exact set.

Where a judge *is* used, two run together — a deterministic heuristic that
catches invented figures for free, and an LLM judge that catches unsupported
causal claims — and their agreement rate is reported. A judge nobody has checked
is just a second opinion with better formatting.

Full detail: [D-20](DESIGN_DECISIONS.md#d-20--two-deterministic-metrics-two-judged),
[D-26](DESIGN_DECISIONS.md#d-26--two-groundedness-judges-and-report-their-agreement).

---

## ADR-4 — A conditional write, not a transaction

Deduplicating an alert storm needs exactly one item to be consistent: the live
incident for a given alarm fingerprint. `create_incident` does a single
`PutItem` conditioned on `attribute_not_exists(PK) OR opened_at_epoch < :cutoff`,
which is atomic at the item level — all the atomicity this problem has — for one
WCU and one round trip. `TransactWriteItems` would cost twice the write units
and add a second failure mode to interpret, to protect a multi-item invariant
that does not exist. A read-then-write is the classic race: two Lambdas both
read "no incident" and both write.

The condition is a genuine sliding window rather than a time bucket, and that
distinction is the whole point. With fixed buckets, an alert at 14:59 and
another at 15:01 land in different buckets and both open an incident — the storm
gets through the dedupe precisely at the boundary. Comparing against when the
incumbent was opened holds regardless of where the clock is.

One subtlety worth knowing: the incumbent item returned by
`ReturnValuesOnConditionCheckFailure` arrives in raw DynamoDB wire format even
through the resource layer, because that layer deserialises successful responses
and not exceptions. Reading it directly yields a dict where a string is
expected, and the dedupe silently returns an unusable incident id.

Full detail: [D-07](DESIGN_DECISIONS.md#d-07--dedupe-with-a-conditional-write-not-a-transaction).

---

## ADR-5 — What I would change with more time

**Validate the LLM judge against human labels.** The agreement rate between the
two judges is a consistency check, not a correctness one — they could agree and
both be wrong. Hand-labelling 40 traces for groundedness and measuring the
judge's agreement with a person is the honest fix, and it is the largest gap in
the harness as it stands.

**More cases, and real ones.** 38 cases means one case moves the false-page rate
by 0.053, so any comparison whose delta is under about 0.1 is inside the noise.
The comparison report prints that caveat automatically for exactly that reason.
Synthetic alerts are also cleaner than real ones, which arrive in bursts and
correlate with each other.

**Measure variance.** Everything runs at temperature 0 so the deterministic
scorers measure the prompt rather than sampling noise. That is right for the
experiment and wrong as a model of production, which would run non-zero. Running
each arm five times at production temperature and reporting the spread would say
something the current numbers cannot.

**Then the deferred infrastructure** — the Fargate runner (ADR-2), an AgentCore
Runtime deploy for comparison, and a JWT authorizer in place of the shared-secret
API key. All three are known quantities; none of them would change what the
project demonstrates, which is why they came last.
