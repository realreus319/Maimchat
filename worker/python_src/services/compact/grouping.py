from __future__ import annotations

from collections.abc import Sequence

from ...query import AssistantMessage, Message


def group_messages_by_api_round(
    messages: Sequence[Message],
) -> tuple[tuple[Message, ...], ...]:
    groups: list[tuple[Message, ...]] = []
    current: list[Message] = []
    last_assistant_id: str | None = None

    for message in messages:
        assistant_id = (
            message.message.id if isinstance(message, AssistantMessage) else None
        )
        if assistant_id is not None and assistant_id != last_assistant_id and current:
            groups.append(tuple(current))
            current = [message]
        else:
            current.append(message)
        if assistant_id is not None:
            last_assistant_id = assistant_id

    if current:
        groups.append(tuple(current))
    return tuple(groups)
