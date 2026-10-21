"""Unit tests run on in memory SQLite so they need no Docker. Postgres only specific bits
(JSONB) are mapped to JSON here. The partial unique index is declared for both dialects."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import models
from app.db import Base
from app.services.conversations import get_or_create_conversation

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):
    return "JSON"


@dataclass
class World:
    clinic: models.Clinic
    other_clinic: models.Clinic
    therapist: models.Provider
    pcp: models.Provider
    taylor: models.Patient
    riley: models.Patient
    therapy_slots: list[models.Slot]  # future, open, ordered by start
    past_slot: models.Slot
    pcp_slot: models.Slot
    other_clinic_slot: models.Slot


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as s:
        yield s


@pytest.fixture()
def world(session) -> World:
    clinic = models.Clinic(name="Test Clinic", phone_number="+15555550100")
    other_clinic = models.Clinic(name="Other Clinic", phone_number="+15555550200")
    session.add_all([clinic, other_clinic])
    session.flush()

    therapist = models.Provider(clinic_id=clinic.id, name="Dr. Chen", specialty="therapy")
    pcp = models.Provider(clinic_id=clinic.id, name="Dr. Rivera", specialty="primary_care")
    other_therapist = models.Provider(clinic_id=other_clinic.id, name="Dr. Far", specialty="therapy")
    session.add_all([therapist, pcp, other_therapist])

    taylor = models.Patient(
        clinic_id=clinic.id,
        first_name="Taylor",
        last_name="Morgan",
        date_of_birth=date(1988, 4, 12),
        phone_number="+15555550111",
    )
    riley = models.Patient(
        clinic_id=clinic.id,
        first_name="Riley",
        last_name="Brooks",
        date_of_birth=date(1992, 9, 3),
        phone_number="+15555550112",
    )
    session.add_all([taylor, riley])
    session.flush()

    def slot(provider, offset_hours):
        start = NOW + timedelta(hours=offset_hours)
        return models.Slot(
            provider_id=provider.id, starts_at=start, ends_at=start + timedelta(minutes=50)
        )

    therapy_slots = [slot(therapist, 24), slot(therapist, 25), slot(therapist, 48)]
    past_slot = slot(therapist, -24)
    pcp_slot = slot(pcp, 30)
    other_clinic_slot = slot(other_therapist, 26)
    session.add_all([*therapy_slots, past_slot, pcp_slot, other_clinic_slot])
    session.flush()

    return World(
        clinic, other_clinic, therapist, pcp, taylor, riley,
        therapy_slots, past_slot, pcp_slot, other_clinic_slot,
    )


@pytest.fixture()
def conversation(session, world):
    """An unverified conversation from Taylor's phone number."""
    return get_or_create_conversation(session, world.clinic.id, world.taylor.phone_number)


@pytest.fixture()
def verified_conversation(session, conversation):
    conversation.verified = True
    session.flush()
    return conversation


@pytest.fixture()
def riley_conversation(session, world):
    convo = get_or_create_conversation(session, world.clinic.id, world.riley.phone_number)
    convo.verified = True
    session.flush()
    return convo


@pytest.fixture(autouse=True)
def _no_signature_by_default(monkeypatch):
    """Most tests post unsigned requests. Signature tests turn validation back on."""
    from app.config import settings

    monkeypatch.setattr(settings, "twilio_validate_signature", False)
