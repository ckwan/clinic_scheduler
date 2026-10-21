"""Patient facing text. Written by hand, not by the model, so every reply is predictable and
nothing in it can be invented. The model only helps read the patient's message."""

ASK_IDENTITY = (
    "Before I can help, I need to confirm who you are. "
    "Please send your first name, last name, and date of birth (MM/DD/YYYY)."
)
ASK_IDENTITY_MISSING = "Thanks. I still need your {missing}."
VERIFY_FAILED = (
    "Those details did not match our records. "
    "Please send your first name, last name, and date of birth (MM/DD/YYYY) again."
)
VERIFIED_NO_ACTION = (
    "Thanks, you're verified. Would you like to book, change, or cancel an appointment?"
)
CLARIFY = (
    "I can help you book, change, or cancel an appointment. "
    "For example: \"I'd like to book a therapy session.\""
)
ASK_TYPE = "What kind of appointment: a therapy session, a checkup, or an annual wellness visit?"

NO_UPCOMING = "I don't see any upcoming appointments for you."
NO_AVAILABILITY = "I couldn't find any openings in that window. Would a different day or time work?"
NOT_A_CHOICE = "Please reply with the number of one of the options."
KEPT = "Okay, I've kept your appointment as is."
NO_PROBLEM = "No problem."
SLOT_TAKEN = "Sorry, that time was just taken."

ESCALATION = {
    "clinical_question": (
        "I can't give medical advice. If this is an emergency, call 911. "
        "I've asked a staff member to follow up with you."
    ),
    "requested_human": "Of course. I've asked a staff member to follow up with you.",
    "frustrated": "I'm sorry about the trouble. I've asked a staff member to follow up with you.",
    "verification_failed": (
        "I couldn't verify your details, so I've asked a staff member to follow up with you."
    ),
    "low_confidence": "I'm having trouble understanding. I've asked a staff member to follow up.",
    "llm_error": "I'm having trouble right now. I've asked a staff member to follow up with you.",
    "tool_error": "Something went wrong on my end. I've asked a staff member to follow up with you.",
}


REMINDER_DECLINED = (
    "Okay. Reply RESCHEDULE to pick a new time, or tell me you want to cancel the appointment."
)
REMINDER_STALE = "That appointment is no longer booked. I can book a new one if you'd like."

# Sent by the workflows (reminders and nudges), not in reply to a message.
NUDGES = {
    "awaiting_verification": (
        "Just checking in. To continue, please send your first name, last name, "
        "and date of birth (MM/DD/YYYY)."
    ),
    "awaiting_type": f"Just checking in. {ASK_TYPE}",
    "awaiting_slot_choice": (
        "Just checking in. Reply with a number to pick a time, or tell me what would work better."
    ),
    "awaiting_confirmation": "Just checking in. Reply YES to confirm, or NO to pick another time.",
    "awaiting_appointment_choice": "Just checking in. Reply with the number of the appointment.",
}


def reminder(label: str) -> str:
    return (
        f"Reminder: you have a {label}. "
        "Reply CONFIRM to confirm or RESCHEDULE to pick a new time."
    )


def attendance_confirmed(label: str) -> str:
    return f"Thanks, you're confirmed for {label}. See you then!"


def numbered(options: list[str]) -> str:
    return "\n".join(f"{i}. {label}" for i, label in enumerate(options, start=1))


def offer_slots(options: list[str]) -> str:
    return f"Here are some openings:\n{numbered(options)}\nReply with a number to pick one."


def choose_appointment(options: list[str]) -> str:
    return f"Which appointment?\n{numbered(options)}"


def confirm_book(label: str) -> str:
    return f"Book {label}? Reply YES to confirm, or NO to pick another time."


def confirm_reschedule(label: str) -> str:
    return f"Move your appointment to {label}? Reply YES to confirm, or NO to pick another time."


def confirm_cancel(label: str) -> str:
    return f"Cancel your {label}? Reply YES to confirm, or NO to keep it."


def booked(label: str) -> str:
    return f"You're booked: {label}. See you then!"


def rescheduled(label: str) -> str:
    return f"Done. Your appointment is now {label}."


def cancelled(label: str) -> str:
    return f"Your {label} is cancelled."


def upcoming(options: list[str]) -> str:
    return f"Your upcoming appointments:\n{numbered(options)}"


# First text sent by staff from the dashboard to start a conversation. The opt-out line is
# there because carriers and US rules expect it on the first message of a program.
OUTREACH = "Hi, would you like to book an appointment? Reply with what you need. Reply STOP to opt out."
