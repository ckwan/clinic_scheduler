# Clinic Scheduler dashboard

Pages
- Appointments: filter by day and status; confirmed column; link to the text thread that booked it
- Patients: Text patient button (first outbound message; respects opt-outs, a 24 hour limit, and conversations staff own)
- Overview: conversation counts, completion and escalation rates, latest eval run
- Needs a human: open escalations, take over, resolve
- Conversations: filterable list; transcript with the audit log and LangSmith trace id; staff can text the patient only while the conversation is escalated
- Evals: runs from `evals/results/*.json`, per category pass rates, failed cases with what the agent said

