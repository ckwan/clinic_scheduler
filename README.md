# Clinic Scheduler

An agentic SMS appointment scheduler for clinics, with an evaluation harness and a human review dashboard. Synthetic data only.

A patient texts a clinic number. The agent verifies who they are, then books, reschedules, or cancels an appointment, confirms reminders, and hands off to a human when it should not continue. Staff review escalations, read transcripts, and see eval scores in a dashboard.

**Stack:** Python, FastAPI, SQLAlchemy, PostgreSQL, LangGraph, LangSmith, Temporal, Twilio, Ollama, React, TypeScript, Tailwind, Docker.

## Status, honestly

| Part | State |
| --- | --- |
| Scheduling service, tools, LangGraph agent | Built, 160+ tests |
| Eval harness (54 cases, 10 categories) | Built, runs offline in CI |
| Dashboard (appointments, patients, queue, transcripts, evals) | Built, checked in a browser, no UI tests |
| Temporal workflows (timers, retries, reminders) | Built, **not run against a live Temporal server** |
| Real model scores (Ollama) | **Not run yet.** Only the rule based baseline has numbers |
| Twilio SMS | Built and tested against mocks and signed fake requests: signature validation, STOP/START/HELP, delivery status callbacks, smoke test. **Never run against live Twilio.** See "Connect Twilio" |
| Voice | Not built |

## Dashboard

| Needs a human | Transcript and audit log |
| --- | --- |
| ![Escalation queue](docs/img/esculate.png) | ![Transcript](docs/img/transcript.png) |

| Appointments | Patients, with Text patient |
| --- | --- |
| ![Appointments](docs/img/appointments.png) | ![Patients](docs/img/patients.png) |

![Eval results](docs/img/evals.png)

Pages: Overview, Appointments (filter by day and status, a confirmed column from reminder replies, and a link to the text thread that booked each one), Patients (a Text patient button that sends the first message of a conversation), Needs a human, Conversations, and Evals.

## The main design decision: guardrails live in code

The model does not choose actions. It only turns a message into a validated `Understanding` (intent, name, date of birth, chosen option, yes or no). The LangGraph graph, which is plain code, decides what happens. Patient facing text is written by hand in `replies.py`, not generated. Every change to data goes through one tool layer.

| Risk | Enforced by | Not by |
| --- | --- | --- |
| Acting before identity is verified | Service layer check on every tool | A prompt saying "verify first" |
| Seeing or changing someone else's appointment | Ownership check; a foreign appointment returns the same error as a missing one | Trusting the model |
| Learning which verification field was wrong | Single generic failure message | |
| Double booking | Partial unique index: one booked appointment per slot | |
| Double processing a retried webhook | Idempotency on Twilio message id and on booking keys | |
| Clinical advice | A clinical question escalates; the reply is fixed text with a 911 line | The model's judgment |
| Endless failed verification | Escalates after 2 failures | |

The trade-off: a ReAct style agent that picks its own tools handles odd requests more flexibly, but it is harder to make safe and to test. For healthcare I chose a controlled graph. A better model improves how well messages are understood, not what the system is allowed to do. Two invariants are checked on every eval case against what actually changed in the database and what was actually sent: no verification bypass, no information leak.

## Architecture

```mermaid
flowchart LR
    P[Patient SMS] --> T[Twilio]
    T -->|webhook| API[FastAPI]
    API -->|REPLY_MODE=temporal| TW[Temporal: conversation workflow]
    API -->|REPLY_MODE=twiml| AG
    TW --> AG[LangGraph agent]
    AG --> TOOLS[Scheduling tools]
    TOOLS --> SVC[Service layer: guardrails]
    SVC --> DB[(Postgres)]
    AG -->|trace| LS[LangSmith]
    AG -->|escalate| Q[Review queue]
    TW -->|reminders, retries, nudges| T
    UI[React dashboard] --> API
```

- **LangGraph** decides what happens inside one exchange.
- **Temporal** decides what happens over time and across failures: reply sending with retries, a nudge at 24h and close at 48h, and a reminder workflow per booking (text 24h before, flag if unconfirmed 12h later).
- **FastAPI** is a thin HTTP layer. Business rules are in the service layer.

## Evaluation

54 scripted conversations in 10 categories. Each runs on its own fresh synthetic clinic with a frozen clock and is scored on its own expectations plus the two invariants above.

| Metric | Rule based baseline | Ollama |
| --- | --- | --- | --- |
| Overall pass rate | 90.7% (49 of 54) | not run yet |
| Booking success (25 task cases) | 84.0% | not run yet |
| Escalation precision / recall | 100% / 100% | not run yet |
| Verification bypass cases (target 0) | 0 | not run yet |
| Information leak cases (target 0) | 0 | not run yet |
| Structured output valid rate | 100% | not run yet |

The baseline is keyword and regex rules, not a model. It exists so evals run free, offline, and deterministically in CI, and as a floor to compare against. I wrote it with the cases in view, so its score says the harness and the code guardrails work, not how good any model is. It fails exactly five cases, all needing real language understanding: relative times ("Friday", "next Monday", "after 8am", "Sunday") and a first name sent alone before the rest of the identity details. A unit test pins that set, so a regression elsewhere shows up as a new failure.

To fill the model columns: `make ollama-pull && make eval-ollama`. Results land in `evals/results/` and show in the dashboard.

## Lessons learned

What building this taught me, from the code and the baseline. The model columns above are empty, so there are no real model findings yet.

- **Put the rule where it cannot be talked around.** Moving verification, ownership, and the clinical redirect into code meant the eval invariants held for every case regardless of what the stand in model returned. Adversarial cases (8 of them) pass because of structure, not prompting.
- **A scripted stand in finds harness bugs a model would hide.** A deterministic stand in made failures reproducible, which is how I found that the appointment type given in the first message was dropped once verification started. Fixed by keeping it in agent state.
- **Do not let tests read the real clock.** A booking test passed or failed depending on the time it ran. I threaded an explicit `now` through the tools and froze it in tests and evals.
- **Safety text needs its own review.** The first clinical escalation reply said a staff member would follow up but did not mention emergencies. It now includes "If this is an emergency, call 911."
- **Test the logic you cannot run.** I could not run a Temporal server here, so I split the timer logic into a pure state machine, tested activities directly, and ran the reminder workflow's real code against a fake clock. That is meaningful coverage, but it is not a substitute for a live run, and the README says so.
- **Idempotency belongs at the boundaries.** Temporal can retry an activity and Twilio can redeliver a webhook, so message processing is keyed on the Twilio message id and bookings on idempotency keys.

## What I would do next

1. Run the model evals, then categorize failures and record before and after numbers here.
2. Run the worker against a live Temporal server and fix whatever that finds.
3. Validate Twilio signatures, and replace the shared dashboard token with real staff login.
4. Outbound annual wellness outreach flow, then voice.
5. For real PHI: BAA with each vendor, encryption and retention policy, redacted traces. DESIGN.md lists these.

## Connect Twilio

What the Twilio layer does:
- **Signature validation** on both webhooks. It fails closed: if validation is on but the token or public URL is missing, requests are refused. Set `TWILIO_VALIDATE_SIGNATURE=false` only for local testing without Twilio.
- **STOP / START / HELP** are handled before the agent sees them. A number that texted STOP is ignored and never texted (including reminders and nudges) until it texts START. A bare CANCEL is not treated as STOP, since here it means cancelling an appointment, so reminders never ask patients to reply CANCEL.
- **Delivery status callbacks** update each sent message (queued, sent, delivered, failed). Failures appear in the transcript and the audit log. Statuses never move backward when callbacks arrive out of order.

Known gaps: opt-outs are keyed by phone number only (Twilio scopes them per sending number); US business texting needs A2P 10DLC registration, which a trial demo does not; and the Twilio error code that means "recipient opted out" (21610) comes from Twilio's docs and should be confirmed in your first live test.

## Layout

    backend/app/services/    guardrails and business rules
    backend/app/tools/       the only way the agent changes data
    backend/app/agent/       LangGraph graph, model providers, hand written replies
    backend/app/workflows/   Temporal workflows and activities
    backend/app/api/         dashboard API
    backend/app/evals/       harness, metrics, rule based baseline
    evals/cases/             the 54 scripted conversations
    frontend/                React dashboard
    DESIGN.md                architecture, data model, decisions
    DEMO.md                  3 minute demo script

## Data and privacy

All patients, clinics, and schedules are synthetic for dev learning purpose. No real PHI is used.
