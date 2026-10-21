"""Run with `make worker`. Hosts both workflows and all activities."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

from temporalio.worker import Worker

from app.agent.providers import make_llm
from app.channels.sms import get_sms_sender
from app.config import settings
from app.db import SessionLocal
from app.workflows.activities import Activities
from app.workflows.client import connect
from app.workflows.conversation import ConversationWorkflow
from app.workflows.reminder import AppointmentReminderWorkflow


def build_worker(client, activities: Activities, executor) -> Worker:
    return Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[ConversationWorkflow, AppointmentReminderWorkflow],
        activities=activities.all(),
        activity_executor=executor,
    )


async def main() -> None:
    client = await connect()
    activities = Activities(SessionLocal, make_llm(), get_sms_sender())
    with ThreadPoolExecutor(max_workers=8) as executor:
        await build_worker(client, activities, executor).run()


if __name__ == "__main__":
    asyncio.run(main())
