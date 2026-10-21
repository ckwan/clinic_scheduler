import { useState } from 'react'
import { api, type PatientRow } from '../api'
import { Badge, Card, Status, useAsync, when } from '../ui'

type Result = { ok: boolean; text: string; conversationId?: string }

export default function Patients({ staffName }: { staffName: string }) {
  const list = useAsync(api.patients)
  const [busy, setBusy] = useState<string | null>(null)
  const [results, setResults] = useState<Record<string, Result>>({})

  async function text(p: PatientRow) {
    setBusy(p.id)
    try {
      const sent = await api.textPatient(p.id, staffName)
      setResults((r) => ({ ...r, [p.id]: { ok: true, text: 'Sent', conversationId: sent.conversation_id } }))
      list.reload()
    } catch (err) {
      setResults((r) => ({ ...r, [p.id]: { ok: false, text: err instanceof Error ? err.message : 'Failed' } }))
    } finally {
      setBusy(null)
    }
  }

  return (
    <Card title="Patients">
      <p className="mb-3 text-sm text-slate-500">
        “Text patient” sends the first message of a conversation. The patient’s reply goes to the agent. Patients who opted out, or who were texted in the last 24 hours, cannot be texted.
      </p>
      <Status state={list}>
        {(rows) => (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-xs text-slate-500">
                <tr><th className="py-1 pr-4">Patient</th><th className="pr-4">Next appointment</th><th className="pr-4">Last texted by staff</th><th /></tr>
              </thead>
              <tbody>
                {rows.map((p) => {
                  const result = results[p.id]
                  return (
                    <tr key={p.id} className="border-t border-slate-200 dark:border-slate-800">
                      <td className="py-2 pr-4">
                        {p.name} <span className="text-slate-500">{p.phone}</span>{' '}
                        {p.opted_out && <Badge tone="red">opted out</Badge>}
                      </td>
                      <td className="pr-4">{p.next_appointment ? when(p.next_appointment) : <span className="text-slate-400">none</span>}</td>
                      <td className="pr-4">{p.last_outreach_at ? when(p.last_outreach_at) : <span className="text-slate-400">never</span>}</td>
                      <td className="text-right">
                        <div className="flex items-center justify-end gap-3">
                          {result && (
                            <span role={result.ok ? 'status' : 'alert'} className={`text-xs ${result.ok ? 'text-emerald-600' : 'text-red-600'}`}>
                              {result.text}
                              {result.ok && result.conversationId && (
                                <> · <a className="underline" href={`#/conversations/${result.conversationId}`}>view</a></>
                              )}
                            </span>
                          )}
                          <button disabled={p.opted_out || busy === p.id} onClick={() => text(p)} className="rounded-lg bg-sky-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-sky-700 disabled:cursor-not-allowed disabled:opacity-40">
                            Text patient
                          </button>
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </Status>
    </Card>
  )
}
