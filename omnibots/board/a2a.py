"""Agent-to-agent tools with CORAL's hub rule (PLAN.md ADR-10, A4.a.06).

  send_message(to, text)      -> an A2A_MESSAGE on #bot/<to>, addressed to <to>
  wait_for_mention(timeout)   -> blocks on the bot's inbox (no polling) and
                                 returns whatever arrived for it
  submit(result)              -> boss only: the final result (TASK_COMPLETED)

The hub rule is enforced here, not just in the prompt: the boss may message
anyone, but a worker can only message the boss. The user (via the UI) is a
node on the hub too: user messages to a bot arrive in the same inbox.
"""

from __future__ import annotations

import contextlib
from typing import Any

from omnibots.board.bus import MessageBus, Subscription
from omnibots.board.types import topic_bot, topic_project
from omnibots.runtime.tools import Tool, ToolContext

INBOX_TYPES = {"A2A_MESSAGE", "TASK_ASSIGNED", "QUESTION", "CLAIM_SUBMITTED", "CLAIM_ACCEPTED", "CLAIM_REJECTED", "HELP_REQUEST",
               "TASK_COMPLETED", "TASK_FAILED",   # a worker finishing (or failing) lands in the boss's inbox
               "TOOL_REQUEST"}                    # A8.d.02: found live in A14.a.02, Omi never heard a bot ask for a tool


class Inbox:
    """A bot's mailbox: everything addressed to it. Subscribe BEFORE the bot starts."""

    def __init__(self, sub: Subscription):
        self.sub = sub

    @classmethod
    async def open(cls, bus: MessageBus, bot_id: str, since_id: int | None = None) -> "Inbox":
        return cls(await bus.subscribe(recipient=bot_id, types=INBOX_TYPES, since_id=since_id))

    def close(self) -> None:
        self.sub.close()


def fmt(m) -> str:
    who = m.sender_id or m.sender_type
    extra = f" (claim #{m.payload['claim_id']})" if "claim_id" in m.payload else ""
    return f"[{m.message_type} from {who}{extra}] {m.text()}"


def a2a_tools(bus: MessageBus, inbox: Inbox, *, bot_id: str, boss_id: str, project_id: str | None = None,
              is_active=None) -> list[Tool]:
    is_boss = bot_id == boss_id

    async def send_message(args: dict[str, Any], ctx: ToolContext) -> str:
        to = str(args.get("to") or "").strip()
        text = str(args.get("text") or args.get("content") or "").strip()
        if not to or not text:
            return "ERROR: send_message needs 'to' and 'text'"
        if not is_boss and to != boss_id:
            return f"ERROR: workers can only message the boss ({boss_id}). Send it to {boss_id} and ask it to relay."
        await bus.publish(topic_bot(to), "A2A_MESSAGE", {"text": text}, sender_type="bot", sender_id=bot_id,
                          recipient_id=to, job_id=ctx.job_id, project_id=project_id)
        if is_active is not None and to != boss_id and not is_active(to):
            return (f"sent to {to}. {to} is not inside a job right now; they keep reading the board and will see this. "
                    "assign_job still starts them on a ready job immediately.")
        return f"sent to {to}"

    async def wait_for_mention(args: dict[str, Any], ctx: ToolContext) -> str:
        # No 30-minute ceiling. Time spent here does not spend the goal's work budget.
        timeout = float(args.get("timeout_seconds") or 120)
        await ctx.event("state", "sleeping: waiting for a message")
        hold = ctx.waiting_on_user() if ctx.waiting_on_user else contextlib.nullcontext()
        with hold:
            first = await inbox.sub.get(timeout=timeout)
        if first is None:
            return f"no messages arrived (waited {timeout:.0f}s)"
        got = [first, *inbox.sub.drain()]
        return "\n".join(fmt(m) for m in got)

    async def submit(args: dict[str, Any], ctx: ToolContext) -> str:
        result = str(args.get("result") or "").strip()
        if not result:
            return "ERROR: submit needs 'result'"
        topic = topic_project(project_id) if project_id else topic_bot(bot_id)
        await bus.publish(topic, "TASK_COMPLETED", {"result": result}, sender_type="bot", sender_id=bot_id,
                          job_id=ctx.job_id, project_id=project_id)
        return "result submitted"

    tools = [
        Tool("send_message",
             f"Send a message to another bot. {'You are the boss: you may message any bot.' if is_boss else f'You may only message the boss, {boss_id}.'}",
             {"type": "object", "properties": {"to": {"type": "string", "description": "bot id"}, "text": {"type": "string"}}, "required": ["to", "text"]},
             "R0", send_message, path_arg=None, summary=lambda a: f"message → {a.get('to')}: {str(a.get('text', ''))[:60]}"),
        Tool("wait_for_mention",
             "Wait (without busy-looping) until a message addressed to you arrives, then return it.",
             {"type": "object", "properties": {"timeout_seconds": {"type": "integer"}}},
             "R0", wait_for_mention, timeout=7 * 24 * 3600, path_arg=None, summary=lambda a: "wait_for_mention"),
    ]
    if is_boss:
        tools.append(Tool("submit", "Boss only: submit the final result for the user's goal.",
                          {"type": "object", "properties": {"result": {"type": "string"}}, "required": ["result"]},
                          "R0", submit, path_arg=None, summary=lambda a: f"submit: {str(a.get('result', ''))[:60]}"))
    return tools


async def steering_pump(bus: MessageBus, bot) -> None:
    """Deliver the user's USER_STEER board messages to a running bot (A11.c.03).
    Run it as a task next to the bot; cancel it when the bot stops."""
    sub = await bus.subscribe({topic_bot(bot.bot_id)}, types={"USER_STEER"})
    try:
        while True:
            m = await sub.get()
            if m is not None:
                bot.steer(str(m.payload.get("text", "")))
    finally:
        sub.close()
