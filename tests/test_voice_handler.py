"""Tests for handle_voice — the core pipeline: solo vs pair routing, quota
attribution to the channel-owner, target-language selection, and failure modes.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot
from .conftest import make_pair


# ---------- Solo path (no pair) ----------

@pytest.mark.asyncio
async def test_solo_translates_with_user_target_lang(
    mock_msg, mock_update, mock_context, voice_pipeline,
):
    """No pair, user has tgt='es' → translate transcript into Spanish, send
    voice + transcript + translation to the user's own chat."""
    voice_pipeline["get_user_prefs"].return_value = ("en", "es")
    voice_pipeline["get_active_pair"].return_value = None
    voice_pipeline["find_pair_by_replied"].return_value = None

    await bot.handle_voice(mock_update, mock_context)

    voice_pipeline["transcribe"].assert_called_once()
    voice_pipeline["translate"].assert_called_once()
    args = voice_pipeline["translate"].call_args.args
    assert args[2] == "es"  # target language
    # Voice was sent to the user's own chat (msg.chat_id == user.id == 1001)
    sent_voice_calls = mock_context.bot.send_voice.call_args_list
    assert len(sent_voice_calls) == 1
    assert sent_voice_calls[0].kwargs["chat_id"] == 1001
    voice_pipeline["increment_usage"].assert_called_once()
    # Quota goes to the user themselves
    assert voice_pipeline["increment_usage"].call_args.args[1] == 1001


@pytest.mark.asyncio
async def test_solo_no_setlang_says_setup_first(
    mock_msg, mock_update, mock_context, voice_pipeline,
):
    """User has no target_lang and no pair → 'finish setup first' error,
    no translation done."""
    voice_pipeline["get_user_prefs"].return_value = (None, None)
    voice_pipeline["get_active_pair"].return_value = None
    voice_pipeline["find_pair_by_replied"].return_value = None

    await bot.handle_voice(mock_update, mock_context)

    voice_pipeline["translate"].assert_not_called()
    body = mock_msg.reply_text.call_args.args[0]
    assert "finish setup" in body.lower()


@pytest.mark.asyncio
async def test_solo_at_quota_limit_blocks(
    mock_msg, mock_update, mock_context, voice_pipeline,
):
    """User on Free tier at 10/10 messages → limit_hit message, no translate."""
    voice_pipeline["get_user_prefs"].return_value = ("en", "es")
    voice_pipeline["get_active_pair"].return_value = None
    voice_pipeline["find_pair_by_replied"].return_value = None
    voice_pipeline["get_plan"].return_value = "free"
    voice_pipeline["get_message_count"].return_value = 10  # at limit

    await bot.handle_voice(mock_update, mock_context)

    voice_pipeline["translate"].assert_not_called()
    body = mock_msg.reply_text.call_args.args[0]
    assert "10 free" in body or "used all" in body.lower()


@pytest.mark.asyncio
async def test_voice_too_long_rejected(
    mock_msg, mock_update, mock_context, voice_pipeline,
):
    """Voice >5min (300s) → too_long message, no translate."""
    mock_msg.voice.duration = 400
    voice_pipeline["get_user_prefs"].return_value = ("en", "es")

    await bot.handle_voice(mock_update, mock_context)

    voice_pipeline["transcribe"].assert_not_called()
    body = mock_msg.reply_text.call_args.args[0]
    assert "400" in body or "5 min" in body.lower()


@pytest.mark.asyncio
async def test_no_speech_returns_friendly_message(
    mock_msg, mock_update, mock_context, voice_pipeline,
    mock_status_msg,
):
    """Whisper returned empty transcript → user sees 'no speech' prompt."""
    voice_pipeline["get_user_prefs"].return_value = ("en", "es")
    voice_pipeline["get_active_pair"].return_value = None
    voice_pipeline["find_pair_by_replied"].return_value = None
    voice_pipeline["transcribe"].return_value = ("", "en")

    await bot.handle_voice(mock_update, mock_context)

    voice_pipeline["translate"].assert_not_called()
    # status message was edited to no_speech
    edits = [c.args[0] for c in mock_status_msg.edit_text.call_args_list]
    assert any("speech" in e.lower() or "again" in e.lower() for e in edits)


# ---------- Pair path: channel-owner sending ----------

@pytest.mark.asyncio
async def test_pair_owner_sending_translates_to_recipient_lang(
    mock_msg, mock_update, mock_context, voice_pipeline,
):
    """Channel-owner (Gadi) sends voice while paired with Maria — translate
    INTO Maria's target language, send voice to Maria's chat, count quota
    against Gadi (the channel-owner)."""
    pair = make_pair(status="active", sender_id=1001, recipient_id=2002)
    voice_pipeline["get_active_pair"].return_value = pair
    voice_pipeline["find_pair_by_replied"].return_value = None
    # First call: user.id (1001 == sender) — matters less here. Second call:
    # other_user_id (2002) → Maria's tgt = pt.
    voice_pipeline["get_user_prefs"].side_effect = [
        ("en", "es"),  # for user (Gadi) — used in solo fallback path; harmless
        ("pt", "pt"),  # for other_user (Maria)
    ]

    await bot.handle_voice(mock_update, mock_context)

    # Translation goes INTO Maria's tgt
    args = voice_pipeline["translate"].call_args.args
    assert args[2] == "pt", "Should translate to recipient (Maria's) tgt language"
    # Voice was sent to Maria's chat
    sent_voice_calls = mock_context.bot.send_voice.call_args_list
    assert len(sent_voice_calls) == 1
    assert sent_voice_calls[0].kwargs["chat_id"] == 2002
    # Quota counted against the channel-owner (Gadi, sender_id)
    voice_pipeline["increment_usage"].assert_called_once()
    assert voice_pipeline["increment_usage"].call_args.args[1] == 1001
    # forward_messages row recorded so reply-routing works
    voice_pipeline["record_forward"].assert_called_once()


@pytest.mark.asyncio
async def test_pair_recipient_replying_routes_back_with_owner_quota(
    mock_msg, mock_update, mock_context, mock_user, voice_pipeline,
):
    """Maria (recipient, user.id=2002) replies to a forwarded message → bot
    routes voice back to Gadi (sender_id=1001), translates into HIS lang,
    counts the quota against GADI not Maria."""
    mock_user.id = 2002
    mock_user.first_name = "Maria"
    mock_msg.chat_id = 2002
    mock_msg.reply_to_message = MagicMock()
    mock_msg.reply_to_message.message_id = 555

    pair = make_pair(status="active", sender_id=1001, recipient_id=2002)
    voice_pipeline["find_pair_by_replied"].return_value = pair
    voice_pipeline["get_active_pair"].return_value = None
    # Maria's prefs (current user) and Gadi's prefs (other user)
    voice_pipeline["get_user_prefs"].side_effect = [
        ("pt", "pt"),  # Maria
        ("en", "en"),  # Gadi (other side, where voice should land)
    ]

    await bot.handle_voice(mock_update, mock_context)

    # Voice translated INTO Gadi's tgt = en
    args = voice_pipeline["translate"].call_args.args
    assert args[2] == "en"
    # Voice sent to Gadi's chat (1001), not Maria's (2002)
    assert mock_context.bot.send_voice.call_args_list[0].kwargs["chat_id"] == 1001
    # Quota counted against the CHANNEL-OWNER (Gadi, sender_id=1001) NOT
    # the actual voice-sender (Maria, 2002)
    voice_pipeline["increment_usage"].assert_called_once()
    assert voice_pipeline["increment_usage"].call_args.args[1] == 1001


@pytest.mark.asyncio
async def test_pair_owner_at_quota_blocks_recipient_reply(
    mock_msg, mock_update, mock_context, mock_user, voice_pipeline,
):
    """If channel-owner is over their quota, recipient's REPLY is blocked too —
    the recipient sees a forward_failed_quota message, no translate."""
    mock_user.id = 2002
    mock_msg.chat_id = 2002
    mock_msg.reply_to_message = MagicMock()
    mock_msg.reply_to_message.message_id = 555

    pair = make_pair(status="active", sender_id=1001, recipient_id=2002)
    voice_pipeline["find_pair_by_replied"].return_value = pair
    voice_pipeline["get_user_prefs"].side_effect = [("pt", "pt"), ("en", "en")]
    voice_pipeline["get_plan"].return_value = "basic"  # Gadi's plan
    voice_pipeline["get_message_count"].return_value = 100  # Gadi at Basic limit

    await bot.handle_voice(mock_update, mock_context)

    voice_pipeline["translate"].assert_not_called()
    body = mock_msg.reply_text.call_args.args[0]
    assert "monthly limit" in body.lower() or "couldn't forward" in body.lower()


@pytest.mark.asyncio
async def test_pair_send_to_recipient_blocked_notifies_sender(
    mock_msg, mock_update, mock_context, voice_pipeline, mock_status_msg,
):
    """Recipient blocked the bot → send_voice raises → sender sees
    'couldn't forward — they may have blocked the bot' message, no quota
    increment."""
    pair = make_pair(status="active", sender_id=1001, recipient_id=2002)
    voice_pipeline["get_active_pair"].return_value = pair
    voice_pipeline["find_pair_by_replied"].return_value = None
    voice_pipeline["get_user_prefs"].side_effect = [("en", "es"), ("pt", "pt")]
    mock_context.bot.send_voice.side_effect = Exception("Forbidden: bot was blocked")

    await bot.handle_voice(mock_update, mock_context)

    # Translation happened (we paid OpenAI) but no quota increment because
    # the forward never landed
    voice_pipeline["translate"].assert_called_once()
    voice_pipeline["increment_usage"].assert_not_called()
    voice_pipeline["record_forward"].assert_not_called()
    # User saw the blocked message
    edits = [c.args[0] for c in mock_status_msg.edit_text.call_args_list]
    assert any("blocked" in e.lower() for e in edits)


@pytest.mark.asyncio
async def test_pair_other_user_no_setlang_falls_back_to_solo(
    mock_msg, mock_update, mock_context, voice_pipeline,
):
    """If the other side of the pair has no target_lang somehow (data bug),
    voice falls back to solo translation against the current user's tgt
    instead of dropping the message."""
    pair = make_pair(status="active", sender_id=1001, recipient_id=2002)
    voice_pipeline["get_active_pair"].return_value = pair
    voice_pipeline["find_pair_by_replied"].return_value = None
    voice_pipeline["get_user_prefs"].side_effect = [
        ("en", "es"),     # current user (Gadi) tgt = es
        (None, None),     # other user (Maria) has no tgt
    ]

    await bot.handle_voice(mock_update, mock_context)

    # Solo fallback: translate to current user's tgt = es
    args = voice_pipeline["translate"].call_args.args
    assert args[2] == "es"
    # Voice sent to user's own chat (1001), not Maria's (2002)
    assert mock_context.bot.send_voice.call_args_list[0].kwargs["chat_id"] == 1001
    # No forward_messages row created (it's solo)
    voice_pipeline["record_forward"].assert_not_called()
