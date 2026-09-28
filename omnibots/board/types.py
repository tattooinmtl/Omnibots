"""Board message types, topics and payload rules (PLAN.md §4.3, A4.a.02)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# §4.3 plus A2A_MESSAGE (CORAL send_message) and SEAT_WAITING (the waiting list, A4.b.01).
MESSAGE_TYPES = {
    "TASK_RECEIVED", "TASK_PLANNED", "BOT_CREATED", "SEAT_WAITING", "SEAT_GRANTED", "SEAT_RELEASED",
    "TASK_ASSIGNED", "WORK_STARTED", "PROGRESS_UPDATE", "CLAIM_SUBMITTED", "CLAIM_ACCEPTED", "CLAIM_REJECTED",
    "HELP_REQUEST", "QUESTION", "BLOCKED", "ARTIFACT_READY", "COUNCIL_OPENED", "COUNCIL_VERDICT",
    "REVIEW_RESULT", "APPROVAL_REQUEST", "APPROVAL_DECISION", "TOOL_CREATED", "PLAYBOOK_UPDATED",
    "LOCK_ACQUIRED", "LOCK_RELEASED", "USER_STEER", "TASK_COMPLETED", "TASK_FAILED", "SYSTEM_RESET",
    "A2A_MESSAGE", "TOOL_REQUEST", "TOOL_RESULT",           # the tool relay (A8.d.02)
}

# Minimal payload contracts: these keys must be present.
REQUIRED = {
    "A2A_MESSAGE": {"text"},
    "USER_STEER": {"text"},
    "TASK_ASSIGNED": {"title"},
    "CLAIM_SUBMITTED": {"claim_id", "text"},
    "CLAIM_ACCEPTED": {"claim_id"},
    "CLAIM_REJECTED": {"claim_id", "reason"},
    "LOCK_ACQUIRED": {"resource"},
    "LOCK_RELEASED": {"resource"},
    "SEAT_WAITING": {"position"},
    "SEAT_GRANTED": {"seat"},
    "SEAT_RELEASED": {"seat"},
    "APPROVAL_REQUEST": {"approval_id", "summary"},
    "APPROVAL_DECISION": {"approval_id", "approved"},
    "TASK_COMPLETED": {"result"},
    "TASK_FAILED": {"error"},
    "BLOCKED": {"reason"},
    "QUESTION": {"text"},
    "HELP_REQUEST": {"text"},
    "TOOL_REQUEST": {"request_id", "text"},
    "TOOL_RESULT": {"request_id", "text"},
}

ERROR_TYPES = {"TASK_FAILED", "BLOCKED", "CLAIM_REJECTED"}
SENDER_TYPES = {"bot", "user", "system"}

GENERAL, ORCHESTRATOR, APPROVALS, COUNCIL = "#general", "#orchestrator", "#approvals", "#council"


def topic_bot(bot_id: str) -> str:
    return f"#bot/{bot_id}"


def topic_job(job_id: str) -> str:
    return f"#job/{job_id}"


def topic_project(project_id: str) -> str:
    return f"#project/{project_id}"


class BoardError(ValueError):
    pass


@dataclass
class Message:
    id: int
    topic: str
    sender_type: str
    sender_id: str | None
    message_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    recipient_id: str | None = None
    job_id: str | None = None
    project_id: str | None = None
    created_at: str = ""

    @classmethod
    def from_row(cls, r) -> "Message":
        return cls(id=r["id"], topic=r["topic"], sender_type=r["sender_type"], sender_id=r["sender_id"],
                   message_type=r["message_type"], payload=json.loads(r["payload_json"] or "{}"),
                   recipient_id=r["recipient_id"], job_id=r["job_id"], project_id=r["project_id"], created_at=r["created_at"])

    def text(self) -> str:
        p = self.payload
        for k in ("text", "result", "summary", "reason", "error", "title", "resource"):
            if p.get(k) not in (None, ""):
                return str(p[k])
        return json.dumps(p, ensure_ascii=False) if p else ""


def validate(message_type: str, sender_type: str, payload: dict[str, Any]) -> None:
    if message_type not in MESSAGE_TYPES:
        raise BoardError(f"unknown message type {message_type!r}")
    if sender_type not in SENDER_TYPES:
        raise BoardError(f"unknown sender type {sender_type!r}")
    missing = REQUIRED.get(message_type, set()) - set(payload)
    if missing:
        raise BoardError(f"{message_type} payload is missing {sorted(missing)}")
