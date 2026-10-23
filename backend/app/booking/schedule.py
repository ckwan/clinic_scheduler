"""Create the daily campaign schedule: `python -m app.booking.schedule` (or `make wellness-schedule`).

Safe to run again; it leaves an existing schedule alone."""

import asyncio

from temporalio.client import Schedule, ScheduleActionStartWorkflow, ScheduleSpec, ScheduleAlreadyRunningError
from temporalio.service import RPCError

from app.booking.workflows import CampaignInput, WellnessCampaignWorkflow
from app.config import settings
from app.workflows.client import connect

SCHEDULE_ID = "wellness-campaign-daily"


async def main() -> None:
    client = await connect()
    try:
        await client.create_schedule(
            SCHEDULE_ID,
            Schedule(
                action=ScheduleActionStartWorkflow(
                    WellnessCampaignWorkflow.run,
                    CampaignInput(),
                    id="wellness-campaign",
                    task_queue=settings.temporal_task_queue,
                ),
                # 17:00 UTC is 10am Pacific: patients get texts at a sensible hour.
                spec=ScheduleSpec(cron_expressions=["0 17 * * *"]),
            ),
        )
        print(f"Created schedule {SCHEDULE_ID} (daily 17:00 UTC)")
    except (ScheduleAlreadyRunningError, RPCError) as exc:
        print(f"Schedule not created: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
