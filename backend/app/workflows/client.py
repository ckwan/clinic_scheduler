"""Starting and signalling conversation workflows from the webhook."""

from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy

from app.config import settings
from app.workflows.conversation import ConversationWorkflow, workflow_id
from app.workflows.models import ConversationInput, InboundMessage


async def connect() -> Client:
    return await Client.connect(settings.temporal_address)


async def deliver_inbound(
    client: Client, conversation_id: str, clinic_id: str, body: str, sid: str | None
) -> None:
    """Signal-with-start: creates the workflow if needed, else signals the running one."""
    await client.start_workflow(
        ConversationWorkflow.run,
        ConversationInput(conversation_id=conversation_id, clinic_id=clinic_id),
        id=workflow_id(conversation_id),
        task_queue=settings.temporal_task_queue,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        start_signal="inbound_message",
        start_signal_args=[InboundMessage(body=body, sid=sid)],
    )


async def human_takeover(client: Client, conversation_id: str) -> None:
    await client.get_workflow_handle(workflow_id(conversation_id)).signal(ConversationWorkflow.human_takeover)
