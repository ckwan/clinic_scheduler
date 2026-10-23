"""A rule based stand in for the model: keywords and regexes, no API calls.

It exists so the eval harness and CI run offline, deterministically, and
for free, and so there is a floor to compare the real model against. It cannot read times
("Friday", "after 8am") or names without a date of birth next to them, so it is expected to fail
those cases. Every result file records which model produced it.
"""

import re
from datetime import date
from typing import Any

from app.agent.schemas import Understanding

MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1
    )
}

CLINICAL = re.compile(
    r"\b(pain|symptoms?|medic(?:ation|ine)s?|ibuprofen|sertraline|dos(?:e|age)|prescription|"
    r"diagnos\w*|rash|fever|lab results|test results|blood tests?|side effects?|overdose|bleeding)\b"
)
HUMAN = re.compile(
    r"\b(real person|human|speak (?:to|with) (?:a |some)?(?:one|person|someone|staff|representative)|"
    r"talk to (?:a |some)?(?:person|someone|human)|call me|someone call|front desk|representative)\b"
)
FRUSTRATED = re.compile(
    r"\b(ridiculous|useless|stupid|terrible|worst|furious|fed up|waste of time|nobody helps|unacceptable)\b"
)
CANCEL = re.compile(r"\bcancel")
RESCHEDULE = re.compile(r"\b(reschedul\w*|move (?:my|her|his|the)|change my appointment|different time)\b")
LIST = re.compile(r"\b(what appointments|my appointments|upcoming|do i have|when is my)\b")
BOOK = re.compile(r"\b(book|schedule|appointment|therapy|therapist|check-?up|wellness|physical|see a)\b")
SLASH_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")
WORD_DATE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? (\d{1,2}),? (\d{4})\b", re.I)
NAME_STOPWORDS = {
    "hi", "hello", "hey", "this", "is", "my", "name", "im", "i'm", "i", "am", "born", "dob", "on",
    "date", "of", "birth", "birthday", "its", "it's", "the", "and", "please",
}
YES = re.compile(r"^\s*(yes|yeah|yep|yup|sure|ok|okay|confirm|y)\b")
NO = re.compile(r"^\s*(no|nope|nah|n)\b")
ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3}


def _find_dob(text: str) -> tuple[date | None, int]:
    """Return (date, start index of the match)."""
    match = SLASH_DATE.search(text)
    try:
        if match:
            month, day, year = (int(g) for g in match.groups())
            if year < 100:
                year += 1900 if year > 30 else 2000
            return date(year, month, day), match.start()
        match = WORD_DATE.search(text)
        if match:
            return date(int(match.group(3)), MONTHS[match.group(1).lower()[:3]], int(match.group(2))), match.start()
    except ValueError:
        pass
    return None, len(text)


def _find_name(text: str, dob_start: int) -> tuple[str | None, str | None]:
    before = re.sub(r"[^A-Za-z' ]", " ", text[:dob_start])
    words = [w for w in before.split() if w.lower() not in NAME_STOPWORDS]
    if len(words) < 2:
        return None, None
    return words[-2].capitalize(), words[-1].capitalize()


def _find_option(low: str) -> int | None:
    match = re.match(r"^\s*(?:option\s*)?#?([1-9])\b", low)
    if match:
        return int(match.group(1))
    for word, number in ORDINALS.items():
        if re.search(rf"\b{word}\b", low):
            return number
    return None


def _appointment_type(low: str) -> str | None:
    if "wellness" in low:
        return "annual_wellness_visit"
    if re.search(r"check-?up|physical", low):
        return "checkup"
    if re.search(r"therap|counsel", low):
        return "therapy_session"
    return None


class HeuristicLLM:
    def understand(self, message: str, context: dict[str, Any]) -> Understanding:
        low = message.lower()
        phase = context.get("phase", "idle")

        dob, dob_start = _find_dob(message)
        first, last = _find_name(message, dob_start) if dob else (None, None)
        option = _find_option(low) if phase in ("awaiting_slot_choice", "awaiting_appointment_choice") else None
        confirmation = None
        if phase in ("awaiting_confirmation", "awaiting_reminder_response"):
            confirmation = "yes" if YES.match(low) else "no" if NO.match(low) else None

        if CANCEL.search(low):
            intent = "cancel"
        elif RESCHEDULE.search(low):
            intent = "reschedule"
        elif LIST.search(low):
            intent = "list"
        elif BOOK.search(low):
            intent = "book"
        else:
            intent = "other"

        engaged = bool(option or confirmation or dob or phase != "idle")
        return Understanding(
            intent=intent,
            confidence=0.9 if intent != "other" or engaged else 0.3,
            asks_clinical_question=bool(CLINICAL.search(low)),
            wants_human=bool(HUMAN.search(low)),
            frustrated=bool(FRUSTRATED.search(low)),
            appointment_type=_appointment_type(low),
            first_name=first,
            last_name=last,
            date_of_birth=dob,
            chosen_option=option,
            confirmation=confirmation,
        )
