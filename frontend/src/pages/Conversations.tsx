import { useState } from 'react'
import { api } from '../api'
import { Badge, Card, Status, label, stateTone, useAsync, when } from '../ui'

const FILTERS = ['', 'started', 'verified', 'escalated', 'closed']

export default function Conversations() {
  const [state, setState] = useState('')
  const list = useAsync(() => api.conversations(state || undefined), [state])

  return (
    <Card
      title="Conversations"
      right={
        <select aria-label="Filter by state" value={state} onChange={(e) => setState(e.target.value)} className="rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700">
          {FILTERS.map((f) => (
            <option key={f} value={f}>{f ? label(f) : 'all states'}</option>
          ))}
        </select>
      }
    >
      <Status state={list}>
        {(rows) =>
          rows.length === 0 ? (
            <p className="text-sm text-slate-500">No conversations yet.</p>
          ) : (
            <ul className="divide-y divide-slate-200 dark:divide-slate-800">
              {rows.map((c) => (
                <li key={c.id}>
                  <a href={`#/conversations/${c.id}`} className="flex items-start justify-between gap-3 py-3 hover:bg-slate-50 dark:hover:bg-slate-800/50">
                    <div className="min-w-0">
                      <div className="font-medium">
                        {c.patient ?? 'Unknown caller'} <span className="font-normal text-slate-500">{c.phone}</span>
                      </div>
                      <div className="truncate text-sm text-slate-500 dark:text-slate-400">{c.last_message}</div>
                    </div>
                    <div className="flex shrink-0 flex-col items-end gap-1">
                      <Badge tone={stateTone(c.state)}>{c.state}</Badge>
                      {c.outcome && <span className="text-xs text-slate-500">{label(c.outcome)}</span>}
                      <span className="text-xs text-slate-400">{when(c.updated_at)}</span>
                    </div>
                  </a>
                </li>
              ))}
            </ul>
          )
        }
      </Status>
    </Card>
  )
}
