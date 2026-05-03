"""Tests for /voice command and the voice-pick callbacks.

The voice picker sends two preview voice messages, each with a 'Use this
voice' button. Tapping a button calls set_user_voice and confirms.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot


# ---------- /voice command ----------

@pytest.mark.asyncio
async def test_voice_cmd_sends_prompt_and_two_previews(
    mock_msg, mock_update, mock_context, mock_update_user_info,
):
    """Running /voice posts a prompt + 2 voice messages (alloy + nova)."""
    # mock effective_chat
    mock_update.effective_chat = MagicMock()
    mock_update.effective_chat.id = 1001

    await bot.voice_cmd(mock_update, mock_context)

    # Initial text prompt
    mock_msg.reply_text.assert_called_once()
    body = mock_msg.reply_text.call_args.args[0]
    assert "voice" in body.lower()

    # Two send_voice calls (alloy + nova previews)
    sent = mock_context.bot.send_voice.call_args_list
    assert len(sent) == 2
    # Captions are the labels
    captions = [c.kwargs["caption"] for c in sent]
    assert any("Alloy" in cap for cap in captions)
    assert any("Nova" in cap for cap in captions)

    # Each voice message has a "Use this voice" button with voice_<id> callback
    for c in sent:
        kb = c.kwargs["reply_markup"]
        callback_data = kb.inline_keyboard[0][0].callback_data
        assert callback_data.startswith("voice_")
        assert callback_data[len("voice_"):] in ("alloy", "nova")


# ---------- voice_callback (Use this voice) ----------

@pytest.mark.asyncio
async def test_voice_callback_sets_voice_and_confirms(
    mock_callback_query, mock_callback_update, mock_context, monkeypatch,
):
    """Tapping 'Use Nova' updates DB and sends a confirmation."""
    set_voice_mock = AsyncMock()
    get_voice_mock = AsyncMock(return_value="alloy")  # currently on alloy
    monkeypatch.setattr(bot, "set_user_voice", set_voice_mock)
    monkeypatch.setattr(bot, "get_user_voice", get_voice_mock)

    mock_callback_query.data = "voice_nova"

    await bot.voice_callback(mock_callback_update, mock_context)

    set_voice_mock.assert_called_once()
    args = set_voice_mock.call_args.args
    assert args[2] == "nova"
    # Confirmation sent
    mock_context.bot.send_message.assert_called_once()
    confirmation = mock_context.bot.send_message.call_args.kwargs["text"]
    assert "Nova" in confirmation


@pytest.mark.asyncio
async def test_voice_callback_already_set_no_op(
    mock_callback_query, mock_callback_update, mock_context, monkeypatch,
):
    """Tapping the voice you're already using → 'Already on X' alert, no DB write."""
    set_voice_mock = AsyncMock()
    get_voice_mock = AsyncMock(return_value="nova")
    monkeypatch.setattr(bot, "set_user_voice", set_voice_mock)
    monkeypatch.setattr(bot, "get_user_voice", get_voice_mock)

    mock_callback_query.data = "voice_nova"

    await bot.voice_callback(mock_callback_update, mock_context)

    set_voice_mock.assert_not_called()
    # No confirmation message; alert is shown via query.answer instead
    mock_context.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_voice_callback_unsupported_voice_no_op(
    mock_callback_query, mock_callback_update, mock_context, monkeypatch,
):
    """Tampered callback_data with an unknown voice name → silent no-op."""
    set_voice_mock = AsyncMock()
    monkeypatch.setattr(bot, "set_user_voice", set_voice_mock)

    mock_callback_query.data = "voice_evilvoice"

    await bot.voice_callback(mock_callback_update, mock_context)

    set_voice_mock.assert_not_called()


# ---------- Voice flows through to synthesize ----------

@pytest.mark.asyncio
async def test_voice_handler_uses_speakers_voice_for_synthesis(
    mock_msg, mock_update, mock_context, voice_pipeline,
):
    """When a user sends a voice message, synthesize is called with the user's
    chosen voice (not the env default)."""
    voice_pipeline["get_user_prefs"].return_value = ("en", "es")
    voice_pipeline["get_active_pair"].return_value = None
    voice_pipeline["find_pair_by_replied"].return_value = None
    voice_pipeline["get_user_voice"].return_value = "nova"

    # Spy on synthesize via the existing fake; we re-monkeypatch with a recorder
    captured = {}

    async def fake_synth(text, out_path, voice="alloy"):
        captured["voice"] = voice
        out_path.write_bytes(b"X")
        return out_path

    import bot as bot_mod
    bot_mod.synthesize = fake_synth

    await bot.handle_voice(mock_update, mock_context)

    assert captured.get("voice") == "nova", \
        f"Expected synthesize to receive voice='nova', got {captured.get('voice')!r}"
