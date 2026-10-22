export type Conversation = {
  id: string
  phone: string
  patient: string | null
  state: string
  outcome: string | null
  verified: boolean
  phase: string
  last_message: string | null
  updated_at: string | null
}

export type Message = {
  id: string
  sender: 'patient' | 'agent' | 'staff'
  body: string
  at: string
  delivery: string | null
  delivery_error: string | null
}
export type AuditEntry = { action: string; actor: string; detail: Record<string, unknown> | null; at: string }

export type Escalation = {
  id: string
  conversation_id: string
  reason: string
  status: 'open' | 'taken' | 'resolved'
  assigned_to: string | null
  created_at: string
  resolved_at: string | null
  phone?: string
  patient?: string | null
}

export type ConversationDetail = Conversation & {
  langsmith_trace_id: string | null
  messages: Message[]
  escalations: Escalation[]
  audit: AuditEntry[]
}

export type Summary = {
  conversations: number
  by_state: Record<string, number>
  by_outcome: Record<string, number>
  open_escalations: number
  completion_rate: number | null
  escalation_rate: number | null
}

export type Metrics = {
  cases: number
  passed: number
  pass_rate: number
  booking_success_rate: number | null
  task_cases: number
  escalation_precision: number | null
  escalation_recall: number | null
  verification_bypass_cases: number
  info_leak_cases: number
  structured_output_valid_rate: number | null
  avg_turns_to_complete: number | null
  avg_seconds_per_case: number
  by_category: Record<string, { passed: number; total: number }>
  failure_categories: Record<string, number>
}

export type AppointmentRow = {
  id: string
  patient: string
  phone: string
  type: string
  provider: string
  starts_at: string
  status: string
  confirmed: boolean
  confirmed_at: string | null
  conversation_id: string | null
}

export type PatientRow = {
  id: string
  name: string
  phone: string
  opted_out: boolean
  next_appointment: string | null
  last_outreach_at: string | null
}

export type EvalRunSummary = { label: string; model: string; created_at: string; metrics: Metrics }
export type CaseResult = {
  id: string
  category: string
  passed: boolean
  failure_category: string | null
  failure_detail: string | null
  replies: string[]
}
export type EvalRun = EvalRunSummary & { results: CaseResult[] }

const TOKEN_KEY = 'clinicScheduler.token'

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? ''
  } catch {
    return ''
  }
}

export function setToken(value: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, value)
  } catch {
    /* storage unavailable: the token just will not be remembered */
  }
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken()
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
  })
  if (!response.ok) {
    let detail = response.statusText
    try {
      detail = (await response.json()).detail ?? detail
    } catch {
      /* body was not JSON */
    }
    throw new ApiError(response.status, detail)
  }
  return response.json() as Promise<T>
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })

export const api = {
  summary: () => request<Summary>('/summary'),
  conversations: (state?: string) =>
    request<Conversation[]>(`/conversations${state ? `?state=${encodeURIComponent(state)}` : ''}`),
  conversation: (id: string) => request<ConversationDetail>(`/conversations/${id}`),
  reply: (id: string, body: string, staff_name: string) =>
    post<{ id: string }>(`/conversations/${id}/reply`, { body, staff_name }),
  escalate: (id: string, reason: string, staff_name: string) =>
    post<Escalation>(`/conversations/${id}/escalate`, { reason, staff_name }),
  escalations: (status = 'active') => request<Escalation[]>(`/escalations?status=${status}`),
  take: (id: string, staff_name: string) => post<Escalation>(`/escalations/${id}/take`, { staff_name }),
  resolve: (id: string) => post<Escalation>(`/escalations/${id}/resolve`),
  appointments: (day: string, status: string) => {
    const params = new URLSearchParams({ status })
    if (day) params.set('day', day)
    return request<AppointmentRow[]>(`/appointments?${params}`)
  },
  patients: () => request<PatientRow[]>('/patients'),
  textPatient: (id: string, staff_name: string) =>
    post<{ conversation_id: string }>(`/patients/${id}/outreach`, { staff_name }),
  evalRuns: () => request<EvalRunSummary[]>('/evals'),
  evalRun: (label: string) => request<EvalRun>(`/evals/${encodeURIComponent(label)}`),
}
