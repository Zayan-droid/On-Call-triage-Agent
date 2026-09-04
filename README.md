# On-Call Triage Agent + Evaluation Harness

[![CI](https://github.com/Zayan-droid/On-Call-triage-Agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Zayan-droid/On-Call-triage-Agent/actions/workflows/ci.yml)

A tool-using agent on Amazon Bedrock that investigates infrastructure alerts —
querying CloudWatch metrics, deploy history and runbooks — and decides whether to
open an incident, page the on-call engineer, or stay quiet. Alongside it, a
harness that replays 38 alerts through the agent and scores whether it
investigated properly, grounded its reasoning in real data, and escalated
correctly.

**The agent is the vehicle. The harness is the point.** Building an agent with
tool use is table stakes. Being able to answer *"how do you know it works?"* with
a number is not.

---

## Architecture

![Architecture: an alert POSTs to API Gateway, which invokes a Lambda running a Bedrock Converse tool-use loop. The loop calls CloudWatch for metrics, DynamoDB for deploys, runbooks and incidents, and SNS to page. The same agent package also runs as a container on Bedrock AgentCore Runtime, reached by SigV4 with no public URL. An evaluation harness replays 38 alerts through the same loop, writes results to DynamoDB, and publishes scores as CloudWatch metrics that alarms watch; a full sweep runs as an ECS Fargate task because it outlives Lambda's fifteen-minute ceiling.](docs/architecture.svg)

Every arrow has a reason — if an interviewer asks "why ECS for the eval runner?",
the answer is that a sweep over 38 alerts takes longer than Lambda's 15-minute
ceiling. `docs/decisions.md` covers the six most arguable decisions;
`docs/DESIGN_DECISIONS.md` covers all 46.

**The agent is deployed two ways, and they fail differently.** A Lambda behind
API Gateway is the right shape for an alerting webhook: public HTTPS, scales to
zero, guarded by a shared secret this project openly calls insufficient. The
same agent also runs as a container on Bedrock AgentCore Runtime, which has no
public URL at all — callers use SigV4 and need
`bedrock-agentcore:InvokeAgentRuntime`. That removes the weakest part of the
other deployment and replaces "curl this URL" with "assume a role first".
Neither dominates, so both stay. The second target costs one module:
`agent/server.py` translates HTTP into the event shape the Lambda handler
already accepts, so auth, validation, the Converse loop and the five tools are
the *same* code on both paths and cannot drift
([ADR-5](docs/decisions.md#adr-5--agentcore-runtime-as-a-second-deployment-target-not-a-replacement)).

### The agent's five tools

| Tool | Does | Why it exists |
|---|---|---|
| `get_service_metrics(service, metric, window_minutes)` | Reads real CloudWatch statistics | An alarm says a threshold was crossed once; the history says whether it *still* is |
| `get_recent_deploys(service, hours)` | DynamoDB range query | Most incidents correlate with a release |
| `search_runbook(symptom)` | DynamoDB query, ranked in Python | The only approved source of remediation — cite it, never invent it |
| `create_incident(severity, summary)` | **Conditional** DynamoDB write | Dedupes alert storms on a sliding 15-minute window |
| `page_oncall(incident_id, reason)` | SNS publish | The high-stakes action. Everything in the eval revolves around whether it fires correctly |

The first three are read-only *investigation* tools; the last two are
*action* tools. That split is what keeps the eval metrics independent — see
[D-09](docs/DESIGN_DECISIONS.md#d-09--five-tools-split-into-investigation-and-action).

---

## Results

> **These numbers are from offline mode, which scripts the model.** They measure
> the harness, the tools and the dataset — not a model's judgement. The
> against-Bedrock numbers go here once run; the command is in
> [Run it yourself](#run-it-yourself). Reporting a scripted number as a model
> result would be the exact dishonesty this project is built to avoid.

38 cases · scripted reference policy · heuristic judge · `v2_investigate_first`

| Metric | Score |
|---|---|
| **1. Tool selection** (exact match) | 1.000 |
| **2. Tool parameter accuracy** | 0.993 |
| **3. Groundedness** (heuristic judge) | 1.000 |
| **4. Escalation precision** | 0.950 |
| **4. Escalation recall** | 1.000 |
| **4. False-page rate** | 0.053 |
| **4. Missed-page rate** | 0.000 |
| Weighted cost (miss ×5, false page ×1) | 0.026 |
| Confusion TP/FP/TN/FN | 19 / 1 / 18 / 0 |
| Decision self-report consistency | 1.000 |

| Bucket | n | Tool sel. | Params | TP/FP/TN/FN | False-page |
|---|---|---|---|---|---|
| clear_incident | 10 | 1.000 | 1.000 | 10/0/0/0 | n/a |
| needs_investigation | 9 | 1.000 | 1.000 | 3/1/5/0 | 0.167 |
| multi_step_correlation | 9 | 1.000 | 1.000 | 6/0/3/0 | 0.000 |
| noise | 10 | 1.000 | 0.975 | 0/0/10/0 | 0.000 |

The single false page is `investigate_007` — Lambda concurrency throttling where
the correct answer is written in the runbook (async invocations retry; only
synchronous throttling warrants a page). The reference policy reasons from metric
shape alone and cannot get there. **A fixture scoring 1.000 on everything would
be the suspicious result**, because it would mean the dataset contained nothing
it could not trivially solve.

### The experiment

One thing changed: the system prompt. `v1_baseline` says "investigate the alert
and decide whether to page". `v2_investigate_first` adds an explicit instruction
to gather metrics before escalating, and explicit permission to stay quiet.
Everything else in the two prompts is byte-identical
([D-19](docs/DESIGN_DECISIONS.md#d-19--prompt-variants-differ-on-exactly-one-axis)).

| Metric | v1_baseline | v2_investigate_first | Δ |
|---|---|---|---|
| Tool selection (exact) | 0.947 | 1.000 | +0.053 |
| Escalation precision | 0.792 | 0.950 | +0.158 |
| Escalation recall | 1.000 | 1.000 | 0.000 |
| **False-page rate** | **0.263** | **0.053** | **−0.211** |
| Weighted cost | 0.132 | 0.026 | −0.105 |
| Total output tokens | 15,120 | 17,220 | +2,100 |

> Adding an explicit instruction to gather metrics before escalating cut the
> false-page rate from 0.26 to 0.05 with no loss in recall, across 38 cases.

Four cases changed: `noise_001`, `noise_002`, `noise_006`, `noise_007` — three
non-production alarms and a traffic spike with no error impact. All four went
`FP → TN`. **A delta with no cases behind it is a harness bug, not a result**, so
the comparison report always names them.

Caveat, printed automatically beside the headline: 38 cases is a small sample.
One case moves the false-page rate by 0.053, so any delta under about 0.1 is
inside the noise. This one is not, but it is not far outside either.

### Proof the harness discriminates

An eval that has only ever seen a good agent has never shown it can detect a bad
one. Each deliberately-broken policy is caught by the metric that should catch
it, and by no other:

| Policy | Tool sel. | Params | Grounded | False-page | Recall |
|---|---|---|---|---|---|
| `good` | 1.000 | 0.993 | 1.000 | 0.053 | 1.000 |
| `trigger_happy` — escalates without investigating | **0.000** | n/a | 1.000 | **1.000** | 1.000 |
| `lazy` — calls nothing, dismisses everything | **0.000** | n/a | 1.000 | 0.000 | **0.000** |
| `hallucinator` — investigates, then invents numbers | 1.000 | 0.993 | **0.066** | 0.053 | 1.000 |
| `bad_params` — right tools, wrong arguments | 1.000 | **0.507** | 1.000 | 0.000 | 0.000 |

`hallucinator` is the row that justifies paying for a judge: every other number
looks healthy and the reasoning is fiction. `bad_params` justifies metric 2 —
invisible to metric 1. And `trigger_happy` versus `lazy` is why escalation is
never reported as one number: one has perfect recall, the other a perfect
false-page rate, and both are useless.

Asserted in `tests/test_harness_e2e.py::TestDiscrimination`.

---

## Design decisions

Three that shaped everything else:

- **The decision is read from side effects, never from the model's self-report.**
  `paged` is true because a `page_oncall` call succeeded, not because the model
  wrote `"decision": "PAGE"`. Models misreport their own behaviour; an eval that
  scores self-reports measures honesty rather than judgement. The gap between
  claim and action is captured separately, and it catches a real failure — the
  agent that says it escalated but whose page the guardrail blocked.
  → [D-11](docs/DESIGN_DECISIONS.md#d-11--the-decision-is-read-from-side-effects-never-from-the-models-report)

- **Escalation is precision and recall, never one accuracy number.** A missed
  page means an outage runs longer; a false page burns an engineer's night and,
  repeated, causes the alert fatigue that makes every future page less effective.
  Those costs are not equal, so the weighted cost prices a miss at 5× a false
  page — a judgement exposed as a CLI flag precisely so it can be argued with.
  → [D-25](docs/DESIGN_DECISIONS.md#d-25--escalation-as-precision-and-recall-with-stated-asymmetric-weights)

- **Two of four metrics need no model at all.** Deterministic scorers are cheap
  and fast, but the real argument is drift: they return the same number for the
  same trace forever, so when one moves, the agent moved. A judge can drift with
  a model update and you would never know. Only groundedness asks a genuinely
  subjective question, and only groundedness pays for a judge.
  → [ADR-3](docs/decisions.md#adr-3--two-deterministic-metrics-two-judged)

Full set: [`docs/decisions.md`](docs/decisions.md) (6 ADRs) ·
[`docs/DESIGN_DECISIONS.md`](docs/DESIGN_DECISIONS.md) (all 46, with rejected
alternatives and costs).

---

## Known limitations

Specific, because vague limitations are not limitations.

- **Synthetic alerts.** All 38 cases are hand-authored. Real alerts are messier,
  arrive in bursts, and correlate with each other.
- **38 cases is too small for tight confidence intervals.** One case moves the
  false-page rate by 0.053. No significance testing is reported because this
  sample would not support it, and a p-value here would be worse than none.
- **Single-turn.** One alert, one investigation, done. No follow-up, no human in
  the loop.
- **The LLM judge has never been validated against human labels.** The agreement
  rate between the two judges is a consistency check, not a correctness one —
  they could agree and both be wrong. This is the largest gap in the harness.
- **The heuristic judge scores numberless reasoning as grounded.** Documented,
  not fixed: penalising unquantified reasoning would conflate "vague" with
  "fabricated", and those need different responses.
- **Temperature 0 everywhere,** so the deterministic scorers measure the prompt
  rather than sampling noise. Right for the experiment, wrong as a model of
  production, which would run non-zero. Nothing here measures that variance.
- **`expected_tools` encodes one person's view of the minimum investigation.** An
  agent doing something reasonable but unanticipated scores badly for it. That is
  the real limitation of ground-truth trajectories.
- **Auth is a shared-secret API key.** In production this would sit behind an API
  Gateway JWT authorizer, or be invoked over EventBridge with no public surface.
- **Offline mode scripts the model,** so offline numbers measure the harness and
  the tools, not a model's judgement. Every offline report says so in its header.
- **One deploy step has never been run against a live account:**
  `create-agent-runtime`. The AgentCore image is built and health-checked in CI
  on every commit, and the role, the server contract and the teardown are
  tested — but the API call that registers the runtime is unverified, and the
  step says so rather than pretending otherwise. Everything else here has run.
- **The Fargate sweep assigns a public IP** instead of routing through a NAT
  gateway, because a NAT gateway bills ~$32/month whether or not a sweep runs.
  The task has no inbound rules, but VPC endpoints would be the production
  answer.

---

## Run it yourself

Everything except the deployment sections runs **offline** — no AWS account, no
credentials, no cost. If you want the guided version,
[`docs/DEMO.md`](docs/DEMO.md) is a ten-minute walkthrough with the expected
output quoted for every command.

```bash
python -m venv .venv && .venv/Scripts/activate    # or: source .venv/bin/activate
pip install -r requirements-dev.txt
```

### The test suite — 440 tests, 93% coverage, ~90 seconds

```bash
pytest
```

Lint and shell-lint the way CI does:

```bash
ruff check . && shellcheck --severity=warning infra/setup.sh infra/teardown.sh
```

### A full evaluation sweep, offline

```bash
python -m eval.run
```

### Reproduce the experiment

```bash
python -m eval.run --policy prompt_sensitive --prompt-variant v1_baseline --tag baseline --quiet
```

```bash
python -m eval.run --policy prompt_sensitive --prompt-variant v2_investigate_first --tag treatment --quiet
```

```bash
python -m eval.report --compare baseline treatment
```

### Watch the harness catch a bad agent

```bash
python -m eval.run --policy hallucinator --tag probe
```

Everything stays healthy except groundedness, which collapses to ~0.07.

### Against real AWS

Each step is idempotent and can be run alone. `preflight` checks credentials,
tooling, and Bedrock model access before anything is created.

```bash
cp infra/.env.example infra/.env    # then edit AWS_REGION and PAGER_EMAIL
bash infra/setup.sh preflight
```

```bash
bash infra/setup.sh all
```

`bash infra/setup.sh` with no arguments lists every step and what it creates.
It never acts on an empty argument list — a setup script that did would be one
stray Enter away from a surprise bill.

Then the real experiment — this is where the numbers for the results table above
come from:

```bash
python -m eval.run --mode aws --prompt-variant v1_baseline --tag aws-baseline --judge both --write-dynamo --emit-metrics
```

```bash
python -m eval.run --mode aws --prompt-variant v2_investigate_first --tag aws-treatment --judge both --write-dynamo --emit-metrics
```

```bash
python -m eval.report --compare aws-baseline aws-treatment --markdown
```

### The eval runner on ECS Fargate

Why it exists: a 38-case sweep against real Bedrock takes 15–25 minutes and
Lambda stops at 15. Needs Docker.

```bash
bash infra/setup.sh ecs_all
```

```bash
bash infra/setup.sh evalrun
```

That builds and pushes the image, creates the task execution role, the task
role, the cluster and the log group, registers the task definition, then runs
one sweep as a Fargate task and tails it to completion. Per-case results land in
DynamoDB under `PK=RUN#<run-id>`; the aggregate scores become CloudWatch metrics
that the two quality alarms read.

### The same agent on AgentCore Runtime

Needs Docker with arm64 support (`docker buildx`) — AgentCore accepts
`linux/arm64` images only.

```bash
bash infra/setup.sh agentcore_all
```

That builds and pushes the arm64 image, creates the runtime execution role,
registers the agent runtime, and invokes it once with a real alert. There is no
public URL: the invoke goes through `aws bedrock-agentcore invoke-agent-runtime`
with SigV4.

**Delete every billable resource when you are done:**

```bash
bash infra/teardown.sh all
```

One confirmation, then deletes in dependency order — the runtime and the task
definition go before the image repositories they point at. `teardown.sh metrics`
explains the one thing that cannot be deleted: CloudWatch custom metrics expire
15 months after their last datapoint and not before.

---

## Repository layout

```
├── agent/
│   ├── handler.py      Lambda entrypoint: auth, validation, metrics
│   ├── server.py       the same handler over HTTP, for AgentCore Runtime
│   ├── agent.py        Converse loop, bounded retry, forced-decision fallback
│   ├── tools.py        the five tools, strict validation, full trace
│   ├── prompts.py      v1_baseline / v2_investigate_first, one axis apart
│   ├── config.py       frozen, env-driven
│   ├── aws.py          module-scope boto3 clients
│   └── obs.py          single-line JSON logs + CloudWatch EMF
├── eval/
│   ├── alerts.jsonl    38 cases across 4 buckets — the dataset is the world
│   ├── world.py        scenario → CloudWatch + DynamoDB, one seeder for both modes
│   ├── backends.py     moto offline / real AWS
│   ├── fake_bedrock.py scripted model, including broken agents on purpose
│   ├── scorers.py      the four metrics
│   ├── judge.py        heuristic + LLM judges, with an agreement rate
│   ├── run.py          sweep → results, DynamoDB, CloudWatch scores
│   ├── report.py       tables and the experiment comparison
│   └── results/        run summaries (full traces are git-ignored)
├── infra/
│   ├── iam-policies/           7 policies, tested for tight scoping
│   ├── setup.sh                17 idempotent steps in 3 deploy targets
│   ├── teardown.sh             reverse order, one confirmation
│   ├── seed_data.py            materialise the dataset's world into real AWS
│   ├── .env.example            every knob, with the reason for each default
│   ├── ecs-task-definition.json
│   ├── Dockerfile.eval-runner  the batch sweep (amd64)
│   └── Dockerfile.agentcore    the agent itself (arm64, required)
├── tests/                      440 tests against moto, not hand-rolled mocks
├── .github/workflows/ci.yml    lint, tests, a real sweep, shellcheck, both images
└── docs/
    ├── decisions.md          6 ADRs
    ├── DESIGN_DECISIONS.md   all 46, with rejected alternatives and costs
    ├── TESTING_GUIDE.md      how to break this on purpose
    └── DEMO.md               a ten-minute walkthrough, offline, with expected output
```

---

## Continuous integration

Five jobs, every one of them offline. There are no AWS credentials in this
repository and none are needed — the tests use moto and the harness has an
offline mode with a scripted model. That is deliberate: a pipeline that needs a
live account fails for reasons unrelated to the commit, and one that needs
long-lived keys in secrets is a credential nobody rotates.

| Job | What it proves |
|---|---|
| `ruff` | Lint, against a rule set pinned in `pyproject.toml` rather than whatever the installed version defaults to ([D-46](docs/DESIGN_DECISIONS.md#d-46--lint-configuration-is-pinned-in-the-repository-not-inherited)) |
| `pytest` | 440 tests with a 90% coverage floor |
| `eval harness` | A full 38-case sweep, two broken policies caught, **and the prompt experiment re-run from scratch** — if a refactor changes the measured effect of the prompt, that is the most important regression this project can have, and the README quotes the number |
| `shellcheck` | The deploy scripts parse, lint clean, and print help without touching AWS |
| `docker` | Both images build, the eval runner's imports resolve, and the AgentCore container passes the same `GET /ping` that AgentCore itself polls |

---

## Licence

[MIT](LICENSE).
