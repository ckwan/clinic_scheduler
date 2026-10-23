import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import BookingPage from './booking/BookingPage.tsx'

// /book/<token> is the patient page opened from a text. Everything else is the staff dashboard.
const bookingToken = window.location.pathname.match(/^\/book\/([^/]+)\/?$/)?.[1]

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {bookingToken ? <BookingPage token={bookingToken} /> : <App />}
  </StrictMode>,
)
