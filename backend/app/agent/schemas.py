from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.tools.scheduling import AppointmentType


class Understanding(BaseModel):
    """What the model read out of one patient message.

    This is the only thing the model produces. It never writes the reply text and never
    calls a tool; the graph decides what happens next from these fields.
    """

    intent: Literal["book", "reschedule", "cancel", "list", "other"]
    confidence: float = Field(ge=0, le=1, description="How sure you are about the intent.")
    asks_clinical_question: bool = Field(
        description="Asks for medical advice, symptoms, medication, diagnosis, or test results."
    )
    wants_human: bool = Field(description="Asks to speak to a person or staff member.")
    frustrated: bool = Field(description="Angry, abusive, or clearly losing patience.")

    appointment_type: AppointmentType | None = None
    first_name: str | None = None
    last_name: str | None = None
    date_of_birth: date | None = None
    chosen_option: int | None = Field(
        default=None, description="1 based number of the option the patient picked, if any."
    )
    confirmation: Literal["yes", "no"] | None = Field(
        default=None, description="Only when answering a yes or no confirmation question."
    )
    preferred_after: datetime | None = None
    preferred_before: datetime | None = None
