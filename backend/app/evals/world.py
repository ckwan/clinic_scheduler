"""A small, fixed, synthetic clinic that every eval case starts from.

The clock is frozen at Wed 2026-10-21 12:00 UTC (5:00 AM in Los Angeles) so "tomorrow",
"Friday", and "next Monday" mean the same thing on every run.

Therapy slots (Dr. Chen), clinic local time:
  t0 Thu Oct 22 5:00 AM   t1 Thu 6:00 AM   t2 Thu 9:00 AM
  t3 Fri Oct 23 5:00 AM   t4 Fri 9:00 AM
  t5 Mon Oct 26 5:00 AM   t6 Mon 9:00 AM
Primary care slots (Dr. Rivera):
  p0 Thu Oct 22 10:00 AM  p1 Fri Oct 23 10:00 AM  p2 Tue Oct 27 10:00 AM
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app import models

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
CLINIC_PHONE = "+15555550100"
UNKNOWN_PHONE = "+15557770000"

PATIENT_SPECS = {
    "taylor": ("Taylor", "Morgan", date(1988, 4, 12), "+15555550111"),
    "riley": ("Riley", "Brooks", date(1992, 9, 3), "+15555550112"),
    "casey": ("Casey", "Nguyen", date(1979, 1, 25), "+15555550113"),
}

THERAPY_OFFSET_HOURS = [24, 25, 28, 48, 52, 120, 124]
PCP_OFFSET_HOURS = [29, 53, 149]


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # lets the Postgres schema run on in-memory SQLite
    return "JSON"


@dataclass
class EvalWorld:
    clinic: models.Clinic
    patients: dict[str, models.Patient]
    slots: dict[str, list[models.Slot]]  # "therapy" and "pcp"
    slot_keys: dict[UUID, tuple[str, int]]  # slot id -> (group, index)

    def phone_for(self, sender: str) -> str:
        return UNKNOWN_PHONE if sender == "unknown" else self.patients[sender].phone_number

    def patient_key(self, patient_id: UUID) -> str:
        for key, patient in self.patients.items():
            if patient.id == patient_id:
                return key
        return "?"


def build_world(session: Session) -> EvalWorld:
    clinic = models.Clinic(name="Eval Clinic", phone_number=CLINIC_PHONE)
    session.add(clinic)
    session.flush()

    therapist = models.Provider(clinic_id=clinic.id, name="Dr. Chen", specialty="therapy")
    pcp = models.Provider(clinic_id=clinic.id, name="Dr. Rivera", specialty="primary_care")
    session.add_all([therapist, pcp])

    patients = {}
    for key, (first, last, dob, phone) in PATIENT_SPECS.items():
        patients[key] = models.Patient(
            clinic_id=clinic.id, first_name=first, last_name=last, date_of_birth=dob, phone_number=phone
        )
    session.add_all(patients.values())
    session.flush()

    def make_slots(provider, offsets):
        slots = []
        for hours in offsets:
            start = NOW + timedelta(hours=hours)
            slots.append(
                models.Slot(
                    provider_id=provider.id, starts_at=start, ends_at=start + timedelta(minutes=50)
                )
            )
        return slots

    slots = {
        "therapy": make_slots(therapist, THERAPY_OFFSET_HOURS),
        "pcp": make_slots(pcp, PCP_OFFSET_HOURS),
    }
    session.add_all([*slots["therapy"], *slots["pcp"]])
    session.flush()

    slot_keys = {s.id: (group, i) for group, group_slots in slots.items() for i, s in enumerate(group_slots)}
    return EvalWorld(clinic, patients, slots, slot_keys)


def book_directly(
    session: Session, world: EvalWorld, patient: str, group: str, index: int, key: str
) -> models.Appointment:
    """Put an appointment in the database without going through the agent.
    Used for case setup and for 'someone else grabbed it' events."""
    slot = world.slots[group][index]
    appointment = models.Appointment(
        patient_id=world.patients[patient].id,
        slot_id=slot.id,
        appointment_type="therapy_session" if group == "therapy" else "checkup",
        idempotency_key=key,
    )
    slot.status = "booked"
    session.add(appointment)
    session.flush()
    return appointment


def snapshot(session: Session) -> dict[str, tuple[str, str, str]]:
    """appointment id -> (patient id, slot id, status)"""
    return {
        str(a.id): (str(a.patient_id), str(a.slot_id), a.status)
        for a in session.scalars(select(models.Appointment))
    }
