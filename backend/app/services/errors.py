class SchedulingError(Exception):
    """Base class. `code` is a stable machine readable value the agent can act on."""

    code = "scheduling_error"

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)
        self.message = message or self.code


class NotVerified(SchedulingError):
    code = "not_verified"


class SlotUnavailable(SchedulingError):
    code = "slot_unavailable"


class AppointmentNotFound(SchedulingError):
    code = "appointment_not_found"


class InvalidRequest(SchedulingError):
    code = "invalid_request"
