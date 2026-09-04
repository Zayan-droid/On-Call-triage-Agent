# Demo runbook

Ten minutes, offline, no AWS account needed. Every command here has been run and
its output is quoted, so you know what should appear before you type it.

**Lead with the harness, not the agent.** Everyone's agent looks the same. The
thing that is hard to build and hard to fake is being able to answer *"how do
you know it works?"* with a number.

---

## Before you start

```bash
pip install -r requirements-dev.txt && pytest -q
```

Expect `440 passed` in about 90 seconds. If that passes, nothing in the offline
demo can fail — it needs no network, no credentials and no AWS.

Have open, in this order:

1. A terminal at the repository root
2. `docs/architecture.svg` (or the README, which embeds it)
3. `eval/alerts.jsonl`, scrolled to the `noise_` cases

Set `PYTHONIOENCODING=utf-8` on Windows if your terminal mangles the report's
box characters.

---

## 1. The framing — 60 seconds, before any command

> "An alert arrives at an HTTP endpoint. The agent investigates — pulls recent
> CloudWatch metrics, checks whether anything deployed lately, searches a
> runbook — and then decides: open an incident, page the on-call engineer, or
> conclude it is noise and stay quiet.
>
> The agent is the vehicle. What I actually care about is the harness that
> scores it. I have 38 test alerts across four categories, and the important
> one is the noise bucket, where the correct behaviour is to investigate and
> then *stay quiet*. That is where agents fail and almost nobody tests it."

Point at the diagram once, and name one arrow with a reason attached — the ECS
Fargate box is the best one: **it exists because a full sweep takes longer than
Lambda's 15-minute ceiling.** A real constraint producing a real decision.

---

## 2. One alert, investigated end to end

```bash
python -m eval.run --case clear_001 --case noise_003
```

Two cases, contrasting on purpose. Read the two result lines out loud:

```
[1/2] clear_001   PAGE            ok   tools=ok ground=1.00
[2/2] noise_003   INCIDENT_ONLY   ok   tools=ok ground=1.00
```

One pages. The other opens an incident record and deliberately does not wake
anyone. Both called the same investigation tools first. That is the whole thesis
in two lines — **the noise case is not correct because it stayed quiet, it is
correct because it investigated and then stayed quiet.** An agent that reached
the same answer without calling a tool scores zero on metric 1, and should.

---

## 3. The full sweep and the four metrics

```bash
python -m eval.run
```

38 cases in about two seconds. What to say while it runs:

> "Two of the four metrics are deterministic — did it call the right tools, with
> the right arguments — so they are cheap, instant, and cannot drift. A judge
> can drift with a model update and you would never know. I only pay for a
> judge where the question is genuinely subjective, which is groundedness:
> is this reasoning citing numbers the tools actually returned, or numbers it
> invented?"

Then the line that matters most, on escalation:

> "I report escalation as precision and recall separately, never one accuracy
> number, because the two failure modes have different costs. A missed page
> means an outage runs longer. A false page burns someone's night, and enough of
> them cause the alert fatigue that makes every future page less effective.
> Those are not interchangeable, so a single accuracy figure would hide the
> thing I care about."

Expected headline: tool selection 1.000, parameters 0.993, escalation precision
0.950, recall 1.000, false-page rate 0.053.

**Do not skip the false page.** It is `investigate_007`, and volunteering it is
worth more than the 0.950:

> "One false page, and I left it in. It is Lambda concurrency throttling, where
> the right answer is in the runbook — async invocations retry, so only
> synchronous throttling warrants a page. The reference policy reasons from
> metric shape alone and cannot get there. A fixture scoring 1.000 on
> everything would be the suspicious result, because it would mean the dataset
> contained nothing it could not trivially solve."

---

## 4. Prove the harness catches a bad agent

This is the strongest 30 seconds in the demo. An eval that has only ever seen a
good agent has never shown it can detect a bad one.

```bash
python -m eval.run --policy hallucinator --tag probe
```

> "That agent investigates properly and then invents its numbers. Every metric
> stays healthy except groundedness, which collapses to about 0.07. That row is
> the entire justification for paying for a judge — nothing else on the
> dashboard would tell you the reasoning is fiction."

If there is time, the pair that explains why escalation is two numbers:

```bash
python -m eval.run --policy trigger_happy --tag probe2 --quiet
```

`trigger_happy` pages everything: perfect recall, false-page rate 1.000.
`lazy` pages nothing: a perfect false-page rate, recall 0.000. Both useless,
and a single accuracy number would flatter both.

---

## 5. The experiment — the number to land on

```bash
python -m eval.run --policy prompt_sensitive --prompt-variant v1_baseline --tag baseline --quiet
```

```bash
python -m eval.run --policy prompt_sensitive --prompt-variant v2_investigate_first --tag treatment --quiet
```

```bash
python -m eval.report --compare baseline treatment
```

> "One thing changed: the system prompt. The second one adds an explicit
> instruction to gather metrics before escalating, and explicit permission to
> stay quiet. Everything else is byte-identical. That cut the false-page rate
> from 0.26 to 0.05 with no loss in recall."

Then, unprompted, the two things that make it credible rather than a slide:

- **The report names the four cases that changed** — three non-production alarms
  and a traffic spike with no error impact, all `FP → TN`. A delta with no cases
  behind it is a harness bug, not a result.
- **It prints its own caveat.** 38 cases is small: one case moves the false-page
  rate by 0.053, so any delta under about 0.1 is inside the noise. This one is
  outside it, but not by much.

Saying the caveat before you are asked is the difference between a measurement
and a claim.

---

## 6. Close on the operational point

```bash
grep -A9 "triage-eval-false-page-rate" infra/setup.sh
```

> "The aggregate scores publish as CloudWatch custom metrics, with an alarm on
> tool selection accuracy below 0.85 and false-page rate above 0.10. So a
> quality regression pages me the same way a latency regression does. And
> `treat-missing-data` is `missing`, not `notBreaching` — a suite that stopped
> running must not look like a suite that is passing."

---

## If you have AWS deployed

Only if `bash infra/setup.sh all` has been run and the SNS subscription is
confirmed. The offline demo is the safer one and makes every point except
"it really runs on AWS".

```bash
bash infra/setup.sh smoke
```

That POSTs a real alert to the live API Gateway endpoint and tails the
structured logs. The page arrives by email.

Have ready in the console, since these are worth showing rather than describing:

- The **Logs Insights** query over the single-line JSON logs
- The two **quality alarms**, so the "quality is an operational metric" line has
  something behind it
- The **IAM policy** — `infra/iam-policies/triage-agent-permissions.json` — and
  the fact that `dynamodb:Scan` is absent, not forgotten

---

## Questions you will get, and short answers

**"Why not just use AgentCore Evaluations?"**
I would, now. I built the crude version first because I wanted to understand
what the managed one is doing before depending on it. Having built it, I would
still want ground-truth tool trajectories rather than relying only on LLM
judges — that is the part a managed service cannot supply for my domain.

**"How do you know the judge is right?"**
I do not, and that is the largest gap in the harness. I run two judges — a free
deterministic heuristic and an LLM judge — and report their agreement rate. That
is a consistency check, not a correctness one: they could agree and both be
wrong. The honest fix is hand-labelling 40 traces and measuring the judge
against a person. It is the first thing in ADR-6.

**"Why DynamoDB and not Postgres?"**
Every access pattern here is a key lookup or a key-range scan, and I need a
conditional write as a primitive for the dedupe. Postgres is better at exactly
the things this workload never does, and would bring a VPC, connection pooling
from Lambda, and an instance billing hourly whether or not anything queries it.

**"Why is `paged` not read from the model's own answer?"**
Because models misreport their own behaviour. `paged` is true because a
`page_oncall` call actually succeeded. An eval that scores self-reports measures
honesty rather than judgement. I do capture the gap between claim and action
separately, and it catches a real failure — the agent that says it escalated but
whose page the guardrail blocked.

**"38 cases isn't many."**
No, it is not, and the report says so on every run. One case moves the
false-page rate by 0.053. I do not report significance tests because this
sample would not support them, and a p-value here would be worse than none.

**"What would you do next?"**
Validate the judge against human labels; more cases and real ones; and measure
variance — everything runs at temperature 0 so the deterministic scorers measure
the prompt rather than sampling noise, which is right for the experiment and
wrong as a model of production.

---

## If something breaks mid-demo

Nothing in sections 1–6 touches the network, so the failure modes are small:

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError` | wrong virtualenv | `pip install -r requirements-dev.txt` |
| Garbled table borders | Windows console encoding | `set PYTHONIOENCODING=utf-8` |
| `No such case id` | typo in `--case` | `python -m eval.report --list` |
| A sweep prints different numbers | you changed a scorer | `git stash`, then re-run |

If a live AWS step fails, say what you are looking at and move on to the offline
path — the numbers are the same, and diagnosing IAM in front of an audience
spends the time you wanted for the experiment. `bash infra/setup.sh preflight`
is the one command worth running to see *why*, because it checks credentials,
tooling and Bedrock model access separately.
