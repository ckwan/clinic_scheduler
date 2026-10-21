"""Pure state machine for a conversation's timers. No Temporal imports, so it is unit tested
directly. The workflow asks it what to wait for and what to do when the wait ends."""

from dataclasses import dataclass

PENDING_PHASES = {
    "awaiting_verification", "awaiting_type", "awaiting_slot_choice",
    "awaiting_confirmation", "awaiting_appointment_choice",
}


@dataclass(frozen=True)
class Step:
    wait_s: int
    on_timeout: str  # "nudge" or "close"


class ConversationTimeline:
    def __init__(self, nudge_after_s, close_pending_after_s, close_idle_after_s, close_human_after_s):
        self.nudge_after_s = nudge_after_s
        self.close_pending_after_s = close_pending_after_s
        self.close_idle_after_s = close_idle_after_s
        self.close_human_after_s = close_human_after_s
        self.nudged = False

    def on_message(self) -> None:
        """Any new inbound message resets the nudge."""
        self.nudged = False

    def next_step(self, phase: str, escalated: bool) -> Step:
        if escalated:
            return Step(self.close_human_after_s, "close")
        if phase in PENDING_PHASES:
            if not self.nudged:
                return Step(self.nudge_after_s, "nudge")
            return Step(max(self.close_pending_after_s - self.nudge_after_s, 0), "close")
        return Step(self.close_idle_after_s, "close")

    def on_nudge_sent(self) -> None:
        self.nudged = True
