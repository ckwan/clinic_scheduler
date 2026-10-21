import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { ApiError } from './api'

export function useAsync<T>(load: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<ApiError | Error | null>(null)
  const [loading, setLoading] = useState(true)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(load, deps)

  const reload = useCallback(() => {
    setLoading(true)
    run()
      .then((d) => {
        setData(d)
        setError(null)
      })
      .catch((e) => setError(e))
      .finally(() => setLoading(false))
  }, [run])

  useEffect(reload, [reload])
  return { data, error, loading, reload }
}

export function Card({ title, children, right }: { title?: string; children: ReactNode; right?: ReactNode }) {
  return (
    <section className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
      {(title || right) && (
        <div className="mb-3 flex items-center justify-between gap-2">
          <h2 className="text-sm font-semibold text-slate-600 dark:text-slate-300">{title}</h2>
          {right}
        </div>
      )}
      {children}
    </section>
  )
}

const TONES: Record<string, string> = {
  green: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300',
  red: 'bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300',
  amber: 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
  blue: 'bg-sky-100 text-sky-800 dark:bg-sky-950 dark:text-sky-300',
  gray: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
}

export function Badge({ tone = 'gray', children }: { tone?: keyof typeof TONES; children: ReactNode }) {
  return <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${TONES[tone]}`}>{children}</span>
}

export function stateTone(state: string): keyof typeof TONES {
  if (state === 'escalated' || state === 'open') return 'red'
  if (state === 'taken') return 'amber'
  if (state === 'closed' || state === 'resolved') return 'gray'
  return 'blue'
}

export function Stat({ label, value, hint, tone }: { label: string; value: ReactNode; hint?: string; tone?: 'bad' }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
      <div className="text-xs text-slate-500 dark:text-slate-400">{label}</div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums ${tone === 'bad' ? 'text-red-600 dark:text-red-400' : ''}`}>
        {value}
      </div>
      {hint && <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">{hint}</div>}
    </div>
  )
}

export function Status<T>({ state, children }: { state: ReturnType<typeof useAsync<T>>; children: (data: T) => ReactNode }) {
  if (state.error) {
    const unauthorized = state.error instanceof ApiError && state.error.status === 401
    return (
      <p role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-800 dark:bg-red-950 dark:text-red-300">
        {unauthorized ? 'The API needs a token. Enter it at the top right.' : `Could not load: ${state.error.message}`}
      </p>
    )
  }
  if (state.data === null) return <p className="text-sm text-slate-500">Loading…</p>
  return <>{children(state.data)}</>
}

export const pct = (v: number | null | undefined) => (v === null || v === undefined ? 'n/a' : `${(v * 100).toFixed(1)}%`)

export function when(iso: string | null): string {
  if (!iso) return ''
  return new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
}

export const label = (s: string) => s.replaceAll('_', ' ')
