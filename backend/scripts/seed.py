"""Seed synthetic clinic data. Run from backend/: python -m scripts.seed"""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Clinic, Patient, Provider, Slot

CLINIC_PHONE = "+15555550100"

PROVIDERS = [
    ("Dr. Avery Chen", "therapy"),
    ("Dr. Jordan Patel", "therapy"),
    ("Dr. Sam Rivera", "primary_care"),
]

PATIENTS = [
    ("Taylor", "Morgan", date(1988, 4, 12), "+15555550111"),
    ("Riley", "Brooks", date(1992, 9, 3), "+15555550112"),
    ("Casey", "Nguyen", date(1979, 1, 25), "+15555550113"),
    ("Jamie", "Ortiz", date(2000, 6, 30), "+15555550114"),
    ("Alex", "Kim", date(1965, 11, 8), "+15555550115"),
]


def seed() -> None:
    with SessionLocal() as session:
        if session.scalar(select(Clinic).where(Clinic.phone_number == CLINIC_PHONE)):
            print("Already seeded")
            return

        clinic = Clinic(name="Sunrise Wellness (synthetic)", phone_number=CLINIC_PHONE)
        session.add(clinic)
        session.flush()

        providers = [Provider(clinic_id=clinic.id, name=n, specialty=s) for n, s in PROVIDERS]
        session.add_all(providers)
        session.add_all(
            [
                Patient(
                    clinic_id=clinic.id,
                    first_name=f,
                    last_name=l,
                    date_of_birth=dob,
                    phone_number=phone,
                )
                for f, l, dob, phone in PATIENTS
            ]
        )
        session.flush()

        # Open 50 minute slots, 9 to 4 on weekdays, for the next 14 days.
        start_day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        for provider in providers:
            for day in range(1, 15):
                date_ = start_day + timedelta(days=day)
                if date_.weekday() >= 5:
                    continue
                for hour in range(16, 23):  # 9 to 4 Pacific, stored in UTC
                    starts = date_.replace(hour=hour)
                    session.add(
                        Slot(
                            provider_id=provider.id,
                            starts_at=starts,
                            ends_at=starts + timedelta(minutes=50),
                        )
                    )
        session.commit()
        print("Seeded synthetic clinic, providers, patients, and slots")


if __name__ == "__main__":
    seed()
