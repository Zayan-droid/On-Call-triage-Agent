# The Capstone — On-Call Triage Agent + Evaluation Harness

**One project. Six days. Every AWS service in their job description, plus the thing that makes you different from every other candidate.**

---

## WHY THIS PROJECT

**Why an eval harness and not just an agent.** Every other shortlisted candidate will arrive with "I built an agent with tool use on Bedrock." That's table stakes now. Your resume already shows what actually sets you apart — the Agentic RAG Evaluation System, where you benchmarked four architectures, defined the metrics, and produced a number. Most engineers build an agent, eyeball a dozen outputs, decide it seems fine, and ship. Very few can answer "how do you know it works?" with anything but vibes.

There's a second reason specific to this job: **AWS shipped AgentCore Evaluations in March 2026**, scoring live agents on tool selection accuracy, tool parameter accuracy, faithfulness, and goal success. If you've built that by hand, you don't just know the product exists — you know what problem it solves and where it falls short. Completely different conversation from having read the docs.

**Why on-call triage as the domain.**
- The role is AWS infrastructure. An agent that operates on infrastructure is on-theme in a way a customer-service bot isn't.
- **The agent queries CloudWatch as one of its tools.** Your Day 5 learning does double duty — you learn CloudWatch as an operator and as a consumer.
- The stakes make evaluation obviously necessary. Paging someone at 3am when you shouldn't have has a real cost. So does staying quiet when you shouldn't have. Those two costs aren't equal, and that asymmetry gives you something genuinely interesting to measure.
- Nothing on your resume touches it, so it reads as its own project.

**What this project will not do:** make you look like an AWS expert. Six days can't. What it does is make you look like someone who builds carefully and measures rigorously, who happens to be one week into AWS. That's an honest and strong position — don't stretch it further.

---

## WHAT YOU'RE BUILDING

An alert arrives at an HTTP endpoint. The agent investigates: pulls recent metrics, checks whether anything deployed lately, searches a runbook. Then it decides — open an incident, page the on-call engineer, or conclude it's noise and stay quiet.

Alongside it, a harness that replays a fixed set of alerts through the agent and scores whether it investigated properly, grounded its reasoning in real data, and escalated correctly.

The agent is the vehicle. **The harness is the headline.**

```
   Alert (POST /alert)  ─────►  API Gateway (HTTP API)
                                        │
                              ┌─────────▼──────────┐
                              │ Lambda: triage-agent│  ← IAM role you wrote yourself
                              └─────────┬──────────┘
                                        │
              ┌──────────────┬──────────┼──────────────┬──────────────┐
              │              │          │              │              │
        ┌─────▼─────┐  ┌─────▼─────┐  ┌─▼──────────┐ ┌─▼─────────┐ ┌──▼──────┐
        │  Bedrock  │  │ CloudWatch│  │  DynamoDB  │ │   SNS     │ │CloudWatch│
        │  Converse │  │ GetMetric │  │ runbooks   │ │  paging   │ │ logs +   │
        │ + tool use│  │ (a TOOL)  │  │ deploys    │ │           │ │ metrics  │
        └───────────┘  └───────────┘  │ incidents  │ └───────────┘ └──────────┘
                                      │ eval_runs  │
                                      └─────┬──────┘
                                            ▲
                              ┌─────────────┴──────────┐
                              │ ECS Fargate:           │ ← >15 min, so genuinely
                              │ batch eval runner      │    not a Lambda
                              └────────────────────────┘
```

Every arrow exists for a defensible reason. If an interviewer asks "why ECS here?" the answer is that a full sweep over 40 alerts takes longer than Lambda's 15-minute ceiling. A real constraint producing a real decision — exactly what "strong understanding of architecture" means.

### The agent's tools

| Tool | What it does | Why it's here |
|---|---|---|
| `get_service_metrics(service, metric, window)` | Reads real CloudWatch metrics | Teaches you the CloudWatch API from the consumer side |
| `get_recent_deploys(service, hours)` | DynamoDB lookup | Most incidents correlate with a deploy — realistic reasoning step |
| `search_runbook(symptom)` | DynamoDB lookup | Grounding source; the agent should cite it, not invent remediation |
| `create_incident(severity, summary)` | DynamoDB write, **conditional** | Dedupe: same alert fingerprint within 15 min must not open a second incident |
| `page_oncall(incident_id, reason)` | SNS → your email | The high-stakes action. Everything in the eval revolves around whether this fires correctly. |

The conditional write lives in `create_incident` — alert storms are a real production problem and deduping them is the same DynamoDB technique as any other race condition.

---

## SCOPE DISCIPLINE — READ BEFORE STARTING

Six days, zero AWS. Your failure mode is overbuilding, not laziness.

**MUST SHIP (this is the project):**
- Agent with the five tools above
- Bedrock Converse with a working tool-use loop
- DynamoDB: runbooks, deploys, incidents — with conditional-write dedupe
- API Gateway endpoint callable from `curl`
- SNS topic for paging
- CloudWatch: structured logs, custom metrics, one alarm
- **Eval harness: 35–40 test alerts, 4 metrics, results in DynamoDB, scores as CloudWatch metrics**
- Clean README with the architecture diagram

**IF TIME (Day 6, kept separate from the working system):**
- ECS Fargate batch eval runner
- One AgentCore Runtime deploy

**DO NOT BUILD:**
- A frontend. Nobody grades your UI and it eats a day.
- Real PagerDuty/Slack integration. SNS to your own email is enough.
- RAG or Knowledge Bases. Vector stores cost real money and it's a different project.
- Auth beyond an API key. Note in the README that you'd use a JWT authorizer in production — that sentence is worth as much as building it.
- Multi-turn conversation. One alert, one investigation, done.

**If you fall behind, cut ECS and AgentCore first.** Core system plus harness is the project.

---

## THE EVAL HARNESS — THE PART THAT MATTERS

Day 5's work, and what you'll actually talk about in the interview.

### The test set

JSONL, 35–40 cases. Each is one alert plus what should happen:

```json
{
  "id": "cpu_spike_003",
  "alert": {
    "service": "checkout-api",
    "metric": "CPUUtilization",
    "value": 94,
    "threshold": 80,
    "duration_min": 12
  },
  "expected_tools": ["get_service_metrics", "get_recent_deploys"],
  "expected_page": true,
  "reference_reasoning": "Sustained CPU above threshold 12 min, correlates with deploy 20 min prior. Page."
}
```

Four buckets, roughly ten each:

- **Clear incident** — should investigate and page
- **Needs investigation** — ambiguous on its face; the agent must gather metrics *before* deciding, not page reflexively
- **Multi-step correlation** — requires metrics *and* deploy history to reach the right call
- **Noise** — a brief blip, a known-flapping alert, a dev-environment alarm. **Correct behaviour is to NOT page.**

That fourth bucket is where agents fail and almost nobody tests it. It's also where your most interesting metric comes from.

### The four metrics

**1. Tool selection accuracy** — did it call the right tools? Deterministic, no model needed.

**2. Tool parameter accuracy** — right arguments? Correct service name, sensible time window. Deterministic.

**3. Groundedness** — does its reasoning cite values the tools actually returned, rather than invented numbers? Needs an LLM judge. Log the judge's justification, not just the score.

**4. Escalation correctness** — did it page when it should, and stay quiet when it shouldn't? Report this as **precision and recall on paging**, not a single number.

Metric 4 is the one that makes this project memorable. **The two error types have different costs and you should weight them accordingly:** a missed page means an outage runs longer; a false page burns an engineer's night and, repeated, causes alert fatigue that makes the whole system worse. Say that out loud in the interview — reasoning about asymmetric error costs is a senior signal.

And say this too: **two of the four metrics are deterministic**, so they're cheap, fast, and can't drift. You only pay for a judge where the question is genuinely subjective. That's a cost and reliability argument, and it separates someone who's thought about eval from someone who copied an eval tutorial.

### Wiring it up

Sweep → per-case results to DynamoDB (`PK: RUN#<timestamp>`, `SK: CASE#<id>`) → aggregate scores as CloudWatch custom metrics → alarm if tool selection accuracy drops below 0.85 or false-page rate rises above 0.10.

**A quality regression pages you the same way a latency regression does.** That sentence reframes agent quality as an operational metric rather than a vibe. Use it.

### The experiment — do not skip this

Once the harness runs, change one thing and measure it:
- Two different system prompts (one that says "when uncertain, investigate further" vs one that doesn't)
- Two Claude models
- Terse vs verbose tool descriptions

Then your README says something like: *"adding an explicit instruction to gather metrics before escalating cut the false-page rate from 0.24 to 0.09 with no loss in recall."*

**A measured number is what makes this project stick.** Your RAG bullet is your best one precisely because it ends in "~35%."

---

## DAY-BY-DAY BUILD

Each day teaches an AWS service and produces a component. No throwaway tutorials.

### Day 1 (today) — IAM + first Lambda
Read a real role in the console, then create `triage-agent` as a stub returning `{"ok": true}`. **Remove its logging permission on purpose, watch it fail, put it back.**
→ *Ends with:* a deployed function, and real understanding of why nothing works without a role.

### Day 2 — Lambda depth + API Gateway
Cold starts, memory/CPU coupling, module-scope initialisation. HTTP API in front of the function.
→ *Ends with:* a public HTTPS URL you can POST an alert to.

### Day 3 — DynamoDB
Write the access patterns *first*, then design the table. Seed runbooks and deploy history. Implement `create_incident` with conditional-write dedupe.
→ *Ends with:* four of five tools working as plain Python against real data.

### Day 4 — Bedrock
Converse, then the full tool-use loop: model requests a tool → your code runs it → result returns → model continues. Wire in Day 3's functions plus the CloudWatch metrics tool.
→ *Ends with:* the agent investigating a real alert and reaching a decision. **This is the day it becomes a system.**

### Day 5 — CloudWatch + the eval harness
Structured logging, retention, custom metrics, Logs Insights, alarms. Then the test set, the four scorers, results storage, scores as metrics, regression alarm. Run your experiment.
→ *Ends with:* a number. **Longest and most important day — protect it.**

### Day 6 — ECS + AgentCore (both optional)
Containerise the eval runner on Fargate; learn task role vs task execution role. One trivial AgentCore Runtime deploy.
→ **Cut this day entirely if Day 5 isn't finished.**

### Day 7 — Ship and rehearse
README, diagram, results table. Draw the architecture from memory five times, out loud. Cost pass, security pass. **Delete every billable resource.**

---

## THE REPO

```
oncall-triage-agent/
├── README.md              ← diagram, results table, limitations
├── architecture.png
├── agent/
│   ├── handler.py         ← Lambda entrypoint, Converse loop
│   ├── tools.py           ← the five tools
│   └── prompts.py
├── eval/
│   ├── alerts.jsonl       ← your 35–40 test cases
│   ├── scorers.py         ← the four metrics
│   ├── run.py
│   └── results/
├── infra/
│   ├── iam-policies/      ← the roles you wrote, as JSON
│   └── setup.sh           ← CLI commands that create everything
└── docs/
    └── decisions.md       ← 5 short architecture decision records
```

Two details that punch above their weight:

**`infra/iam-policies/`** — checking in the actual policies proves you didn't just attach `AdministratorAccess` and move on. Scope them: `bedrock:InvokeModel` on one model ARN, `dynamodb:*Item` on one table, `cloudwatch:GetMetricStatistics`, `sns:Publish` on one topic. Nothing else.

**`docs/decisions.md`** — five paragraphs: why DynamoDB over RDS; why ECS for the eval runner; why two deterministic metrics and two judged; why conditional writes rather than a transaction; what you'd change with more time. Cheapest possible demonstration of architectural thinking, and it gives an interviewer something to ask about.

### README structure
1. One line: what it is
2. Architecture diagram
3. **Results table** — the four metrics, plus your experiment comparison
4. Design decisions — three bullets linking to `decisions.md`
5. **Known limitations** — specific and honest. "Synthetic alerts only. 38 cases is too small for tight confidence intervals. Single-turn. No auth beyond an API key." Naming your own limits is a credibility multiplier.
6. Run it yourself

---

## THE RESUME BULLET

Written in the voice of your existing bullets. **Don't add it until it's true. Don't invent the numbers.**

> **On-Call Triage Agent + Evaluation Harness (AWS)** — Tool-using agent on Amazon Bedrock that investigates infrastructure alerts by querying CloudWatch metrics, deploy history, and runbooks, then decides whether to escalate. Serverless on Lambda, API Gateway, and DynamoDB with conditional writes deduplicating alert storms. Built an evaluation harness scoring tool selection, parameter accuracy, groundedness, and escalation precision/recall across 38 alerts; scores emit as CloudWatch metrics with alarms on quality regression. [Prompt change cut false-page rate from 0.24 to 0.09 with no loss in recall.]

Short version:

> **On-Call Triage Agent + Eval Harness (AWS)** — Bedrock tool-using agent on Lambda/API Gateway/DynamoDB that triages infrastructure alerts and decides when to page. Evaluation harness scores tool selection, groundedness, and escalation precision/recall across 38 cases, with quality regressions alarming through CloudWatch like any other production metric.

Updated Infrastructure line, once honestly true:

> `AWS (Lambda, API Gateway, DynamoDB, IAM, CloudWatch, SNS), Amazon Bedrock, FastAPI, Docker, Kubernetes, PostgreSQL, GitHub Actions, MLflow, Prometheus/Grafana`

Add ECS and AgentCore only if you reach Day 6.

---

## WHAT TO SAY ABOUT IT

Lead with the eval, not the agent. Everyone's agent looks the same.

> *"The agent itself is fairly standard — Bedrock Converse, five tools, CloudWatch and DynamoDB behind them. What I actually cared about is the harness. I have 38 test alerts across four categories, and the important one is the noise bucket, where the correct behaviour is to investigate and then stay quiet. Two of the four metrics are deterministic — did it call the right tools, with the right arguments — so they're cheap and can't drift. Groundedness and escalation correctness need a judge, and I log the judge's reasoning rather than just the score.*
>
> *I report escalation as precision and recall separately rather than one number, because the two failure modes have different costs. A missed page means an outage runs longer. A false page burns someone's night, and enough of them cause the alert fatigue that makes the whole system worse. Those aren't interchangeable, so a single accuracy figure would hide the thing I care about.*
>
> *Scores go to CloudWatch as custom metrics with an alarm on tool selection accuracy and false-page rate, so a quality regression alerts me the same way a latency regression would. One experiment: adding an explicit instruction to gather metrics before escalating took the false-page rate from 0.24 to 0.09 without hurting recall.*
>
> *I built it partly because I'd done something similar for retrieval — benchmarking four RAG architectures — and partly because I wanted to understand what AgentCore Evaluations is doing before using it. Having built the crude version, I'd use the managed one, but I'd still want ground-truth tool trajectories rather than relying only on LLM judges."*

Four things at once: demonstrates the AWS services, shows judgment about cost and reliability, connects to prior work on your resume, and ends with an informed opinion about their stack.

---

## NAMING

`oncall-triage-agent`, `tooltrace`, or `pagecheck`. Pick one that stands alone — the repo should read as its own project, not an appendix to anything else on your resume.
