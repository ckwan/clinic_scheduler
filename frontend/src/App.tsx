import { useEffect, useState } from 'react'
import { getToken, setToken } from './api'
import ConversationDetail from './pages/ConversationDetail'
import Appointments from './pages/Appointments'
import Conversations from './pages/Conversations'
import Evals from './pages/Evals'
import Overview from './pages/Overview'
import Patients from './pages/Patients'
import Queue from './pages/Queue'

const NAV = [
  ['#/', 'Overview'],
  ['#/appointments', 'Appointments'],
  ['#/patients', 'Patients'],
  ['#/queue', 'Needs a human'],
  ['#/conversations', 'Conversations'],
  ['#/evals', 'Evals'],
] as const

function useHash() {
  const [hash, setHash] = useState(window.location.hash || '#/')
  useEffect(() => {
    const on = () => setHash(window.location.hash || '#/')
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  return hash
}

function read(key: string, fallback: string) {
  try {
    return localStorage.getItem(key) ?? fallback
  } catch {
    return fallback
  }
}

export default function App() {
  const hash = useHash()
  const [staffName, setStaffName] = useState(() => read('clinicScheduler.staff', 'staff'))
  const [token, setTokenState] = useState(getToken)

  const detail = hash.match(/^#\/conversations\/([\w-]+)$/)
  let page
  if (detail) page = <ConversationDetail key={detail[1]} id={detail[1]} staffName={staffName} />
  else if (hash.startsWith('#/appointments')) page = <Appointments />
  else if (hash.startsWith('#/patients')) page = <Patients staffName={staffName} />
  else if (hash.startsWith('#/queue')) page = <Queue staffName={staffName} />
  else if (hash.startsWith('#/conversations')) page = <Conversations />
  else if (hash.startsWith('#/evals')) page = <Evals />
  else page = <Overview />

  return (
    <div className="mx-auto max-w-5xl px-4 pb-16">
      <header className="flex flex-wrap items-center justify-between gap-3 py-5">
        <div>
          <h1 className="text-lg font-semibold">Clinic Scheduler</h1>
          <p className="text-xs text-slate-500">Synthetic data only</p>
        </div>
        <div className="flex gap-2 text-xs">
          <input aria-label="Your name" value={staffName} onChange={(e) => { setStaffName(e.target.value); try { localStorage.setItem('clinic_scheduler.staff', e.target.value) } catch { /* not remembered */ } }} className="w-24 rounded-lg border border-slate-300 bg-transparent px-2 py-1 dark:border-slate-700" />
          <input aria-label="API token" type="password" placeholder="API token" value={token} onChange={(e) => { setTokenState(e.target.value); setToken(e.target.value) }} onBlur={() => window.location.reload()} className="w-28 rounded-lg border border-slate-300 bg-transparent px-2 py-1 dark:border-slate-700" />
        </div>
      </header>
      <nav className="mb-6 flex gap-1 overflow-x-auto border-b border-slate-200 dark:border-slate-800" aria-label="Main">
        {NAV.map(([href, text]) => {
          const active = href === '#/' ? hash === '#/' : hash.startsWith(href)
          return (
            <a key={href} href={href} aria-current={active ? 'page' : undefined} className={`whitespace-nowrap border-b-2 px-3 py-2 text-sm ${active ? 'border-sky-600 font-medium text-sky-700 dark:text-sky-400' : 'border-transparent text-slate-500 hover:text-slate-900 dark:hover:text-slate-200'}`}>
              {text}
            </a>
          )
        })}
      </nav>
      <main>{page}</main>
    </div>
  )
}
