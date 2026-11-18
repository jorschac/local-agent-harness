from __future__ import annotations
from .errors import HarnessError


def validate_messages(messages: list[dict], *, session: bool = False) -> list[dict]:
    if session and (len(messages) != 1 or messages[0]["role"] != "user"):
        raise HarnessError("A session request must contain exactly one new user message", "invalid_messages", param="messages")
    if not messages or messages[-1]["role"] != "user":
        raise HarnessError("The final input message must be user", "invalid_messages", param="messages")
    seen_conversation = False
    previous = None
    for message in messages:
        role, content = message.get("role"), message.get("content")
        if set(message) != {"role", "content"} or role not in ("system", "developer", "user", "assistant") or not isinstance(content, str) or not content.strip():
            raise HarnessError("Only nonempty plain-text messages are supported", "invalid_messages", param="messages")
        if role in ("system", "developer"):
            if seen_conversation:
                raise HarnessError("Instruction messages must precede conversation", "invalid_messages", param="messages")
        else:
            if not seen_conversation and role != "user":
                raise HarnessError("Conversation must start with user", "invalid_messages", param="messages")
            if previous == role:
                raise HarnessError("Conversation roles must alternate", "invalid_messages", param="messages")
            seen_conversation, previous = True, role
    return [dict(message) for message in messages]
