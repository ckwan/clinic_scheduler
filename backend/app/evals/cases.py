"""Eval case format and loading.

A case is a script of patient messages plus what must be true afterwards. Cases are plain JSON
in evals/cases/, one file per category, each holding a list.
"""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_CASES_DIR = Path(__file__).resolve().parents[3] / "evals" / "cases"

Sender = Literal["taylor", "riley", "casey", "unknown"]
Group = Literal["therapy", "pcp"]


class AppointmentRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patient: Literal["taylor", "riley", "casey"]
    provider: Group = "therapy"
    slot: int = Field(ge=0)

    def key(self) -> tuple[str, str, int]:
        return (self.patient, self.provider, self.slot)


class Event(BaseModel):
    """Something that happens in the world just before a patient message is sent."""

    model_config = ConfigDict(extra="forbid")

    before_turn: int = Field(ge=0)
    book: AppointmentRef  # another patient takes this slot


class Setup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sender: Sender = "taylor"
    verified: bool = False  # conversation already passed identity verification
    bookings: list[AppointmentRef] = []  # appointments that exist before the first message
    # We already texted a reminder for this booking, so the patient's next message answers it.
    reminder_for: AppointmentRef | None = None


class Expect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    escalated: bool = False
    escalation_reason: str | None = None
    # "unchanged": the set of booked appointments equals the starting set.
    # A list: the set of booked appointments afterwards is exactly this.
    end_state: Literal["unchanged"] | list[AppointmentRef] = "unchanged"
    # Booked appointments the patient confirmed attendance for. Anything else must be unconfirmed.
    confirmed: list[AppointmentRef] = []
    reply_contains: list[str] = []
    reply_not_contains: list[str] = []


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: str
    description: str = ""
    setup: Setup = Setup()
    turns: list[str] = Field(min_length=1)
    events: list[Event] = []
    expect: Expect = Expect()

    @property
    def expects_change(self) -> bool:
        """True for cases where the agent is supposed to complete a booking task."""
        return self.expect.end_state != "unchanged" and not self.expect.escalated


def load_cases(directory: Path | None = None) -> list[EvalCase]:
    directory = directory or DEFAULT_CASES_DIR
    cases: list[EvalCase] = []
    for path in sorted(directory.glob("*.json")):
        for raw in json.loads(path.read_text()):
            case = EvalCase.model_validate(raw)
            if case.category != path.stem:
                raise ValueError(f"{case.id}: category '{case.category}' does not match file {path.name}")
            cases.append(case)

    ids = [c.id for c in cases]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"duplicate case ids: {sorted(duplicates)}")
    return cases
