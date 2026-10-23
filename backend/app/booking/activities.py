"""Temporal activities for the wellness campaign. Side effects only; the workflow decides."""

from uuid import UUID

from temporalio import activity

from app.booking import outreach
from app.channels.sms import SmsSender
from app.models import Patient
from app.services.conversations import utcnow


class BookingActivities:
    def __init__(self, session_factory, sms: SmsSender):
        self._session_factory = session_factory
        self._sms = sms

    @activity.defn
    def find_due_patients(self, limit: int) -> list[str]:
        with self._session_factory() as session:
            return [str(p.id) for p in outreach.find_due_patients(session, utcnow(), limit)]

    @activity.defn
    def send_booking_link(self, patient_id: str, kind: str) -> str:
        """'sent', or 'skipped:<reason>' when a guard stopped it. Raises on a provider failure so
        Temporal retries; the retry is safe because nothing is recorded before the text is sent."""
        with self._session_factory() as session:
            patient = session.get(Patient, UUID(patient_id))
            try:
                outreach.send_booking_link(session, self._sms, patient, kind=kind, actor="workflow")
            except outreach.NotSent as exc:
                return f"skipped:{exc.reason}"
            session.commit()
            return "sent"

    @activity.defn
    def booking_link_outcome(self, patient_id: str) -> str:
        with self._session_factory() as session:
            return outreach.link_outcome(session, session.get(Patient, UUID(patient_id)))

    def all(self):
        return [self.find_due_patients, self.send_booking_link, self.booking_link_outcome]
