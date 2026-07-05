"""
Tests for Slack reaction triggers (slack.reaction_trigger_emojis / emoji summon).

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

from gateway.platforms.base import ProcessingOutcome  # noqa: E402
from gateway.platforms.helpers import MessageDeduplicator  # noqa: E402
from plugins.platforms.slack.adapter import (  # noqa: E402
    SlackAdapter,
    _apply_yaml_config,
    _is_wordless,
    _parse_reaction_status_emojis,
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


def _make_adapter(reaction_trigger_emojis=None, agent_reactions=None):
    extra = {}
    if reaction_trigger_emojis is not None:
        extra["reaction_trigger_emojis"] = reaction_trigger_emojis
    if agent_reactions is not None:
        extra["agent_reactions"] = agent_reactions

    adapter = object.__new__(SlackAdapter)
    adapter.platform = Platform.SLACK
    adapter.config = PlatformConfig(enabled=True, extra=extra)
    adapter._bot_user_id = BOT_USER_ID
    adapter._team_bot_user_ids = {}
    adapter._team_clients = {}
    adapter._channel_team = {}
    adapter._reaction_summon_dedup = MessageDeduplicator(max_size=5000)
    adapter._reaction_channel_type = {}
    adapter._reactor_is_bot_cache = {}
    adapter._reaction_lifecycle_target = {}
    adapter._reacting_message_ids = set()
    adapter._last_inbound_ts = {}
    adapter._recent_agent_reaction = {}
    adapter._active_sessions = {}
    adapter._session_store = None

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

    def _conv_info(channel, **_kw):
        # Classify by ID prefix so tests exercise im / mpim / channel keying:
        # D → 1:1 DM, G → group DM (mpim), everything else → channel.
        chan = {"is_im": channel.startswith("D"), "is_mpim": channel.startswith("G")}
        return {"ok": True, "channel": chan}

    client.conversations_info = AsyncMock(side_effect=_conv_info)
    client.users_info = AsyncMock(
        return_value={"ok": True, "user": {"is_bot": False}}
    )
    client.chat_postMessage = AsyncMock(return_value={"ok": True})
    adapter._app = MagicMock()
    adapter._app.client = client

    adapter._handle_slack_message = AsyncMock()
    return adapter, client


def _mark_active_thread(
    adapter,
    *,
    channel=CHANNEL_ID,
    thread_ts=MSG_TS,
    user=REACTOR_ID,
    channel_type="channel",
):
    from gateway.session import build_session_key

    source = adapter.build_source(
        chat_id=channel,
        chat_name=channel,
        chat_type="dm" if channel_type in {"im", "mpim"} else "group",
        user_id=user,
        thread_id=thread_ts,
    )
    key = build_session_key(
        source,
        group_sessions_per_user=adapter.config.extra.get(
            "group_sessions_per_user", True
        ),
        thread_sessions_per_user=adapter.config.extra.get(
            "thread_sessions_per_user", False
        ),
    )
    adapter._active_sessions[key] = object()
    return key


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


def _reaction_removed_event(**kwargs):
    event = _reaction_event(**kwargs)
    event["type"] = "reaction_removed"
    event["event_ts"] = "1751600200.000300"
    return event


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------

class TestParseReactionTriggerEmojis:
    def test_none_and_empty(self):
        assert _parse_reaction_trigger_emojis(None) == set()
        assert _parse_reaction_trigger_emojis("") == set()
        assert _parse_reaction_trigger_emojis([]) == set()
        # A stray mapping is not a valid value for the flat key → off.
        assert _parse_reaction_trigger_emojis({}) == set()
        assert _parse_reaction_trigger_emojis({"emojis": ["hermes"]}) == set()

    def test_list(self):
        assert _parse_reaction_trigger_emojis(["hermes", "eyes"]) == {"hermes", "eyes"}

    def test_bare_list(self):
        assert _parse_reaction_trigger_emojis(["hermes"]) == {"hermes"}

    def test_csv_string(self):
        assert _parse_reaction_trigger_emojis("hermes, eyes") == {"hermes", "eyes"}

    def test_colons_and_case_normalised(self):
        assert _parse_reaction_trigger_emojis([":Hermes:"]) == {"hermes"}

    def test_scalar_garbage(self):
        assert _parse_reaction_trigger_emojis(42) == set()


class TestParseReactionStatusEmojis:
    DEFAULTS = {"in_progress": "eyes", "done": "white_check_mark", "failed": "x"}

    def test_none_and_empty_return_defaults(self):
        assert _parse_reaction_status_emojis(None) == self.DEFAULTS
        assert _parse_reaction_status_emojis("") == self.DEFAULTS
        assert _parse_reaction_status_emojis({}) == self.DEFAULTS

    def test_full_mapping(self):
        raw = {"in_progress": "hourglass", "done": "tada", "failed": "boom"}
        assert _parse_reaction_status_emojis(raw) == raw

    def test_partial_mapping_merges_over_defaults(self):
        assert _parse_reaction_status_emojis({"done": "tada"}) == {
            "in_progress": "eyes",
            "done": "tada",
            "failed": "x",
        }

    def test_colons_stripped(self):
        assert _parse_reaction_status_emojis({"in_progress": ":hourglass:"})["in_progress"] == (
            "hourglass"
        )

    def test_positional_csv(self):
        assert _parse_reaction_status_emojis("hourglass,tada,boom") == {
            "in_progress": "hourglass",
            "done": "tada",
            "failed": "boom",
        }

    def test_json_string(self):
        assert _parse_reaction_status_emojis('{"done": "tada"}')["done"] == "tada"


class TestReactionStatusEmojiConfigSources:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("SLACK_REACTION_STATUS_EMOJIS", raising=False)
        adapter, _ = _make_adapter()
        assert adapter._slack_reaction_status_emojis()["in_progress"] == "eyes"

    def test_extra_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("SLACK_REACTION_STATUS_EMOJIS", "hourglass,tada,boom")
        adapter, _ = _make_adapter()
        adapter.config.extra["reaction_status_emojis"] = {"in_progress": "spinner"}
        assert adapter._slack_reaction_status_emojis()["in_progress"] == "spinner"

    def test_env_fallback(self, monkeypatch):
        monkeypatch.setenv("SLACK_REACTION_STATUS_EMOJIS", "hourglass,tada,boom")
        adapter, _ = _make_adapter()
        assert adapter._slack_reaction_status_emojis() == {
            "in_progress": "hourglass",
            "done": "tada",
            "failed": "boom",
        }

    def test_yaml_bridge_to_env(self, monkeypatch):
        monkeypatch.setenv("SLACK_REACTION_STATUS_EMOJIS", "placeholder")
        monkeypatch.delenv("SLACK_REACTION_STATUS_EMOJIS")
        _apply_yaml_config({}, {"reaction_status_emojis": {"done": "tada"}})
        assert '"done": "tada"' in os.environ.get("SLACK_REACTION_STATUS_EMOJIS", "")


class TestReactionTriggerConfigSources:
    def test_extra_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("SLACK_REACTION_TRIGGER_EMOJIS", "eyes")
        adapter, _ = _make_adapter(reaction_trigger_emojis=["hermes"])
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
    def test_bridges_list_to_env(self, monkeypatch):
        # setenv-then-delenv records the var's original state with
        # monkeypatch, so the value _apply_yaml_config writes straight into
        # os.environ is rolled back at teardown.
        monkeypatch.setenv("SLACK_REACTION_TRIGGER_EMOJIS", "placeholder")
        monkeypatch.delenv("SLACK_REACTION_TRIGGER_EMOJIS")
        _apply_yaml_config({}, {"reaction_trigger_emojis": ["hermes", "eyes"]})
        assert os.environ.get("SLACK_REACTION_TRIGGER_EMOJIS") == "eyes,hermes"

    def test_empty_list_not_bridged(self, monkeypatch):
        # No emojis → nothing to bridge (the feature stays off).
        monkeypatch.setenv("SLACK_REACTION_TRIGGER_EMOJIS", "placeholder")
        monkeypatch.delenv("SLACK_REACTION_TRIGGER_EMOJIS")
        _apply_yaml_config({}, {"reaction_trigger_emojis": []})
        assert os.environ.get("SLACK_REACTION_TRIGGER_EMOJIS") is None

    def test_env_wins_over_yaml(self, monkeypatch):
        monkeypatch.setenv("SLACK_REACTION_TRIGGER_EMOJIS", "eyes")
        _apply_yaml_config({}, {"reaction_trigger_emojis": ["hermes"]})
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
        adapter, client = _make_adapter(reaction_trigger_emojis=["hermes"])
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
        adapter, client = _make_adapter(reaction_trigger_emojis=["hermes"])
        await adapter._handle_slack_reaction_added(_reaction_event(reaction="tada"))
        client.conversations_replies.assert_not_awaited()
        adapter._handle_slack_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skin_tone_variant_matches(self):
        adapter, _ = _make_adapter(reaction_trigger_emojis=["thumbsup"])
        await adapter._handle_slack_reaction_added(
            _reaction_event(reaction="thumbsup::skin-tone-2")
        )
        adapter._handle_slack_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_bots_own_reaction_ignored(self):
        adapter, client = _make_adapter(reaction_trigger_emojis=["hermes"])
        await adapter._handle_slack_reaction_added(_reaction_event(user=BOT_USER_ID))
        client.conversations_replies.assert_not_awaited()
        adapter._handle_slack_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_team_bot_user_reaction_ignored(self):
        adapter, _ = _make_adapter(reaction_trigger_emojis=["hermes"])
        adapter._team_bot_user_ids = {"T1": "U_OTHER_WS_BOT"}
        await adapter._handle_slack_reaction_added(
            _reaction_event(user="U_OTHER_WS_BOT")
        )
        adapter._handle_slack_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_duplicate_reaction_summons_once(self):
        adapter, _ = _make_adapter(reaction_trigger_emojis=["hermes"])
        await adapter._handle_slack_reaction_added(_reaction_event())
        # Second user reacting with the same emoji on the same message.
        await adapter._handle_slack_reaction_added(
            _reaction_event(user="U_ANOTHER_USER")
        )
        adapter._handle_slack_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_different_messages_each_summon(self):
        adapter, client = _make_adapter(reaction_trigger_emojis=["hermes"])
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
        adapter, client = _make_adapter(reaction_trigger_emojis=["hermes"])
        await adapter._handle_slack_reaction_added(_reaction_event(item_type="file"))
        client.conversations_replies.assert_not_awaited()
        adapter._handle_slack_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_threaded_message_summons_in_parent_thread(self):
        adapter, client = _make_adapter(reaction_trigger_emojis=["hermes"])
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
        adapter, client = _make_adapter(reaction_trigger_emojis=["hermes"])
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
        adapter, client = _make_adapter(reaction_trigger_emojis=["hermes"])
        client.conversations_replies.side_effect = Exception("channel_not_found")
        client.conversations_history.side_effect = Exception("channel_not_found")
        client.chat_postMessage.side_effect = Exception("channel_not_found")

        # Must not raise.
        await adapter._handle_slack_reaction_added(_reaction_event())
        adapter._handle_slack_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_history_fallback_when_replies_fails(self):
        adapter, client = _make_adapter(reaction_trigger_emojis=["hermes"])
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
        adapter, client = _make_adapter(reaction_trigger_emojis=["hermes"])
        await adapter._handle_slack_reaction_added(
            _reaction_event(channel="D12345678")
        )
        synthetic = adapter._handle_slack_message.await_args.args[0]
        assert synthetic["channel_type"] == "im"

    @pytest.mark.asyncio
    async def test_files_passed_through(self):
        adapter, client = _make_adapter(reaction_trigger_emojis=["hermes"])
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

    @pytest.mark.asyncio
    async def test_foreign_bot_reactor_dropped_when_bots_disallowed(self, monkeypatch):
        # Default allow_bots="none": a bot reactor is dropped, and must NOT
        # consume the once-per-message summon slot.
        monkeypatch.delenv("SLACK_ALLOW_BOTS", raising=False)
        adapter, client = _make_adapter(reaction_trigger_emojis=["hermes"])
        client.users_info.return_value = {"ok": True, "user": {"is_bot": True}}
        await adapter._handle_slack_reaction_added(_reaction_event(user="U_OTHER_BOT"))
        adapter._handle_slack_message.assert_not_awaited()

        # A human then reacts with the same emoji on the same message: the slot
        # was not consumed, so this summons.
        client.users_info.return_value = {"ok": True, "user": {"is_bot": False}}
        await adapter._handle_slack_reaction_added(_reaction_event(user=REACTOR_ID))
        adapter._handle_slack_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_foreign_bot_reactor_stamped_when_bots_allowed(self, monkeypatch):
        monkeypatch.delenv("SLACK_ALLOW_BOTS", raising=False)
        adapter, client = _make_adapter(reaction_trigger_emojis=["hermes"])
        adapter.config.extra["allow_bots"] = "all"
        client.users_info.return_value = {"ok": True, "user": {"is_bot": True}}

        await adapter._handle_slack_reaction_added(_reaction_event(user="U_OTHER_BOT"))

        # Bots allowed → summon replays, stamped with bot_id so the pipeline's
        # allow_bots policy still governs it rather than being bypassed.
        adapter._handle_slack_message.assert_awaited_once()
        synthetic = adapter._handle_slack_message.await_args.args[0]
        assert synthetic.get("bot_id")

    @pytest.mark.asyncio
    async def test_team_id_recovered_from_outer_body(self):
        # The inner reaction event has no team; the outer Bolt body carries it.
        adapter, client = _make_adapter(reaction_trigger_emojis=["hermes"])
        await adapter._handle_slack_reaction_added(
            _reaction_event(), body={"team_id": "T_OUTER"}
        )
        synthetic = adapter._handle_slack_message.await_args.args[0]
        assert synthetic["team"] == "T_OUTER"
        assert adapter._channel_team[CHANNEL_ID] == "T_OUTER"

    @pytest.mark.asyncio
    async def test_failed_fetch_can_be_retried(self):
        adapter, client = _make_adapter(reaction_trigger_emojis=["hermes"])
        # First attempt: fetch fails on both paths → access notice, no summon.
        client.conversations_replies.side_effect = Exception("not_in_channel")
        client.conversations_history.side_effect = Exception("not_in_channel")
        await adapter._handle_slack_reaction_added(_reaction_event())
        adapter._handle_slack_message.assert_not_awaited()

        # Bot invited; user re-reacts with the same emoji on the same message.
        client.conversations_replies.side_effect = None
        client.conversations_replies.return_value = {
            "ok": True,
            "messages": [{"ts": MSG_TS, "text": "now visible", "user": AUTHOR_ID}],
        }
        await adapter._handle_slack_reaction_added(_reaction_event())
        adapter._handle_slack_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mpim_channel_type_resolved(self):
        adapter, client = _make_adapter(reaction_trigger_emojis=["hermes"])
        await adapter._handle_slack_reaction_added(
            _reaction_event(channel="G12345678")
        )
        synthetic = adapter._handle_slack_message.await_args.args[0]
        assert synthetic["channel_type"] == "mpim"

    @pytest.mark.asyncio
    async def test_reaction_on_in_thread_reply_resolves(self):
        # conversations.replies returns the parent FIRST; the reacted reply is
        # only in the result because we page the thread (limit > 1).
        adapter, client = _make_adapter(reaction_trigger_emojis=["hermes"])
        parent_ts = "1751500000.000001"
        client.conversations_replies.return_value = {
            "ok": True,
            "messages": [
                {"ts": parent_ts, "text": "parent", "user": AUTHOR_ID,
                 "thread_ts": parent_ts},
                {"ts": MSG_TS, "text": "the reacted reply", "user": AUTHOR_ID,
                 "thread_ts": parent_ts},
            ],
        }
        await adapter._handle_slack_reaction_added(_reaction_event())

        adapter._handle_slack_message.assert_awaited_once()
        synthetic = adapter._handle_slack_message.await_args.args[0]
        assert "the reacted reply" in synthetic["text"]
        assert synthetic["thread_ts"] == parent_ts
        # We must request more than the thread parent alone.
        assert client.conversations_replies.await_args.kwargs["limit"] > 1

    @pytest.mark.asyncio
    async def test_channel_type_not_cached_on_transient_failure(self):
        adapter, client = _make_adapter(reaction_trigger_emojis=["hermes"])
        client.conversations_info.side_effect = Exception("ratelimited")

        first = await adapter._resolve_reaction_channel_type("G12345678")
        assert first == "channel"  # heuristic fallback for the failing call
        assert "G12345678" not in adapter._reaction_channel_type  # not poisoned

        # A later reaction retries; info now succeeds and the mpim keys correctly.
        client.conversations_info.side_effect = None
        client.conversations_info.return_value = {
            "ok": True,
            "channel": {"is_mpim": True},
        }
        second = await adapter._resolve_reaction_channel_type("G12345678")
        assert second == "mpim"
        assert adapter._reaction_channel_type["G12345678"] == "mpim"


class TestHandleSlackReactionRemoved:
    @pytest.mark.asyncio
    async def test_reactions_disabled_ignores_stop_trigger(self, monkeypatch):
        monkeypatch.setenv("SLACK_REACTIONS", "false")
        adapter, client = _make_adapter(reaction_trigger_emojis=["hermes"])
        _mark_active_thread(adapter)

        await adapter._handle_slack_reaction_removed(_reaction_removed_event())

        client.conversations_replies.assert_not_awaited()
        adapter._handle_slack_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_trigger_emoji_removal_ignored(self, monkeypatch):
        monkeypatch.delenv("SLACK_REACTIONS", raising=False)
        adapter, client = _make_adapter(reaction_trigger_emojis=["hermes"])
        _mark_active_thread(adapter)

        await adapter._handle_slack_reaction_removed(
            _reaction_removed_event(reaction="thumbsup")
        )

        client.conversations_replies.assert_not_awaited()
        adapter._handle_slack_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_removal_disabled_when_no_triggers(self, monkeypatch):
        monkeypatch.delenv("SLACK_REACTIONS", raising=False)
        monkeypatch.delenv("SLACK_REACTION_TRIGGER_EMOJIS", raising=False)
        adapter, client = _make_adapter()  # no reaction_trigger_emojis
        _mark_active_thread(adapter)

        await adapter._handle_slack_reaction_removed(_reaction_removed_event())

        client.conversations_replies.assert_not_awaited()
        adapter._handle_slack_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_active_top_level_thread_synthesizes_stop(self, monkeypatch):
        monkeypatch.delenv("SLACK_REACTIONS", raising=False)
        adapter, client = _make_adapter(reaction_trigger_emojis=["hermes"])
        _mark_active_thread(adapter)
        summon_key = f":{CHANNEL_ID}:{MSG_TS}:hermes"
        adapter._reaction_summon_dedup.is_duplicate(summon_key)  # record it

        await adapter._handle_slack_reaction_removed(_reaction_removed_event())

        client.conversations_replies.assert_not_awaited()
        adapter._handle_slack_message.assert_awaited_once()
        synthetic = adapter._handle_slack_message.await_args.args[0]
        assert synthetic["channel"] == CHANNEL_ID
        assert synthetic["user"] == REACTOR_ID
        assert synthetic["thread_ts"] == MSG_TS
        assert synthetic["ts"] == "1751600200.000300"
        assert synthetic["text"] == f"<@{BOT_USER_ID}> /stop"
        # Stop cleared the summon dedup so re-adding the emoji can summon again.
        assert not adapter._reaction_summon_dedup.is_duplicate(summon_key)

    @pytest.mark.asyncio
    async def test_inactive_thread_ignored(self, monkeypatch):
        monkeypatch.delenv("SLACK_REACTIONS", raising=False)
        adapter, client = _make_adapter(reaction_trigger_emojis=["hermes"])
        # Some other thread is in flight, but not the reacted message's — so
        # the handler fetches to check for a parent root, then still ignores it.
        _mark_active_thread(adapter, thread_ts="9999999999.000000")

        await adapter._handle_slack_reaction_removed(_reaction_removed_event())

        adapter._handle_slack_message.assert_not_awaited()
        client.conversations_replies.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_idle_bot_skips_lookup(self, monkeypatch):
        # No turn in flight anywhere: bail before any Slack API call.
        monkeypatch.delenv("SLACK_REACTIONS", raising=False)
        adapter, client = _make_adapter(reaction_trigger_emojis=["hermes"])

        await adapter._handle_slack_reaction_removed(_reaction_removed_event())

        client.conversations_info.assert_not_awaited()
        client.conversations_replies.assert_not_awaited()
        adapter._handle_slack_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bots_own_reaction_removal_ignored(self, monkeypatch):
        monkeypatch.delenv("SLACK_REACTIONS", raising=False)
        adapter, client = _make_adapter(reaction_trigger_emojis=["hermes"])
        _mark_active_thread(adapter)

        await adapter._handle_slack_reaction_removed(
            _reaction_removed_event(user=BOT_USER_ID)
        )

        client.conversations_replies.assert_not_awaited()
        adapter._handle_slack_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_threaded_message_removal_stops_parent_thread(self, monkeypatch):
        monkeypatch.delenv("SLACK_REACTIONS", raising=False)
        adapter, client = _make_adapter(reaction_trigger_emojis=["hermes"])
        parent_ts = "1751500000.000001"
        _mark_active_thread(adapter, thread_ts=parent_ts)
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

        await adapter._handle_slack_reaction_removed(_reaction_removed_event())

        adapter._handle_slack_message.assert_awaited_once()
        synthetic = adapter._handle_slack_message.await_args.args[0]
        assert synthetic["thread_ts"] == parent_ts
        assert synthetic["text"] == f"<@{BOT_USER_ID}> /stop"

    @pytest.mark.asyncio
    async def test_stop_falls_back_to_primary_bot_uid_when_team_uid_empty(
        self, monkeypatch
    ):
        # A workspace whose auth_test returned no user_id is stored as "".
        # bot_uid must fall back to the primary id, or the <@bot> prefix is
        # dropped and mention-gating rejects the synthetic /stop.
        monkeypatch.delenv("SLACK_REACTIONS", raising=False)
        adapter, client = _make_adapter(reaction_trigger_emojis=["hermes"])
        adapter._team_bot_user_ids = {"T1": ""}
        _mark_active_thread(adapter, thread_ts=MSG_TS)

        event = _reaction_removed_event()
        event["team"] = "T1"
        await adapter._handle_slack_reaction_removed(event)

        adapter._handle_slack_message.assert_awaited_once()
        synthetic = adapter._handle_slack_message.await_args.args[0]
        assert synthetic["text"] == f"<@{BOT_USER_ID}> /stop"


class TestReactionSummonLifecycle:
    """The :eyes:/done reaction lifecycle should land on the ORIGINAL reacted
    message for a summon (not the synthetic reaction event ts)."""

    @pytest.mark.asyncio
    async def test_summon_registers_lifecycle_target(self, monkeypatch):
        monkeypatch.delenv("SLACK_REACTIONS", raising=False)
        adapter, _ = _make_adapter(reaction_trigger_emojis=["hermes"])
        await adapter._handle_slack_reaction_added(_reaction_event())
        # synthetic ts (EVENT_TS) redirects to the reacted message (MSG_TS).
        assert adapter._reaction_lifecycle_target.get(EVENT_TS) == MSG_TS

    @pytest.mark.asyncio
    async def test_summon_no_lifecycle_target_when_reactions_disabled(self, monkeypatch):
        monkeypatch.setenv("SLACK_REACTIONS", "false")
        adapter, _ = _make_adapter(reaction_trigger_emojis=["hermes"])
        await adapter._handle_slack_reaction_added(_reaction_event())
        assert adapter._reaction_lifecycle_target == {}

    def _event(self):
        from types import SimpleNamespace

        return SimpleNamespace(
            message_id=EVENT_TS,
            source=SimpleNamespace(chat_id=CHANNEL_ID),
        )

    @pytest.mark.asyncio
    async def test_on_start_reacts_on_original_message(self, monkeypatch):
        monkeypatch.delenv("SLACK_REACTIONS", raising=False)
        adapter, _ = _make_adapter()
        adapter._add_reaction = AsyncMock(return_value=True)
        adapter._reacting_message_ids = {EVENT_TS}
        adapter._reaction_lifecycle_target = {EVENT_TS: MSG_TS}

        await adapter.on_processing_start(self._event())

        # Reaction lands on the original message ts, not the synthetic event ts.
        adapter._add_reaction.assert_awaited_once_with(CHANNEL_ID, MSG_TS, "eyes")

    @pytest.mark.asyncio
    async def test_on_complete_swaps_on_original_and_clears_map(self, monkeypatch):
        monkeypatch.delenv("SLACK_REACTIONS", raising=False)
        adapter, _ = _make_adapter()
        adapter._add_reaction = AsyncMock(return_value=True)
        adapter._remove_reaction = AsyncMock(return_value=True)
        adapter._reacting_message_ids = {EVENT_TS}
        adapter._reaction_lifecycle_target = {EVENT_TS: MSG_TS}

        await adapter.on_processing_complete(self._event(), ProcessingOutcome.SUCCESS)

        adapter._remove_reaction.assert_awaited_once_with(CHANNEL_ID, MSG_TS, "eyes")
        adapter._add_reaction.assert_awaited_once_with(
            CHANNEL_ID, MSG_TS, "white_check_mark"
        )
        assert EVENT_TS not in adapter._reaction_lifecycle_target

    @pytest.mark.asyncio
    async def test_configured_emojis_used(self, monkeypatch):
        monkeypatch.delenv("SLACK_REACTIONS", raising=False)
        monkeypatch.setenv("SLACK_REACTION_STATUS_EMOJIS", "hourglass,tada,boom")
        adapter, _ = _make_adapter()
        adapter._add_reaction = AsyncMock(return_value=True)
        adapter._remove_reaction = AsyncMock(return_value=True)
        adapter._reacting_message_ids = {MSG_TS}  # normal message, no redirect

        ev = self._event()
        ev.message_id = MSG_TS
        await adapter.on_processing_start(ev)
        adapter._add_reaction.assert_awaited_once_with(CHANNEL_ID, MSG_TS, "hourglass")

        await adapter.on_processing_complete(ev, ProcessingOutcome.FAILURE)
        adapter._remove_reaction.assert_awaited_once_with(CHANNEL_ID, MSG_TS, "hourglass")
        adapter._add_reaction.assert_awaited_with(CHANNEL_ID, MSG_TS, "boom")


class TestAgentReactions:
    """Agent-initiated reactions (slack.agent_reactions) — the public
    add_reaction/remove_reaction the gateway react tool dispatches to."""

    @pytest.mark.asyncio
    async def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("SLACK_AGENT_REACTIONS", raising=False)
        adapter, _ = _make_adapter()
        adapter._add_reaction = AsyncMock(return_value=True)
        result = await adapter.add_reaction(CHANNEL_ID, "tada", message_id=MSG_TS)
        assert result["success"] is False
        adapter._add_reaction.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_enabled_reacts_on_explicit_message(self):
        adapter, _ = _make_adapter(agent_reactions=True)
        adapter._add_reaction = AsyncMock(return_value=True)
        result = await adapter.add_reaction(CHANNEL_ID, ":tada:", message_id=MSG_TS)
        assert result["success"] is True
        # colon-stripped emoji, correct target
        adapter._add_reaction.assert_awaited_once_with(CHANNEL_ID, MSG_TS, "tada")

    @pytest.mark.asyncio
    async def test_defaults_to_last_inbound_message(self):
        adapter, _ = _make_adapter(agent_reactions=True)
        adapter._add_reaction = AsyncMock(return_value=True)
        adapter._last_inbound_ts[CHANNEL_ID] = MSG_TS
        result = await adapter.add_reaction(CHANNEL_ID, "eyes")
        assert result["success"] is True
        adapter._add_reaction.assert_awaited_once_with(CHANNEL_ID, MSG_TS, "eyes")

    @pytest.mark.asyncio
    async def test_no_target_message_errors(self):
        adapter, _ = _make_adapter(agent_reactions=True)
        adapter._add_reaction = AsyncMock(return_value=True)
        result = await adapter.add_reaction(CHANNEL_ID, "eyes")  # no last-inbound
        assert result["success"] is False
        adapter._add_reaction.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_env_toggle(self, monkeypatch):
        monkeypatch.setenv("SLACK_AGENT_REACTIONS", "true")
        adapter, _ = _make_adapter()  # not set in config → env wins
        adapter._add_reaction = AsyncMock(return_value=True)
        result = await adapter.add_reaction(CHANNEL_ID, "eyes", message_id=MSG_TS)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_remove_named_emoji(self):
        adapter, _ = _make_adapter(agent_reactions=True)
        adapter._remove_reaction = AsyncMock(return_value=True)
        result = await adapter.remove_reaction(CHANNEL_ID, message_id=MSG_TS, emoji="tada")
        assert result["success"] is True
        adapter._remove_reaction.assert_awaited_once_with(CHANNEL_ID, MSG_TS, "tada")

    @pytest.mark.asyncio
    async def test_remove_without_emoji_removes_bots_own(self, monkeypatch):
        adapter, client = _make_adapter(agent_reactions=True)
        adapter._remove_reaction = AsyncMock(return_value=True)
        client.reactions_get = AsyncMock(
            return_value={
                "ok": True,
                "message": {
                    "reactions": [
                        {"name": "eyes", "users": [BOT_USER_ID]},
                        {"name": "tada", "users": ["U_SOMEONE_ELSE"]},
                    ]
                },
            }
        )
        result = await adapter.remove_reaction(CHANNEL_ID, message_id=MSG_TS)
        assert result["success"] is True
        # Only the bot's own reaction (eyes) is removed, not someone else's tada.
        adapter._remove_reaction.assert_awaited_once_with(CHANNEL_ID, MSG_TS, "eyes")


class TestWordless:
    def test_wordless_true(self):
        for t in ["👍", ":joy:", ":tada: 🎉", "❗️", "", "   ", "<@U123> :joy:"]:
            assert _is_wordless(t), t

    def test_wordless_false(self):
        for t in ["Done.", "ok", "9:30am", "Here's the link", "reacted with :bike:",
                  "はい"]:
            assert not _is_wordless(t), t


class TestWordlessReplySuppression:
    @pytest.mark.asyncio
    async def test_emoji_only_reply_suppressed_after_reaction(self):
        adapter, _ = _make_adapter(agent_reactions=True)
        adapter._add_reaction = AsyncMock(return_value=True)
        await adapter.add_reaction(CHANNEL_ID, "joy", message_id=MSG_TS)
        assert adapter._suppress_wordless_reply_after_reaction(CHANNEL_ID, ":joy:")

    @pytest.mark.asyncio
    async def test_substantive_reply_not_suppressed(self):
        adapter, _ = _make_adapter(agent_reactions=True)
        adapter._add_reaction = AsyncMock(return_value=True)
        await adapter.add_reaction(CHANNEL_ID, "joy", message_id=MSG_TS)
        assert not adapter._suppress_wordless_reply_after_reaction(
            CHANNEL_ID, "Standup is 9:30am."
        )

    @pytest.mark.asyncio
    async def test_one_shot(self):
        adapter, _ = _make_adapter(agent_reactions=True)
        adapter._add_reaction = AsyncMock(return_value=True)
        await adapter.add_reaction(CHANNEL_ID, "joy", message_id=MSG_TS)
        assert adapter._suppress_wordless_reply_after_reaction(CHANNEL_ID, "👍")
        # Marker consumed — a later emoji-only message is not suppressed.
        assert not adapter._suppress_wordless_reply_after_reaction(CHANNEL_ID, "👍")

    def test_no_reaction_means_no_suppression(self):
        adapter, _ = _make_adapter(agent_reactions=True)
        assert not adapter._suppress_wordless_reply_after_reaction(CHANNEL_ID, "👍")
