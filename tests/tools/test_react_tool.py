"""Tests for the agent-facing react tool (tools/react_tool.py)."""

import json

from tools.react_tool import react_tool


def _cb_ok(**kwargs):
    # Echo what the tool passed through so assertions can inspect it.
    return {"success": True, **kwargs}


def test_requires_emoji_for_add():
    out = json.loads(react_tool(emoji="", callback=_cb_ok))
    assert "error" in out


def test_unavailable_without_callback():
    out = json.loads(react_tool(emoji="tada"))
    assert out["error"].lower().startswith("reactions are not available")


def test_strips_colons_and_passes_through():
    out = json.loads(react_tool(emoji=":tada:", message_id="123.45", callback=_cb_ok))
    assert out["success"] is True
    assert out["emoji"] == "tada"
    assert out["message_id"] == "123.45"
    assert out["remove"] is False
    # Success result nudges the model not to narrate the reaction.
    assert "narrate" in out["note"].lower()


def test_remove_needs_no_emoji():
    out = json.loads(react_tool(remove=True, callback=_cb_ok))
    assert out["success"] is True
    assert out["remove"] is True


def test_callback_exception_is_caught():
    def _boom(**_kw):
        raise RuntimeError("kaboom")

    out = json.loads(react_tool(emoji="tada", callback=_boom))
    assert out["success"] is False
    assert "kaboom" in out["error"]


def test_registered_in_registry():
    from tools.registry import registry, discover_builtin_tools

    discover_builtin_tools()
    entry = registry.get_entry("react")
    assert entry is not None
    assert entry.toolset == "react"
