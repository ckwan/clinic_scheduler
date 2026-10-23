"""Texts for the booking link flow. Hand written, like the agent's replies.

They say "yearly visit" and not "annual wellness visit": a text sits on a lock screen, so it should
reveal as little about care as it can. Each carries the opt-out line.
"""

LINK_TEXT = (
    "Hi {first_name}, it's time to schedule your yearly visit at {clinic}. "
    "Pick a time here: {url} (link expires in {days} days). Reply STOP to opt out."
)
NUDGE_TEXT = (
    "Reminder from {clinic}: your yearly visit still needs a time. "
    "Pick one here: {url} Reply STOP to opt out."
)
CONFIRMED_TEXT = "You're booked: {label}. See you then! Reply STOP to opt out."

APPOINTMENT_NAMES = {
    "annual_wellness_visit": "Yearly wellness visit",
    "checkup": "Checkup",
    "therapy_session": "Therapy session",
}
