"""Reminds a patient about one appointment. It re-reads the appointment each time it wakes,
so a reschedule or cancel needs no signal: the workflow just sees the new state."""

import asyncio
from datetime import datetime, timedelta, timezone

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.workflows.models import AppointmentInfo, ReminderInput, ReminderPlan

DB = timedelta(seconds=30)
MAX_SLEEP = timedelta(hours=6)  # wake regularly to notice a moved appointment
SMS_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2), backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5), maximum_attempts=6,
)


@workflow.defn
class AppointmentReminderWorkflow:
    @workflow.run
    async def run(self, params: ReminderInput) -> str:
        aid = params.appointment_id
        remind_before = timedelta(seconds=params.remind_before_s)

        while True:
            info = await workflow.execute_activity(
                "get_appointment_info", aid, result_type=AppointmentInfo, start_to_close_timeout=DB
            )
            if info.status != "booked":
                return "appointment_" + info.status
            starts_at = datetime.fromisoformat(info.starts_at_iso).astimezone(timezone.utc)
            wait = starts_at - remind_before - workflow.now()
            if wait <= timedelta(0):
                break
            # Sleep, then loop to re-check: the appointment may have moved or been cancelled.
            await asyncio.sleep(min(wait, MAX_SLEEP).total_seconds())

        if starts_at <= workflow.now():
            return "too_late"

        plan = await workflow.execute_activity(
            "prepare_reminder", aid, result_type=ReminderPlan, start_to_close_timeout=DB
        )
        if plan is None:
            return "skipped"
        await workflow.execute_activity(
            "send_sms", args=[plan.to_number, plan.body, plan.message_id],
            start_to_close_timeout=timedelta(seconds=30), retry_policy=SMS_RETRY,
        )

        await asyncio.sleep(params.confirm_wait_s)
        flagged = await workflow.execute_activity(
            "flag_unconfirmed", aid, result_type=bool, start_to_close_timeout=DB
        )
        return "unconfirmed" if flagged else "confirmed_or_changed"
