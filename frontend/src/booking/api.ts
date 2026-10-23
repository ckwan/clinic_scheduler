// Patient booking page API. Public: the link token is the credential, so no dashboard token is sent.

export type LinkInfo =
  | { status: 'ok'; first_name: string; clinic: string; appointment_type: string; appointment_name: string }
  | { status: 'invalid' | 'expired' | 'used'; message: string }

export type Slot = { slot_id: string; starts_at: string; provider: string }
export type Slots = { timezone: string; slots: Slot[] }
export type Booked = { appointment_id: string; starts_at: string; provider: string; clinic: string; timezone: string; label: string }

export class BookingError extends Error {
  code: string
  attemptsRemaining?: number
  constructor(code: string, message: string, attemptsRemaining?: number) {
    super(message)
    this.code = code
    this.attemptsRemaining = attemptsRemaining
  }
}

async function call<T>(token: string, path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/public/booking/${encodeURIComponent(token)}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json' },
  })
  if (response.ok) return response.json()
  let detail: { code?: string; message?: string; attempts_remaining?: number } = {}
  try {
    const body = await response.json()
    detail = typeof body.detail === 'object' && body.detail ? body.detail : {}
  } catch {
    /* not JSON */
  }
  if (response.status === 429) throw new BookingError('rate_limited', 'Too many tries. Please wait a minute.')
  throw new BookingError(
    detail.code ?? 'error',
    detail.message ?? 'Something went wrong. Please try again.',
    detail.attempts_remaining,
  )
}

export const booking = {
  open: (token: string) => call<LinkInfo>(token, ''),
  slots: (token: string, date_of_birth: string) =>
    call<Slots>(token, '/slots', { method: 'POST', body: JSON.stringify({ date_of_birth }) }),
  book: (token: string, date_of_birth: string, slot_id: string) =>
    call<Booked>(token, '/book', { method: 'POST', body: JSON.stringify({ date_of_birth, slot_id }) }),
}
