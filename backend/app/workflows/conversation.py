"""One workflow per conversation: serializes messages, owns the nudge and close timers, and
starts a reminder workflow for each new booking."""

import asyncio
from collections import deque
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from app.workflows.models import (
        ConversationInput, InboundMessage, ReminderInput, ReminderPlan, TurnResult,
    )
    from app.workflows.timeline import ConversationTimeline

DB_TIMEOUT = timedelta(seconds=30)
# The model call can be slow on a local machine, so give it room.
AGENT_TIMEOUT = timedelta(minutes=3)
SMS_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2), backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5), maximum_attempts=6,
)
AGENT_RETRY = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=2))


def workflow_id(conversation_id: str) -> str:
    return f"conversation-{conversation_id}"


@workflow.defn
class ConversationWorkflow:
    def __init__(self) -> None:
        self._inbox: deque[InboundMessage] = deque()
        self._human = False
        self._human_handled = False

    @workflow.signal
    def inbound_message(self, message: InboundMessage) -> None:
        self._inbox.append(message)

    @workflow.signal
    def human_takeover(self) -> None:
        self._human = True

    async def _send(
        self, conversation_id: str, to_number: str, body: str, message_id: str | None = None
    ) -> None:
        try:
            await workflow.execute_activity(
                "send_sms", args=[to_number, body, message_id],
                start_to_close_timeout=timedelta(seconds=30), retry_policy=SMS_RETRY,
            )
        except Exception as exc:  # retries exhausted: leave a trace instead of failing silently
            await workflow.execute_activity(
                "record_event", args=[conversation_id, "sms_send_failed", {"error": str(exc)[:200]}],
                start_to_close_timeout=DB_TIMEOUT,
            )

    @workflow.run
    async def run(self, params: ConversationInput) -> str:
        cid = params.conversation_id
        timeline = ConversationTimeline(
            params.nudge_after_s, params.close_pending_after_s,
            params.close_idle_after_s, params.close_human_after_s,
        )
        phase, escalated = "idle", False

        while True:
            # Process everything that has arrived, one message at a time, in order.
            while self._inbox:
                message = self._inbox.popleft()
                timeline.on_message()
                result = await workflow.execute_activity(
                    "process_message", args=[cid, message], result_type=TurnResult,
                    start_to_close_timeout=AGENT_TIMEOUT, retry_policy=AGENT_RETRY,
                )
                phase, escalated = result.phase, result.escalated
                if result.duplicate:
                    continue
                if result.reply:
                    await self._send(cid, result.to_number, result.reply, result.reply_message_id)
                for appointment_id in result.new_appointment_ids:
                    await workflow.start_child_workflow(
                        "AppointmentReminderWorkflow", ReminderInput(appointment_id),
                        id=f"reminder-{appointment_id}",
                        parent_close_policy=ParentClosePolicy.ABANDON,
                        id_reuse_policy=workflow.WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                    )

            if self._human and not self._human_handled:
                self._human_handled = True
                escalated = True
                await workflow.execute_activity(
                    "record_event", args=[cid, "human_takeover", {}], start_to_close_timeout=DB_TIMEOUT
                )

            step = timeline.next_step(phase, escalated)
            try:
                await workflow.wait_condition(
                    lambda: bool(self._inbox) or (self._human and not self._human_handled),
                    timeout=timedelta(seconds=step.wait_s),
                )
                continue
            except asyncio.TimeoutError:
                pass

            if step.on_timeout == "nudge":
                plan = await workflow.execute_activity(
                    "prepare_nudge", cid, result_type=ReminderPlan, start_to_close_timeout=DB_TIMEOUT
                )
                timeline.on_nudge_sent()
                if plan:
                    await self._send(cid, plan.to_number, plan.body, plan.message_id)
                    await workflow.execute_activity(
                        "record_event", args=[cid, "nudge_sent", {"phase": phase}],
                        start_to_close_timeout=DB_TIMEOUT,
                    )
                continue

            reason = "abandoned" if phase != "idle" else "idle_timeout"
            if escalated:
                reason = "escalated"
            await workflow.execute_activity(
                "close_conversation", args=[cid, reason], start_to_close_timeout=DB_TIMEOUT
            )
            return reason

