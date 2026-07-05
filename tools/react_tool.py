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
        # Read by the model right before its final reply: don't narrate the
        # reaction, but do engage normally if the message calls for it.
        result["note"] = (
            "Reaction posted. Don't narrate it (no 'Reacted with :x:' / 'ok "
            "👍'). Reply normally if the message invites a real response; "
            "otherwise the reaction can stand on its own."
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
        "human touch that makes you feel present. Reach for it often: to "
        "acknowledge, celebrate, agree, accept thanks/praise (🙏), signal "
        "you're following a thread, or when asked to react. Aim for reactions "
        "that are apt and characterful — clever, warm, or funny when it fits "
        "— not just a rote 👍.\n\n"
        "Provide `emoji` as a Slack emoji name WITHOUT colons (e.g. "
        "'thumbsup', '+1', 'pray', 'tada'). By default it reacts to the most "
        "recent message in this chat — the one you're responding to — so you "
        "usually don't need `message_id`. Set `remove: true` to retract a "
        "reaction.\n\n"
        "A reaction ADDS to your reply, it doesn't replace engaging: if the "
        "message invites a real response, still send one. Reactions express "
        "sentiment, not work status — never react with 👀/⏳/✅ to signal "
        "you're looking, working, or done (those are separate progress "
        "markers); when asked a question, just answer it. And don't narrate "
        "the reaction — no 'Reacted with :tada:' / 'ok 👍'; the emoji already "
        "shows, so let any message you send be a genuine reply on its own."
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
