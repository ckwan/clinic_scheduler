import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from 'react'
import { booking, BookingError, type Booked, type LinkInfo, type Slot } from './api'

// What a patient sees after tapping the link in their text. Mobile first: one task per screen,
// big tap targets, no login. The date of birth is asked once and sent with each request.

type Stage =
  | { name: 'loading' }
  | { name: 'blocked'; message: string; tone?: 'ok' | 'warn' }
  | { name: 'dob'; info: Extract<LinkInfo, { status: 'ok' }> }
  | { name: 'pick'; info: Extract<LinkInfo, { status: 'ok' }>; slots: Slot[]; tz: string }
  | { name: 'done'; booked: Booked }

const dayKey = (iso: string, tz: string) =>
  new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(iso))
const dayParts = (iso: string, tz: string) => ({
  weekday: new Intl.DateTimeFormat('en-US', { timeZone: tz, weekday: 'short' }).format(new Date(iso)),
  day: new Intl.DateTimeFormat('en-US', { timeZone: tz, day: 'numeric' }).format(new Date(iso)),
  month: new Intl.DateTimeFormat('en-US', { timeZone: tz, month: 'short' }).format(new Date(iso)),
})
const timeOf = (iso: string, tz: string) =>
  new Intl.DateTimeFormat('en-US', { timeZone: tz, hour: 'numeric', minute: '2-digit' }).format(new Date(iso))
const longDay = (iso: string, tz: string) =>
  new Intl.DateTimeFormat('en-US', { timeZone: tz, weekday: 'long', month: 'long', day: 'numeric' }).format(new Date(iso))

function Shell({ clinic, children }: { clinic?: string; children: ReactNode }) {
  return (
    <div className="mx-auto flex min-h-screen max-w-md flex-col px-5 pb-10 pt-8">
      <p className="text-xs font-medium uppercase tracking-wide text-teal-700 dark:text-teal-400">{clinic ?? 'Clinic Scheduler'}</p>
      <div className="mt-4 flex-1">{children}</div>
      <p className="mt-10 text-center text-xs text-slate-400">Need help? Reply to our text message.</p>
    </div>
  )
}

const button =
  'w-full rounded-xl bg-teal-700 px-4 py-3.5 text-base font-semibold text-white transition hover:bg-teal-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-700 disabled:cursor-not-allowed disabled:opacity-40'

export default function BookingPage({ token }: { token: string }) {
  const [stage, setStage] = useState<Stage>({ name: 'loading' })
  const [dob, setDob] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [selected, setSelected] = useState<Slot | null>(null)
  const [day, setDay] = useState<string | null>(null)

  useEffect(() => {
    booking
      .open(token)
      .then((info) => setStage(info.status === 'ok' ? { name: 'dob', info } : { name: 'blocked', message: info.message }))
      .catch(() => setStage({ name: 'blocked', message: 'We could not load this page. Please check your connection and try again.' }))
  }, [token])

  async function loadSlots(info: Extract<LinkInfo, { status: 'ok' }>) {
    const data = await booking.slots(token, dob)
    setSelected(null)
    setDay(data.slots[0] ? dayKey(data.slots[0].starts_at, data.timezone) : null)
    setStage({ name: 'pick', info, slots: data.slots, tz: data.timezone })
  }

  function fail(err: unknown) {
    if (err instanceof BookingError && ['locked', 'needs_staff', 'expired', 'invalid', 'used'].includes(err.code)) {
      setStage({ name: 'blocked', message: err.message })
    } else if (err instanceof BookingError && err.code === 'wrong_details') {
      const left = err.attemptsRemaining
      setError(`That doesn't match our records.${left ? ` You have ${left} more ${left === 1 ? 'try' : 'tries'}.` : ''}`)
    } else {
      setError(err instanceof Error ? err.message : 'Something went wrong. Please try again.')
    }
  }

  async function submitDob(e: FormEvent, info: Extract<LinkInfo, { status: 'ok' }>) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await loadSlots(info)
    } catch (err) {
      fail(err)
    } finally {
      setBusy(false)
    }
  }

  async function confirm(info: Extract<LinkInfo, { status: 'ok' }>) {
    if (!selected) return
    setBusy(true)
    setError(null)
    try {
      setStage({ name: 'done', booked: await booking.book(token, dob, selected.slot_id) })
    } catch (err) {
      if (err instanceof BookingError && err.code === 'slot_taken') {
        await loadSlots(info).catch(fail)
        setError(err.message)
      } else fail(err)
    } finally {
      setBusy(false)
    }
  }

  const days = useMemo(() => {
    if (stage.name !== 'pick') return []
    const seen = new Map<string, Slot[]>()
    for (const s of stage.slots) {
      const k = dayKey(s.starts_at, stage.tz)
      seen.set(k, [...(seen.get(k) ?? []), s])
    }
    return [...seen.entries()]
  }, [stage])

  if (stage.name === 'loading') {
    return <Shell><p className="text-slate-500" role="status">Loading…</p></Shell>
  }

  if (stage.name === 'blocked') {
    return (
      <Shell>
        <h1 className="text-2xl font-semibold">We can't open this link</h1>
        <p className="mt-3 text-slate-600 dark:text-slate-300" role="alert">{stage.message}</p>
      </Shell>
    )
  }

  if (stage.name === 'dob') {
    const { info } = stage
    return (
      <Shell clinic={info.clinic}>
        <h1 className="text-2xl font-semibold">Hi {info.first_name}, let's book your yearly visit</h1>
        <p className="mt-3 text-slate-600 dark:text-slate-300">To keep your information private, confirm your date of birth first.</p>
        <form onSubmit={(e) => submitDob(e, info)} className="mt-6 space-y-4">
          <label className="block text-sm font-medium" htmlFor="dob">Date of birth</label>
          <input
            id="dob" type="date" required autoComplete="bday" value={dob} max={new Date().toISOString().slice(0, 10)}
            onChange={(e) => setDob(e.target.value)}
            className="w-full rounded-xl border border-slate-300 bg-white px-4 py-3.5 text-base dark:border-slate-700 dark:bg-slate-900"
          />
          {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
          <button className={button} disabled={busy || !dob}>{busy ? 'Checking…' : 'Continue'}</button>
        </form>
      </Shell>
    )
  }

  if (stage.name === 'pick') {
    const { info, tz } = stage
    const todays = days.find(([k]) => k === day)?.[1] ?? []
    return (
      <Shell clinic={info.clinic}>
        <h1 className="text-2xl font-semibold">Pick a time</h1>
        <p className="mt-1 text-sm text-slate-500">{info.appointment_name} · times shown in {tz.replace('_', ' ').split('/')[1] ?? tz} time</p>
        {days.length === 0 ? (
          <p className="mt-6 text-slate-600 dark:text-slate-300">There are no open times right now. Please reply to our text and we'll find one for you.</p>
        ) : (
          <>
            <div className="-mx-5 mt-5 flex gap-2 overflow-x-auto px-5 pb-2" role="tablist" aria-label="Day">
              {days.map(([k, slots]) => {
                const p = dayParts(slots[0].starts_at, tz)
                const on = k === day
                return (
                  <button key={k} role="tab" aria-selected={on} onClick={() => { setDay(k); setSelected(null) }}
                    className={`flex w-16 shrink-0 flex-col items-center rounded-xl border px-2 py-2.5 ${on ? 'border-teal-700 bg-teal-700 text-white' : 'border-slate-300 bg-white dark:border-slate-700 dark:bg-slate-900'}`}>
                    <span className="text-xs opacity-80">{p.weekday}</span>
                    <span className="text-lg font-semibold leading-tight">{p.day}</span>
                    <span className="text-xs opacity-80">{p.month}</span>
                  </button>
                )
              })}
            </div>
            <h2 className="mt-5 text-sm font-medium text-slate-500">{todays[0] && longDay(todays[0].starts_at, tz)}</h2>
            <div className="mt-3 grid grid-cols-3 gap-2" role="radiogroup" aria-label="Time">
              {todays.map((s) => (
                <button key={s.slot_id} role="radio" aria-checked={selected?.slot_id === s.slot_id} onClick={() => setSelected(s)}
                  className={`rounded-xl border px-2 py-3 text-sm font-medium ${selected?.slot_id === s.slot_id ? 'border-teal-700 bg-teal-50 text-teal-900 ring-2 ring-teal-700 dark:bg-teal-950 dark:text-teal-100' : 'border-slate-300 bg-white dark:border-slate-700 dark:bg-slate-900'}`}>
                  {timeOf(s.starts_at, tz)}
                </button>
              ))}
            </div>
          </>
        )}
        {error && <p role="alert" className="mt-4 text-sm text-red-600">{error}</p>}
        {selected && (
          <div className="sticky bottom-4 mt-8">
            <p className="mb-2 text-center text-sm text-slate-600 dark:text-slate-300">
              {longDay(selected.starts_at, tz)} at {timeOf(selected.starts_at, tz)} with {selected.provider}
            </p>
            <button className={button} disabled={busy} onClick={() => confirm(info)}>{busy ? 'Booking…' : 'Book this time'}</button>
          </div>
        )}
      </Shell>
    )
  }

  const { booked } = stage
  return (
    <Shell clinic={booked.clinic}>
      <div className="flex h-12 w-12 items-center justify-center rounded-full bg-teal-100 text-2xl text-teal-800 dark:bg-teal-950 dark:text-teal-200" aria-hidden>✓</div>
      <h1 className="mt-4 text-2xl font-semibold">You're booked</h1>
      <div className="mt-5 rounded-2xl border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900">
        <p className="text-lg font-semibold">{longDay(booked.starts_at, booked.timezone)}</p>
        <p className="text-3xl font-semibold text-teal-700 dark:text-teal-400">{timeOf(booked.starts_at, booked.timezone)}</p>
        <p className="mt-3 text-sm text-slate-600 dark:text-slate-300">with {booked.provider}</p>
        <p className="text-sm text-slate-600 dark:text-slate-300">{booked.clinic}</p>
      </div>
      <p className="mt-5 text-sm text-slate-600 dark:text-slate-300">We sent you a confirmation text and will remind you the day before. To change or cancel, just reply to that text.</p>
    </Shell>
  )
}
