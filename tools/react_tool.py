#!/usr/bin/env python3
"""React Tool — let the agent react to the current message with an emoji.

A lightweight, communicative alternative to sending a text reply: acknowledge
a message in a busy thread without interjecting, answer a simple yes/no/confirm
with a 👍, or react on request. Deliberately react-only — it can NOT send
arbitrary messages (that guardrail lives in send_message_tool, which stays out
of the agent loop).

Like ``clarify``, the actual platform work is done by a callback the agent
runner injects (bound in gateway/run.py with the current chat + adapter). Off
the gateway (CLI/cron) there is no callback, so the tool reports it is
unavailable rather than doing anything.
"""

import json
from typing import Optional, Callable

from tools.registry import registry, tool_error


def react_tool(
    emoji: str = "",
    message_id: Optional[str] = None,
    remove: bool = False,
    callback: Optional[Callable] = None,
) -> str:
    """Add (or with ``remove=True`` retract) an emoji reaction on the message
    currently being replied to.

    Args:
        emoji: Emoji name without colons (e.g. ``thumbsup``, ``+1``, ``pray``).
               Required unless ``remove`` is set.
        message_id: Platform message id/ts to react to. Omit to target the most
                    recent message in the current chat (the one being replied to).
        remove: Retract a reaction instead of adding one.
        callback: Platform-injected handler; signature
                  ``callback(emoji, message_id, remove) -> dict``.
    """
    emoji = (emoji or "").strip().strip(":")
    if not remove and not emoji:
        return tool_error("emoji is required to react.")

    if callback is None:
        return json.dumps(
            {"error": "Reactions are not available in this execution context."},
            ensure_ascii=False,
        )

    try:
        result = callback(emoji=emoji, message_id=message_id, remove=remove)
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)

    if not isinstance(result, dict):
        result = {"success": bool(result)}
    if result.get("success") and not remove:
        # Read by the model right before it decides its final reply — the
        # strongest place to stop the reflexive "Done." confirmation.
        result["note"] = (
            "Reaction posted. This completes your response — end your turn now "
            "with NO further message (no 'Done.', 'ok', acknowledgement, or "
            "emoji). Reply only if you have new, substantive information to add."
        )
    return json.dumps(result, ensure_ascii=False)


def check_react_requirements() -> bool:
    """Always registerable — per-turn availability is decided by the callback
    (present only in the gateway) and the adapter's own gating."""
    return True


REACT_SCHEMA = {
    "name": "react",
    "description": (
        "React to the message you're replying to with an emoji — a natural, "
        "human alternative to sending a text reply. Prefer this over a "
        "low-value message when:\n"
        "1. A simple acknowledgement is enough (e.g. someone thanks you, or "
        "you want to signal you're following a thread without interjecting).\n"
        "2. The answer is a simple yes/no/confirmation (👍 to agree, 🎉 to "
        "celebrate, 🙏 to accept praise) — add a follow-up message only if "
        "extra detail is actually warranted.\n"
        "3. You're explicitly asked to react with something.\n\n"
        "Provide `emoji` as a Slack emoji name WITHOUT colons (e.g. "
        "'thumbsup', '+1', 'pray', 'tada'). By default it reacts to the most "
        "recent message in this chat — the one you're responding to — so you "
        "usually don't need `message_id`. Set `remove: true` to retract a "
        "reaction.\n\n"
        "IMPORTANT: the reaction IS your entire response. After calling this "
        "tool, END YOUR TURN WITH NO TEXT AT ALL — do not send any closing or "
        "confirmation message. That means no 'Done.', 'Sure.', 'Got it.', "
        "'ok', no 'reacted with :tada:', and not even a lone emoji. Sending "
        "anything after the reaction defeats the purpose and reads as noise. "
        "Send a follow-up message ONLY if it contains new, substantive "
        "information the reaction itself cannot convey — a bare acknowledgement "
        "never qualifies."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "emoji": {
                "type": "string",
                "description": "Emoji name without colons (e.g. 'thumbsup', 'pray'). Required unless remove=true.",
            },
            "message_id": {
                "type": "string",
                "description": "Optional id/ts of the message to react to. Omit to react to the most recent message in this chat.",
            },
            "remove": {
                "type": "boolean",
                "description": "Retract a reaction instead of adding one.",
            },
        },
        "required": [],
    },
}


registry.register(
    name="react",
    toolset="react",
    schema=REACT_SCHEMA,
    handler=lambda args, **kw: react_tool(
        emoji=args.get("emoji", ""),
        message_id=args.get("message_id"),
        remove=bool(args.get("remove", False)),
        callback=kw.get("callback"),
    ),
    check_fn=check_react_requirements,
    emoji="👍",
)
