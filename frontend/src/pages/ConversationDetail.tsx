import { useState, type FormEvent } from 'react'
import { api } from '../api'
import { Badge, Card, Status, label, stateTone, useAsync, when } from '../ui'

const BUBBLE = {
  patient: 'self-start bg-slate-200 dark:bg-slate-800',
  agent: 'self-end bg-sky-600 text-white',
  staff: 'self-end bg-emerald-600 text-white',
} as const

export default function ConversationDetail({ id, staffName }: { id: string; staffName: string }) {
  const detail = useAsync(() => api.conversation(id), [id])
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function send(e: FormEvent) {
    e.preventDefault()
    if (!text.trim()) return
    setSending(true)
    setError(null)
    try {
      await api.reply(id, text.trim(), staffName)
      setText('')
      detail.reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not send')
    } finally {
      setSending(false)
    }
  }

  return (
    <div className="space-y-4">
      <a href="#/conversations" className="text-sm text-sky-600 hover:underline">← All conversations</a>
      <Status state={detail}>
        {(c) => (
          <div className="grid gap-4 lg:grid-cols-3">
            <div className="space-y-4 lg:col-span-2">
              <Card
                title={`${c.patient ?? 'Unknown caller'} · ${c.phone}`}
                right={<Badge tone={stateTone(c.state)}>{c.state}</Badge>}
              >
                <div className="flex flex-col gap-2" aria-label="Transcript">
                  {c.messages.map((m) => (
                    <div key={m.id} className={`max-w-[85%] whitespace-pre-wrap rounded-2xl px-3 py-2 text-sm ${BUBBLE[m.sender]}`}>
                      {m.body}
                      <div className="mt-1 text-[11px] opacity-70">
                        {m.sender} · {when(m.at)}
                        {m.delivery && m.sender !== 'patient' && ` · ${m.delivery}`}
                      </div>
                      {(m.delivery === 'failed' || m.delivery === 'undelivered') && (
                        <div role="alert" className="mt-1 rounded bg-red-100 px-2 py-0.5 text-[11px] font-medium text-red-800">
                          Not delivered{m.delivery_error ? ` (error ${m.delivery_error})` : ''}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
                {c.state === 'escalated' ? (
                  <form onSubmit={send} className="mt-4 flex gap-2">
                    <input value={text} onChange={(e) => setText(e.target.value)} maxLength={1000} placeholder={`Reply as ${staffName}…`} aria-label="Reply to patient" className="min-w-0 flex-1 rounded-lg border border-slate-300 bg-transparent px-3 py-2 text-sm dark:border-slate-700" />
                    <button disabled={sending || !text.trim()} className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-700 disabled:opacity-50">Send SMS</button>
                  </form>
                ) : (
                  <p className="mt-4 text-xs text-slate-500">You can reply only to escalated conversations, so staff never talk over the agent.</p>
                )}
                {error && <p role="alert" className="mt-2 text-sm text-red-600">{error}</p>}
              </Card>
            </div>
            <div className="space-y-4">
              <Card title="Details">
                <dl className="grid grid-cols-2 gap-y-1 text-sm">
                  <dt className="text-slate-500">Verified</dt><dd>{c.verified ? 'yes' : 'no'}</dd>
                  <dt className="text-slate-500">Outcome</dt><dd>{c.outcome ? label(c.outcome) : '—'}</dd>
                  <dt className="text-slate-500">Agent phase</dt><dd>{label(c.phase)}</dd>
                  <dt className="text-slate-500">Trace</dt>
                  <dd className="break-all font-mono text-xs">{c.langsmith_trace_id ?? '—'}</dd>
                </dl>
              </Card>
              <Card title="Audit log">
                <ol className="space-y-2 text-sm">
                  {c.audit.map((a, i) => (
                    <li key={i}>
                      <span className="font-medium">{label(a.action)}</span>{' '}
                      <span className="text-slate-500">{a.actor} · {when(a.at)}</span>
                    </li>
                  ))}
                  {c.audit.length === 0 && <li className="text-slate-500">Nothing recorded.</li>}
                </ol>
              </Card>
            </div>
          </div>
        )}
      </Status>
    </div>
  )
}
