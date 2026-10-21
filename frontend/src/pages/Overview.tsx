import { api } from '../api'
import { Card, Stat, Status, label, pct, useAsync } from '../ui'

export default function Overview() {
  const summary = useAsync(api.summary)
  const runs = useAsync(api.evalRuns)

  return (
    <div className="space-y-6">
      <Status state={summary}>
        {(s) => (
          <>
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
              <Stat label="Conversations" value={s.conversations} />
              <Stat label="Waiting for a human" value={s.open_escalations} tone={s.open_escalations ? 'bad' : undefined} />
              <Stat label="Completion rate" value={pct(s.completion_rate)} hint="booked, cancelled or rescheduled" />
              <Stat label="Escalation rate" value={pct(s.escalation_rate)} />
            </div>
            <Card title="Outcomes">
              {Object.keys(s.by_outcome).length === 0 ? (
                <p className="text-sm text-slate-500">No finished conversations yet.</p>
              ) : (
                <ul className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
                  {Object.entries(s.by_outcome).map(([k, n]) => (
                    <li key={k}>
                      <span className="font-semibold tabular-nums">{n}</span> {label(k)}
                    </li>
                  ))}
                </ul>
              )}
            </Card>
          </>
        )}
      </Status>

      <Status state={runs}>
        {(list) => (
          <Card title="Latest eval run" right={<a className="text-sm text-sky-600 hover:underline" href="#/evals">All runs</a>}>
            {list.length === 0 ? (
              <p className="text-sm text-slate-500">No eval results found. Run <code>make eval</code>.</p>
            ) : (
              <div className="space-y-3">
                <p className="text-sm text-slate-600 dark:text-slate-300">
                  {list[0].label} · {list[0].model}
                </p>
                <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                  <Stat label="Pass rate" value={pct(list[0].metrics.pass_rate)} hint={`${list[0].metrics.passed} of ${list[0].metrics.cases}`} />
                  <Stat label="Booking success" value={pct(list[0].metrics.booking_success_rate)} />
                  <Stat label="Verification bypasses" value={list[0].metrics.verification_bypass_cases} tone={list[0].metrics.verification_bypass_cases ? 'bad' : undefined} hint="target 0" />
                  <Stat label="Information leaks" value={list[0].metrics.info_leak_cases} tone={list[0].metrics.info_leak_cases ? 'bad' : undefined} hint="target 0" />
                </div>
              </div>
            )}
          </Card>
        )}
      </Status>
    </div>
  )
}
