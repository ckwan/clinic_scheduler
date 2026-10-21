import { useState } from 'react'
import { api, type AppointmentRow } from '../api'
import { Badge, Card, Status, label, useAsync } from '../ui'

const STATUSES = [
  ['all', 'All'],
  ['booked', 'Booked'],
  ['confirmed', 'Confirmed'],
  ['unconfirmed', 'Not confirmed'],
  ['cancelled', 'Cancelled'],
] as const

const dayTime = (iso: string) =>
  new Date(iso).toLocaleString([], { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })

function StatusCell({ row }: { row: AppointmentRow }) {
  if (row.status === 'cancelled') return <Badge tone="gray">cancelled</Badge>
  if (row.confirmed) return <Badge tone="green">confirmed</Badge>
  return <Badge tone="amber">booked</Badge>
}

export default function Appointments() {
  const [day, setDay] = useState('')
  const [status, setStatus] = useState<string>('all')
  const list = useAsync(() => api.appointments(day, status), [day, status])

  return (
    <Card
      title="Appointments"
      right={
        <div className="flex flex-wrap items-center gap-2">
          <input type="date" aria-label="Filter by day" value={day} onChange={(e) => setDay(e.target.value)} className="rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700" />
          {day && <button className="text-sm text-sky-600 hover:underline" onClick={() => setDay('')}>All days</button>}
          <select aria-label="Filter by status" value={status} onChange={(e) => setStatus(e.target.value)} className="rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700">
            {STATUSES.map(([value, text]) => <option key={value} value={value}>{text}</option>)}
          </select>
        </div>
      }
    >
      <Status state={list}>
        {(rows) =>
          rows.length === 0 ? (
            <p className="text-sm text-slate-500">No appointments match.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs text-slate-500">
                  <tr>
                    <th className="py-1 pr-4">When</th>
                    <th className="pr-4">Patient</th>
                    <th className="pr-4">Type</th>
                    <th className="pr-4">Provider</th>
                    <th className="pr-4">Status</th>
                    <th>Booked in</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.id} className="border-t border-slate-200 dark:border-slate-800">
                      <td className="whitespace-nowrap py-2 pr-4">{dayTime(r.starts_at)}</td>
                      <td className="pr-4">{r.patient} <span className="text-slate-500">{r.phone}</span></td>
                      <td className="pr-4">{label(r.type)}</td>
                      <td className="pr-4">{r.provider}</td>
                      <td className="pr-4"><StatusCell row={r} /></td>
                      <td>
                        {r.conversation_id ? (
                          <a className="text-sky-600 hover:underline" href={`#/conversations/${r.conversation_id}`}>Text thread</a>
                        ) : (
                          <span className="text-slate-400">not via text</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        }
      </Status>
    </Card>
  )
}
