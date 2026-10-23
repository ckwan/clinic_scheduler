"""Temporal workflows for the yearly visit campaign.

WellnessCampaignWorkflow  runs on a schedule (daily). Finds patients who are due and starts one
                          outreach workflow each. The workflow id includes the year, so a patient
                          is started at most once a year even if the campaign runs every day.
WellnessOutreachWorkflow  one patient: text the link, wait, and if they have neither booked nor
                          opted out, text one nudge. Then stop. Two texts at most per campaign.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

DB = timedelta(seconds=30)
SMS_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2), backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5), maximum_attempts=6,
)


@dataclass
class CampaignInput:
    limit: int = 100  # per run, so a first run cannot text the whole clinic at once


@dataclass
class OutreachInput:
    patient_id: str
    nudge_after_s: int = 3 * 24 * 3600


@workflow.defn
class WellnessOutreachWorkflow:
    @workflow.run
    async def run(self, params: OutreachInput) -> str:
        pid = params.patient_id
        sent = await workflow.execute_activity(
            "send_booking_link", args=[pid, "initial"], result_type=str,
            start_to_close_timeout=timedelta(seconds=60), retry_policy=SMS_RETRY,
        )
        if sent != "sent":
            return sent

        await asyncio.sleep(params.nudge_after_s)
        outcome = await workflow.execute_activity(
            "booking_link_outcome", pid, result_type=str, start_to_close_timeout=DB
        )
        if outcome != "pending":
            return outcome  # booked, opted_out, or the link was revoked: no nudge

        nudged = await workflow.execute_activity(
            "send_booking_link", args=[pid, "nudge"], result_type=str,
            start_to_close_timeout=timedelta(seconds=60), retry_policy=SMS_RETRY,
        )
        return "nudged" if nudged == "sent" else nudged


@workflow.defn
class WellnessCampaignWorkflow:
    @workflow.run
    async def run(self, params: CampaignInput) -> int:
        patient_ids = await workflow.execute_activity(
            "find_due_patients", params.limit, result_type=list[str], start_to_close_timeout=DB
        )
        year = workflow.now().year
        started = 0
        for pid in patient_ids:
            try:
                await workflow.start_child_workflow(
                    "WellnessOutreachWorkflow", OutreachInput(pid),
                    id=f"wellness-{pid}-{year}",
                    parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                )
                started += 1
            except WorkflowAlreadyStartedError:
                continue  # already texted this year
        return started
