"""The conversation graph. One run handles one inbound message.

    understand -> plan -> escalate | clarify | verify -> flow

- understand: the model reads the message into a validated `Understanding`
- plan: plain code decides what to do (safety checks first, then verification, then the task)
- verify: collects name and date of birth across turns, then calls the verify tool
- flow: book, reschedule, cancel, and list, as a small state machine kept in `agent_state`
- escalate / clarify: hand off to a human, or ask what the patient wants

The model never chooses an action and never writes patient facing text. Every data change goes
through `execute_tool`, which enforces the guardrails in the service layer.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

from app.agent import replies
from app.agent.llm import LLM, LLMError
from app.agent.schemas import Understanding
from app.models import Clinic, Conversation
from app.services.conversations import escalate
from app.tools.scheduling import ToolResult, execute_tool

CONFIDENCE_FLOOR = 0.4
MAX_UNCLEAR_TURNS = 2
FLOW_ACTIONS = ("book", "reschedule", "cancel", "list")
PENDING_PHASES = ("awaiting_slot_choice", "awaiting_confirmation", "awaiting_appointment_choice")
OFFER_LIMIT = 3

_FALLBACK = Understanding(
    intent="other",
    confidence=0,
    asks_clinical_question=False,
    wants_human=False,
    frustrated=False,
)


@dataclass
class AgentContext:
    session: Session
    clinic: Clinic
    conversation: Conversation
    llm: LLM
    now: datetime
    agent_state: dict

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.clinic.timezone)


class TurnState(TypedDict, total=False):
    message: str
    understanding: Understanding
    llm_error: str
    flow: str | None
    escalate_reason: str
    prefix: str
    reply: str | None


def _ctx(config: RunnableConfig) -> AgentContext:
    return config["configurable"]["ctx"]


def _tool(ctx: AgentContext, name: str, args: dict[str, Any]) -> ToolResult:
    return execute_tool(ctx.session, ctx.conversation, name, args, now=ctx.now)


def format_time(iso: str, tz: ZoneInfo) -> str:
    local = datetime.fromisoformat(iso).astimezone(tz)
    hour = local.hour % 12 or 12
    suffix = "AM" if local.hour < 12 else "PM"
    return f"{local:%a %b} {local.day}, {hour}:{local:%M} {suffix}"


def _reset_flow(a: dict) -> None:
    for key in ("phase", "offered", "listed", "pending", "target"):
        a.pop(key, None)


def _finish(a: dict) -> None:
    """A task is done: forget it so a later 'yes' cannot trigger anything."""
    _reset_flow(a)
    for key in ("intent", "flow_action", "appointment_type"):
        a.pop(key, None)
    a["phase"] = "idle"


# ------------------------------------------------------------------ nodes


def _llm_context(ctx: AgentContext) -> dict[str, Any]:
    a = ctx.agent_state
    phase = a.get("phase", "idle")
    options: list[str] = []
    if phase == "awaiting_slot_choice":
        options = [o["label"] for o in a.get("offered", [])]
    elif phase == "awaiting_appointment_choice":
        options = [o["label"] for o in a.get("listed", [])]
    return {
        "today": ctx.now.astimezone(ctx.tz).strftime("%A %Y-%m-%d"),
        "timezone": ctx.clinic.timezone,
        "verified": ctx.conversation.verified,
        "phase": phase,
        "numbered_options": options,
        "appointment_type_so_far": a.get("appointment_type"),
    }


def understand_node(state: TurnState, config: RunnableConfig) -> TurnState:
    ctx = _ctx(config)
    try:
        return {"understanding": ctx.llm.understand(state["message"], _llm_context(ctx))}
    except LLMError as exc:
        return {"understanding": _FALLBACK, "llm_error": str(exc)}


def _active_action(a: dict, u: Understanding) -> str | None:
    answering = u.chosen_option is not None or u.confirmation is not None
    if u.intent != "other" and not answering:
        return u.intent
    if a.get("phase") in PENDING_PHASES and answering:
        return a.get("flow_action")
    return u.intent if u.intent != "other" else a.get("intent")


def plan_node(state: TurnState, config: RunnableConfig) -> TurnState:
    ctx = _ctx(config)
    a = ctx.agent_state
    u = state["understanding"]

    # Safety first, before anything else is considered.
    if state.get("llm_error"):
        return {"flow": "escalate", "escalate_reason": "llm_error"}
    if u.asks_clinical_question:
        return {"flow": "escalate", "escalate_reason": "clinical_question"}
    if u.wants_human:
        return {"flow": "escalate", "escalate_reason": "requested_human"}
    if u.frustrated:
        return {"flow": "escalate", "escalate_reason": "frustrated"}

    # Replying to a reminder we sent: yes or no about attending needs no identity check.
    if a.get("phase") == "awaiting_reminder_response" and u.confirmation is not None:
        a["unclear"] = 0
        return {"flow": "confirm"}

    has_identity = bool(a.get("verify")) or bool(u.first_name or u.last_name or u.date_of_birth)
    answering = u.chosen_option is not None or u.confirmation is not None
    in_flow = a.get("phase") in (*PENDING_PHASES, "awaiting_verification") or answering or has_identity
    if u.intent != "other":
        a["intent"] = u.intent
    if u.appointment_type:
        a["appointment_type"] = u.appointment_type  # remembered even before verification

    unclear = u.confidence < CONFIDENCE_FLOOR or (
        u.intent == "other" and not in_flow and not a.get("intent")
    )
    if unclear:
        a["unclear"] = a.get("unclear", 0) + 1
        if a["unclear"] >= MAX_UNCLEAR_TURNS:
            return {"flow": "escalate", "escalate_reason": "low_confidence"}
        return {"flow": "clarify"}
    a["unclear"] = 0

    if not ctx.conversation.verified:
        return {"flow": "verify"}
    return {"flow": _active_action(a, u) or "clarify"}


def route_plan(state: TurnState) -> str:
    return state["flow"] if state["flow"] in ("escalate", "clarify", "verify", "confirm") else "flow"


def escalate_node(state: TurnState, config: RunnableConfig) -> TurnState:
    ctx = _ctx(config)
    reason = state["escalate_reason"]
    escalate(ctx.session, ctx.conversation, reason)
    _reset_flow(ctx.agent_state)
    return {"reply": replies.ESCALATION[reason]}


def clarify_node(state: TurnState, config: RunnableConfig) -> TurnState:
    ctx = _ctx(config)
    if ctx.conversation.verified:
        return {"reply": replies.VERIFIED_NO_ACTION if not ctx.agent_state.get("intent") else replies.CLARIFY}
    return {"reply": replies.CLARIFY}


def confirm_node(state: TurnState, config: RunnableConfig) -> TurnState:
    """The patient answered a reminder we sent: CONFIRM or no."""
    ctx = _ctx(config)
    a = ctx.agent_state
    u = state["understanding"]
    pending = a.get("pending") or {}

    if u.confirmation == "no":
        a["phase"] = "idle"
        a.pop("pending", None)
        return {"reply": replies.REMINDER_DECLINED}

    result = _tool(ctx, "confirm_attendance", {"appointment_id": pending.get("appointment_id")})
    if result.ok:
        a["phase"] = "idle"
        a.pop("pending", None)
        return {"reply": replies.attendance_confirmed(pending.get("label", "your appointment"))}
    if result.error_code in ("appointment_not_found", "invalid_request"):
        a["phase"] = "idle"
        a.pop("pending", None)
        return {"reply": replies.REMINDER_STALE}
    return {"reply": _tool_failed(ctx, result)}


def verify_node(state: TurnState, config: RunnableConfig) -> TurnState:
    ctx = _ctx(config)
    a = ctx.agent_state
    u = state["understanding"]

    details = a.setdefault("verify", {})
    if u.first_name:
        details["first_name"] = u.first_name
    if u.last_name:
        details["last_name"] = u.last_name
    if u.date_of_birth:
        details["date_of_birth"] = u.date_of_birth.isoformat()

    missing = [
        label
        for key, label in (
            ("first_name", "first name"),
            ("last_name", "last name"),
            ("date_of_birth", "date of birth"),
        )
        if key not in details
    ]
    if missing:
        a["phase"] = "awaiting_verification"
        if len(missing) == 3:
            return {"flow": None, "reply": replies.ASK_IDENTITY}
        return {"flow": None, "reply": replies.ASK_IDENTITY_MISSING.format(missing=" and ".join(missing))}

    result = _tool(ctx, "verify_identity", dict(details))
    a.pop("verify", None)
    if result.ok:
        a["phase"] = "idle"
        action = _active_action(a, u)
        if action is None:
            return {"flow": None, "reply": replies.VERIFIED_NO_ACTION}
        return {"flow": action, "prefix": "Thanks, you're verified."}

    if result.data and result.data.get("escalated"):
        _reset_flow(a)
        return {"flow": None, "reply": replies.ESCALATION["verification_failed"]}
    a["phase"] = "awaiting_verification"
    return {"flow": None, "reply": replies.VERIFY_FAILED}


def route_after_verify(state: TurnState) -> str:
    return "flow" if state.get("flow") in FLOW_ACTIONS else END


# ------------------------------------------------------------------ the task flows


def _aware(ctx: AgentContext, value: datetime) -> datetime:
    """The model reads times in the clinic's timezone; an offset-less value means local time."""
    return value if value.tzinfo else value.replace(tzinfo=ctx.tz)


def _offer_slots(ctx: AgentContext, u: Understanding) -> str:
    a = ctx.agent_state
    appointment_type = a.get("appointment_type") or (a.get("target") or {}).get("type")
    args: dict[str, Any] = {"appointment_type": appointment_type, "limit": OFFER_LIMIT}
    if u.preferred_after:
        args["earliest"] = _aware(ctx, u.preferred_after).isoformat()
    if u.preferred_before:
        args["latest"] = _aware(ctx, u.preferred_before).isoformat()

    result = _tool(ctx, "check_availability", args)
    if not result.ok:
        return _tool_failed(ctx, result)

    slots = result.data["slots"]
    if not slots:
        a.pop("offered", None)
        a["phase"] = "idle"
        return replies.NO_AVAILABILITY

    a["offered"] = [
        {
            "slot_id": s["slot_id"],
            "label": f"{format_time(s['starts_at'], ctx.tz)} with {s['provider']}",
        }
        for s in slots
    ]
    a["phase"] = "awaiting_slot_choice"
    return replies.offer_slots([o["label"] for o in a["offered"]])


def _tool_failed(ctx: AgentContext, result: ToolResult) -> str:
    escalate(ctx.session, ctx.conversation, "tool_error")
    _reset_flow(ctx.agent_state)
    return replies.ESCALATION["tool_error"]


def _appointment_label(ctx: AgentContext, item: dict) -> str:
    kind = item["appointment_type"].replace("_", " ")
    return f"{kind} on {format_time(item['starts_at'], ctx.tz)} with {item['provider']}"


def _find_target(ctx: AgentContext) -> str | None:
    """Pick the appointment to change. Returns a reply to send, or None once a target is set."""
    a = ctx.agent_state
    result = _tool(ctx, "list_appointments", {})
    if not result.ok:
        return _tool_failed(ctx, result)

    items = [
        {
            "appointment_id": i["appointment_id"],
            "type": i["appointment_type"],
            "label": _appointment_label(ctx, i),
        }
        for i in result.data["appointments"]
    ]
    if not items:
        _finish(a)
        return replies.NO_UPCOMING
    if len(items) == 1:
        a["target"] = items[0]
        return None
    a["listed"] = items
    a["phase"] = "awaiting_appointment_choice"
    return replies.choose_appointment([i["label"] for i in items])


def _on_confirmation(ctx: AgentContext, said_yes: bool, u: Understanding) -> str:
    a = ctx.agent_state
    pending = a["pending"]

    if not said_yes:
        if pending["action"] == "cancel":
            _finish(a)
            return replies.KEPT
        a.pop("pending", None)
        return f"{replies.NO_PROBLEM} {_offer_slots(ctx, u)}"

    if pending["action"] == "book":
        result = _tool(
            ctx,
            "book_appointment",
            {
                "slot_id": pending["slot_id"],
                "appointment_type": a["appointment_type"],
                "idempotency_key": f"{ctx.conversation.id}-{pending['slot_id']}",
            },
        )
        done = replies.booked(pending["label"])
    elif pending["action"] == "reschedule":
        result = _tool(
            ctx,
            "reschedule_appointment",
            {"appointment_id": pending["appointment_id"], "new_slot_id": pending["slot_id"]},
        )
        done = replies.rescheduled(pending["label"])
    else:
        result = _tool(ctx, "cancel_appointment", {"appointment_id": pending["appointment_id"]})
        done = replies.cancelled(pending["label"])

    if result.ok:
        _finish(a)
        return done
    if result.error_code == "slot_unavailable":
        a.pop("pending", None)
        return f"{replies.SLOT_TAKEN} {_offer_slots(ctx, u)}"
    return _tool_failed(ctx, result)


def flow_node(state: TurnState, config: RunnableConfig) -> TurnState:
    ctx = _ctx(config)
    a = ctx.agent_state
    u = state["understanding"]
    action = state["flow"]

    if a.get("flow_action") != action:
        _reset_flow(a)
        a["flow_action"] = action
    if u.appointment_type:
        a["appointment_type"] = u.appointment_type
    phase = a.get("phase")

    if action == "list":
        result = _tool(ctx, "list_appointments", {})
        if not result.ok:
            return {"reply": _tool_failed(ctx, result)}
        labels = [_appointment_label(ctx, i) for i in result.data["appointments"]]
        _finish(a)
        return {"reply": replies.upcoming(labels) if labels else replies.NO_UPCOMING}

    # Answering "yes" or "no" to a confirmation question.
    if phase == "awaiting_confirmation" and u.confirmation:
        return {"reply": _on_confirmation(ctx, u.confirmation == "yes", u)}

    # Picking which appointment to change.
    if phase == "awaiting_appointment_choice":
        n = u.chosen_option
        listed = a.get("listed", [])
        if n is None or not 1 <= n <= len(listed):
            return {"reply": replies.NOT_A_CHOICE}
        a["target"] = listed[n - 1]
        a.pop("listed", None)

    # Cancel and reschedule need to know which appointment first.
    if action in ("cancel", "reschedule") and "target" not in a:
        reply = _find_target(ctx)
        if reply:
            return {"reply": reply}

    # Picking one of the offered slots.
    if a.get("phase") == "awaiting_slot_choice" and u.chosen_option is not None:
        offered = a.get("offered", [])
        n = u.chosen_option
        if not 1 <= n <= len(offered):
            return {"reply": f"{replies.NOT_A_CHOICE}\n{replies.numbered([o['label'] for o in offered])}"}
        chosen = offered[n - 1]
        a["pending"] = {"action": action, "slot_id": chosen["slot_id"], "label": chosen["label"]}
        if action == "reschedule":
            a["pending"]["appointment_id"] = a["target"]["appointment_id"]
        a["phase"] = "awaiting_confirmation"
        confirm = replies.confirm_reschedule if action == "reschedule" else replies.confirm_book
        return {"reply": confirm(chosen["label"])}

    if action == "cancel":
        target = a["target"]
        a["pending"] = {
            "action": "cancel",
            "appointment_id": target["appointment_id"],
            "label": target["label"],
        }
        a["phase"] = "awaiting_confirmation"
        return {"reply": replies.confirm_cancel(target["label"])}

    if action == "book" and not a.get("appointment_type"):
        a["phase"] = "awaiting_type"
        return {"reply": replies.ASK_TYPE}

    return {"reply": _offer_slots(ctx, u)}


# ------------------------------------------------------------------ graph


def build_graph():
    graph = StateGraph(TurnState)
    graph.add_node("understand", understand_node)
    graph.add_node("plan", plan_node)
    graph.add_node("escalate", escalate_node)
    graph.add_node("clarify", clarify_node)
    graph.add_node("verify", verify_node)
    graph.add_node("confirm", confirm_node)
    graph.add_node("flow", flow_node)

    graph.add_edge(START, "understand")
    graph.add_edge("understand", "plan")
    graph.add_conditional_edges(
        "plan",
        route_plan,
        {
            "escalate": "escalate",
            "clarify": "clarify",
            "verify": "verify",
            "confirm": "confirm",
            "flow": "flow",
        },
    )
    graph.add_conditional_edges("verify", route_after_verify, {"flow": "flow", END: END})
    graph.add_edge("escalate", END)
    graph.add_edge("clarify", END)
    graph.add_edge("confirm", END)
    graph.add_edge("flow", END)
    return graph.compile()


GRAPH = build_graph()
