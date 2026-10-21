import { useState } from 'react'
import { api, type Escalation } from '../api'
import { Badge, Card, Status, label, stateTone, useAsync, when } from '../ui'

export default function Queue({ staffName }: { staffName: string }) {
  const list = useAsync(() => api.escalations('active'))
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function act(e: Escalation, action: 'take' | 'resolve') {
    setBusy(e.id)
    setError(null)
    try {
      if (action === 'take') await api.take(e.id, staffName)
      else await api.resolve(e.id)
      list.reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed')
      list.reload()
    } finally {
      setBusy(null)
    }
  }

  return (
    <Card title="Needs a human" right={<button className="text-sm text-sky-600 hover:underline" onClick={list.reload}>Refresh</button>}>
      {error && <p role="alert" className="mb-3 rounded-lg bg-red-50 p-2 text-sm text-red-800 dark:bg-red-950 dark:text-red-300">{error}</p>}
      <Status state={list}>
        {(rows) =>
          rows.length === 0 ? (
            <p className="text-sm text-slate-500">Nothing waiting. The agent is handling everything.</p>
          ) : (
            <ul className="divide-y divide-slate-200 dark:divide-slate-800">
              {rows.map((e) => (
                <li key={e.id} className="flex flex-wrap items-center justify-between gap-3 py-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <a className="font-medium hover:underline" href={`#/conversations/${e.conversation_id}`}>
                        {e.patient ?? 'Unknown caller'} <span className="font-normal text-slate-500">{e.phone}</span>
                      </a>
                      <Badge tone={stateTone(e.status)}>{e.status}</Badge>
                    </div>
                    <div className="text-sm text-slate-500 dark:text-slate-400">
                      {label(e.reason)} · {when(e.created_at)}
                      {e.assigned_to && ` · ${e.assigned_to}`}
                    </div>
                  </div>
                  <div className="flex gap-2">
                    {e.status === 'open' && (
                      <button disabled={busy === e.id} onClick={() => act(e, 'take')} className="rounded-lg bg-sky-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-sky-700 disabled:opacity-50">
                        Take over
                      </button>
                    )}
                    <button disabled={busy === e.id} onClick={() => act(e, 'resolve')} className="rounded-lg border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-100 disabled:opacity-50 dark:border-slate-700 dark:hover:bg-slate-800">
                      Resolve
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )
        }
      </Status>
    </Card>
  )
}
