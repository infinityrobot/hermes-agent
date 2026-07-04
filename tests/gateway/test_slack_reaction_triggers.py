"""
Tests for Slack reaction triggers (slack.reaction_triggers / emoji summon).

Reacting to a message with a configured emoji summons the bot on that
message: the reacted message is fetched and replayed through
``_handle_slack_message`` as a forced-mention event threaded onto the
reacted message. Follows the mock/setup pattern of test_slack_mention.py.
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig


# ---------------------------------------------------------------------------
# Mock slack-bolt if not installed (same as test_slack.py)
# ---------------------------------------------------------------------------

def _ensure_slack_mock():
    if "slack_bolt" in sys.modules and hasattr(sys.modules["slack_bolt"], "__file__"):
        return

    slack_bolt = MagicMock()
    slack_bolt.async_app.AsyncApp = MagicMock
    slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler = MagicMock

    slack_sdk = MagicMock()
    slack_sdk.web.async_client.AsyncWebClient = MagicMock

    for name, mod in [
        ("slack_bolt", slack_bolt),
        ("slack_bolt.async_app", slack_bolt.async_app),
        ("slack_bolt.adapter", slack_bolt.adapter),
        ("slack_bolt.adapter.socket_mode", slack_bolt.adapter.socket_mode),
        ("slack_bolt.adapter.socket_mode.async_handler", slack_bolt.adapter.socket_mode.async_handler),
        ("slack_sdk", slack_sdk),
        ("slack_sdk.web", slack_sdk.web),
        ("slack_sdk.web.async_client", slack_sdk.web.async_client),
    ]:
        sys.modules.setdefault(name, mod)


_ensure_slack_mock()

import plugins.platforms.slack.adapter as _slack_mod  # noqa: E402
_slack_mod.SLACK_AVAILABLE = True

from plugins.platforms.slack.adapter import (  # noqa: E402
    SlackAdapter,
    _apply_yaml_config,
    _parse_reaction_trigger_emojis,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BOT_USER_ID = "U_BOT_123"
REACTOR_ID = "U_HUMAN_456"
AUTHOR_ID = "U_AUTHOR_789"
CHANNEL_ID = "C0AQWDLHY9M"
MSG_TS = "1751600000.000100"
EVENT_TS = "1751600100.000200"


def _make_adapter(reaction_triggers=None):
    extra = {}
    if reaction_triggers is not None:
        extra["reaction_triggers"] = reaction_triggers

    adapter = object.__new__(SlackAdapter)
    adapter.platform = Platform.SLACK
    adapter.config = PlatformConfig(enabled=True, extra=extra)
    adapter._bot_user_id = BOT_USER_ID
    adapter._team_bot_user_ids = {}
    adapter._team_clients = {}
    adapter._channel_team = {}
    adapter._reaction_summons_seen = set()
    adapter._REACTION_SUMMONS_MAX = 5000

    client = MagicMock()
    client.conversations_replies = AsyncMock(
        return_value={
            "ok": True,
            "messages": [
                {"ts": MSG_TS, "text": "original message", "user": AUTHOR_ID}
            ],
        }
    )
    client.conversations_history = AsyncMock(
        return_value={"ok": True, "messages": []}
    )
    client.chat_postMessage = AsyncMock(return_value={"ok": True})
    adapter._app = MagicMock()
    adapter._app.client = client

    adapter._handle_slack_message = AsyncMock()
    return adapter, client


def _reaction_event(
    reaction="hermes",
    user=REACTOR_ID,
    channel=CHANNEL_ID,
    ts=MSG_TS,
    item_type="message",
):
    return {
        "type": "reaction_added",
        "reaction": reaction,
        "user": user,
        "item": {"type": item_type, "channel": channel, "ts": ts},
        "item_user": AUTHOR_ID,
        "event_ts": EVENT_TS,
    }


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------

class TestParseReactionTriggerEmojis:
    def test_none_and_empty(self):
        assert _parse_reaction_trigger_emojis(None) == set()
        assert _parse_reaction_trigger_emojis("") == set()
        assert _parse_reaction_trigger_emojis([]) == set()
        assert _parse_reaction_trigger_emojis({}) == set()

    def test_mapping_form(self):
        raw = {"enabled": True, "emojis": ["hermes", "eyes"]}
        assert _parse_reaction_trigger_emojis(raw) == {"hermes", "eyes"}

    def test_mapping_disabled(self):
        raw = {"enabled": False, "emojis": ["hermes"]}
        assert _parse_reaction_trigger_emojis(raw) == set()

    def test_mapping_disabled_string(self):
        raw = {"enabled": "false", "emojis": ["hermes"]}
        assert _parse_reaction_trigger_emojis(raw) == set()

    def test_mapping_enabled_defaults_true(self):
        assert _parse_reaction_trigger_emojis({"emojis": ["hermes"]}) == {"hermes"}

    def test_bare_list(self):
        assert _parse_reaction_trigger_emojis(["hermes"]) == {"hermes"}

    def test_csv_string(self):
        assert _parse_reaction_trigger_emojis("hermes, eyes") == {"hermes", "eyes"}

    def test_colons_and_case_normalised(self):
        assert _parse_reaction_trigger_emojis([":Hermes:"]) == {"hermes"}

    def test_scalar_garbage(self):
        assert _parse_reaction_trigger_emojis(42) == set()


class TestReactionTriggerConfigSources:
    def test_extra_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("SLACK_REACTION_TRIGGER_EMOJIS", "eyes")
        adapter, _ = _make_adapter(reaction_triggers=["hermes"])
        assert adapter._slack_reaction_trigger_emojis() == {"hermes"}

    def test_env_fallback(self, monkeypatch):
        monkeypatch.setenv("SLACK_REACTION_TRIGGER_EMOJIS", "hermes,eyes")
        adapter, _ = _make_adapter()
        assert adapter._slack_reaction_trigger_emojis() == {"hermes", "eyes"}

    def test_default_empty(self, monkeypatch):
        monkeypatch.delenv("SLACK_REACTION_TRIGGER_EMOJIS", raising=False)
        adapter, _ = _make_adapter()
        assert adapter._slack_reaction_trigger_emojis() == set()


class TestApplyYamlConfigBridge:
    def test_bridges_mapping_to_env(self, monkeypatch):
        # setenv-then-delenv records the var's original state with
        # monkeypatch, so the value _apply_yaml_config writes straight into
        # os.environ is rolled back at teardown.
        monkeypatch.setenv("SLACK_REACTION_TRIGGER_EMOJIS", "placeholder")
        monkeypatch.delenv("SLACK_REACTION_TRIGGER_EMOJIS")
        _apply_yaml_config(
            {}, {"reaction_triggers": {"enabled": True, "emojis": ["hermes", "eyes"]}}
        )
        assert os.environ.get("SLACK_REACTION_TRIGGER_EMOJIS") == "eyes,hermes"

    def test_disabled_not_bridged(self, monkeypatch):
        monkeypatch.setenv("SLACK_REACTION_TRIGGER_EMOJIS", "placeholder")
        monkeypatch.delenv("SLACK_REACTION_TRIGGER_EMOJIS")
        _apply_yaml_config(
            {}, {"reaction_triggers": {"enabled": False, "emojis": ["hermes"]}}
        )
        assert os.environ.get("SLACK_REACTION_TRIGGER_EMOJIS") is None

    def test_env_wins_over_yaml(self, monkeypatch):
        monkeypatch.setenv("SLACK_REACTION_TRIGGER_EMOJIS", "eyes")
        _apply_yaml_config({}, {"reaction_triggers": ["hermes"]})
        assert os.environ.get("SLACK_REACTION_TRIGGER_EMOJIS") == "eyes"


# ---------------------------------------------------------------------------
# Reaction handler behaviour
# ---------------------------------------------------------------------------

class TestHandleSlackReactionAdded:
    @pytest.mark.asyncio
    async def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("SLACK_REACTION_TRIGGER_EMOJIS", raising=False)
        adapter, client = _make_adapter()
        await adapter._handle_slack_reaction_added(_reaction_event())
        client.conversations_replies.assert_not_awaited()
        adapter._handle_slack_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_matching_emoji_summons_in_thread(self):
        adapter, client = _make_adapter(reaction_triggers=["hermes"])
        await adapter._handle_slack_reaction_added(_reaction_event())

        adapter._handle_slack_message.assert_awaited_once()
        synthetic = adapter._handle_slack_message.await_args.args[0]
        assert synthetic["channel"] == CHANNEL_ID
        assert synthetic["user"] == REACTOR_ID
        # Reply forced into the reacted message's thread.
        assert synthetic["thread_ts"] == MSG_TS
        # Unique ts (reaction event ts), so the message deduper does not
        # suppress summons on already-processed messages.
        assert synthetic["ts"] == EVENT_TS
        # Forced mention so mention gating passes.
        assert f"<@{BOT_USER_ID}>" in synthetic["text"]
        assert ":hermes:" in synthetic["text"]
        assert "original message" in synthetic["text"]

    @pytest.mark.asyncio
    async def test_non_matching_emoji_ignored(self):
        adapter, client = _make_adapter(reaction_triggers=["hermes"])
        await adapter._handle_slack_reaction_added(_reaction_event(reaction="tada"))
        client.conversations_replies.assert_not_awaited()
        adapter._handle_slack_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skin_tone_variant_matches(self):
        adapter, _ = _make_adapter(reaction_triggers=["thumbsup"])
        await adapter._handle_slack_reaction_added(
            _reaction_event(reaction="thumbsup::skin-tone-2")
        )
        adapter._handle_slack_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_bots_own_reaction_ignored(self):
        adapter, client = _make_adapter(reaction_triggers=["hermes"])
        await adapter._handle_slack_reaction_added(_reaction_event(user=BOT_USER_ID))
        client.conversations_replies.assert_not_awaited()
        adapter._handle_slack_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_team_bot_user_reaction_ignored(self):
        adapter, _ = _make_adapter(reaction_triggers=["hermes"])
        adapter._team_bot_user_ids = {"T1": "U_OTHER_WS_BOT"}
        await adapter._handle_slack_reaction_added(
            _reaction_event(user="U_OTHER_WS_BOT")
        )
        adapter._handle_slack_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_duplicate_reaction_summons_once(self):
        adapter, _ = _make_adapter(reaction_triggers=["hermes"])
        await adapter._handle_slack_reaction_added(_reaction_event())
        # Second user reacting with the same emoji on the same message.
        await adapter._handle_slack_reaction_added(
            _reaction_event(user="U_ANOTHER_USER")
        )
        adapter._handle_slack_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_different_messages_each_summon(self):
        adapter, client = _make_adapter(reaction_triggers=["hermes"])
        # Echo the requested ts so both fetches resolve their own message.
        client.conversations_replies.side_effect = lambda **kw: {
            "ok": True,
            "messages": [{"ts": kw["ts"], "text": "msg", "user": AUTHOR_ID}],
        }
        await adapter._handle_slack_reaction_added(_reaction_event(ts=MSG_TS))
        await adapter._handle_slack_reaction_added(
            _reaction_event(ts="1751600999.000300")
        )
        assert adapter._handle_slack_message.await_count == 2

    @pytest.mark.asyncio
    async def test_non_message_item_ignored(self):
        adapter, client = _make_adapter(reaction_triggers=["hermes"])
        await adapter._handle_slack_reaction_added(_reaction_event(item_type="file"))
        client.conversations_replies.assert_not_awaited()
        adapter._handle_slack_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_threaded_message_summons_in_parent_thread(self):
        adapter, client = _make_adapter(reaction_triggers=["hermes"])
        parent_ts = "1751500000.000001"
        client.conversations_replies.return_value = {
            "ok": True,
            "messages": [
                {
                    "ts": MSG_TS,
                    "thread_ts": parent_ts,
                    "text": "threaded reply",
                    "user": AUTHOR_ID,
                }
            ],
        }
        await adapter._handle_slack_reaction_added(_reaction_event())
        synthetic = adapter._handle_slack_message.await_args.args[0]
        assert synthetic["thread_ts"] == parent_ts

    @pytest.mark.asyncio
    async def test_access_denied_posts_notice(self):
        adapter, client = _make_adapter(reaction_triggers=["hermes"])
        client.conversations_replies.side_effect = Exception("not_in_channel")
        client.conversations_history.side_effect = Exception("not_in_channel")

        await adapter._handle_slack_reaction_added(_reaction_event())

        adapter._handle_slack_message.assert_not_awaited()
        client.chat_postMessage.assert_awaited_once()
        kwargs = client.chat_postMessage.await_args.kwargs
        assert kwargs["channel"] == CHANNEL_ID
        assert kwargs["thread_ts"] == MSG_TS
        assert "access" in kwargs["text"].lower()

    @pytest.mark.asyncio
    async def test_access_notice_failure_is_swallowed(self):
        adapter, client = _make_adapter(reaction_triggers=["hermes"])
        client.conversations_replies.side_effect = Exception("channel_not_found")
        client.conversations_history.side_effect = Exception("channel_not_found")
        client.chat_postMessage.side_effect = Exception("channel_not_found")

        # Must not raise.
        await adapter._handle_slack_reaction_added(_reaction_event())
        adapter._handle_slack_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_history_fallback_when_replies_fails(self):
        adapter, client = _make_adapter(reaction_triggers=["hermes"])
        client.conversations_replies.side_effect = Exception("boom")
        client.conversations_history.return_value = {
            "ok": True,
            "messages": [{"ts": MSG_TS, "text": "from history", "user": AUTHOR_ID}],
        }
        await adapter._handle_slack_reaction_added(_reaction_event())
        adapter._handle_slack_message.assert_awaited_once()
        synthetic = adapter._handle_slack_message.await_args.args[0]
        assert "from history" in synthetic["text"]

    @pytest.mark.asyncio
    async def test_dm_channel_type(self):
        adapter, client = _make_adapter(reaction_triggers=["hermes"])
        await adapter._handle_slack_reaction_added(
            _reaction_event(channel="D12345678")
        )
        synthetic = adapter._handle_slack_message.await_args.args[0]
        assert synthetic["channel_type"] == "im"

    @pytest.mark.asyncio
    async def test_files_passed_through(self):
        adapter, client = _make_adapter(reaction_triggers=["hermes"])
        files = [{"id": "F123", "name": "report.pdf"}]
        client.conversations_replies.return_value = {
            "ok": True,
            "messages": [
                {"ts": MSG_TS, "text": "with file", "user": AUTHOR_ID, "files": files}
            ],
        }
        await adapter._handle_slack_reaction_added(_reaction_event())
        synthetic = adapter._handle_slack_message.await_args.args[0]
        assert synthetic["files"] == files
