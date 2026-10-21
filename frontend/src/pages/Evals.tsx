import { useState } from 'react'
import { api, type EvalRunSummary } from '../api'
import { Badge, Card, Stat, Status, label, pct, useAsync, when } from '../ui'

function Runs({ runs }: { runs: EvalRunSummary[] }) {
  const [selected, setSelected] = useState(runs[0]?.label ?? '')
  const run = useAsync(() => api.evalRun(selected), [selected])

  if (runs.length === 0) return <Card><p className="text-sm text-slate-500">No eval results found. Run <code>make eval</code>.</p></Card>

  return (
    <div className="space-y-4">
      <Card title="Runs">
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="text-xs text-slate-500">
              <tr><th className="py-1 pr-4">Run</th><th className="pr-4">Model</th><th className="pr-4">Pass</th><th className="pr-4">Bypass</th><th>Leaks</th></tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.label} onClick={() => setSelected(r.label)} className={`cursor-pointer border-t border-slate-200 dark:border-slate-800 ${r.label === selected ? 'bg-sky-50 dark:bg-sky-950/40' : ''}`}>
                  <td className="py-2 pr-4 font-medium">{r.label}<div className="text-xs font-normal text-slate-500">{when(r.created_at)}</div></td>
                  <td className="pr-4">{r.model}</td>
                  <td className="pr-4 tabular-nums">{pct(r.metrics.pass_rate)}</td>
                  <td className="pr-4 tabular-nums">{r.metrics.verification_bypass_cases}</td>
                  <td className="tabular-nums">{r.metrics.info_leak_cases}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <Status state={run}>
        {(r) => {
          const failed = r.results.filter((c) => !c.passed)
          return (
            <>
              <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                <Stat label="Pass rate" value={pct(r.metrics.pass_rate)} hint={`${r.metrics.passed} of ${r.metrics.cases} cases`} />
                <Stat label="Booking success" value={pct(r.metrics.booking_success_rate)} hint={`${r.metrics.task_cases} task cases`} />
                <Stat label="Escalation precision / recall" value={`${pct(r.metrics.escalation_precision)} / ${pct(r.metrics.escalation_recall)}`} />
                <Stat label="Avg turns to finish" value={r.metrics.avg_turns_to_complete ?? 'n/a'} />
              </div>
              <Card title="By category">
                <ul className="space-y-2">
                  {Object.entries(r.metrics.by_category).map(([k, v]) => (
                    <li key={k} className="grid grid-cols-[9rem_1fr_3.5rem] items-center gap-3 text-sm">
                      <span>{label(k)}</span>
                      <div className="h-2 rounded-full bg-slate-200 dark:bg-slate-800" role="img" aria-label={`${v.passed} of ${v.total} passed`}>
                        <div className={`h-2 rounded-full ${v.passed === v.total ? 'bg-emerald-500' : 'bg-amber-500'}`} style={{ width: `${(v.passed / v.total) * 100}%` }} />
                      </div>
                      <span className="text-right tabular-nums text-slate-500">{v.passed}/{v.total}</span>
                    </li>
                  ))}
                </ul>
              </Card>
              <Card title={`Failed cases (${failed.length})`}>
                {failed.length === 0 ? <p className="text-sm text-slate-500">Every case passed.</p> : (
                  <ul className="divide-y divide-slate-200 dark:divide-slate-800">
                    {failed.map((c) => (
                      <li key={c.id} className="py-3 text-sm">
                        <div className="flex items-center gap-2"><span className="font-mono">{c.id}</span><Badge tone="red">{label(c.failure_category ?? 'failed')}</Badge></div>
                        {c.failure_detail && <p className="mt-1 text-slate-600 dark:text-slate-300">{c.failure_detail}</p>}
                        {c.replies.length > 0 && <p className="mt-1 text-slate-500">Last reply: {c.replies[c.replies.length - 1]}</p>}
                      </li>
                    ))}
                  </ul>
                )}
              </Card>
            </>
          )
        }}
      </Status>
    </div>
  )
}

export default function Evals() {
  const runs = useAsync(api.evalRuns)
  return <Status state={runs}>{(list) => <Runs runs={list} />}</Status>
}
