"""Tests for the pair-related inline-button callbacks:
  - pair_lang_callback (recipient picks language during invite)
  - pair_accept_callback (Accept button)
  - pair_decline_callback (Decline button)
  - forward_undo_callback (Undo button on sender's confirmation)
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot
from .conftest import make_pair


# ---------- pair_lang_callback ----------

@pytest.mark.asyncio
async def test_lang_callback_sets_target_then_shows_buttons(
    mock_callback_query, mock_callback_update, mock_context,
    mock_get_pair_by_code, mock_set_user_target, mock_get_user_first_name,
):
    """Picking a language sets user's tgt and re-renders the message with
    Accept/Decline buttons."""
    mock_callback_query.data = "plang_pair_abc_es"
    pair = make_pair(status="pending", sender_id=2002, recipient_id=None, pair_code="pair_abc")
    mock_get_pair_by_code.return_value = pair
    mock_get_user_first_name.return_value = "Gadi"

    await bot.pair_lang_callback(mock_callback_update, mock_context)

    mock_set_user_target.assert_called_once()
    args = mock_set_user_target.call_args.args
    assert args[2] == "es"  # the language
    # Edited message should now have Accept/Decline buttons
    kwargs = mock_callback_query.edit_message_text.call_args.kwargs
    cbs = [b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert "paccept_pair_abc" in cbs
    assert "pdecline_pair_abc" in cbs


@pytest.mark.asyncio
async def test_lang_callback_unknown_language_no_op(
    mock_callback_query, mock_callback_update, mock_context, mock_set_user_target,
):
    """Malformed/unsupported lang code → silently no-op (defensive against
    callback-data tampering)."""
    mock_callback_query.data = "plang_pair_abc_zz"  # zz not in SUPPORTED

    await bot.pair_lang_callback(mock_callback_update, mock_context)

    mock_set_user_target.assert_not_called()


@pytest.mark.asyncio
async def test_lang_callback_malformed_payload_handled(
    mock_callback_query, mock_callback_update, mock_context, mock_set_user_target,
):
    """If query.data has no underscore (impossible from our buttons but tampered
    callbacks could) → caught by try/except, no crash."""
    mock_callback_query.data = "plang_corrupted"  # no '_<lang>' tail

    # Should not raise
    await bot.pair_lang_callback(mock_callback_update, mock_context)

    mock_set_user_target.assert_not_called()


@pytest.mark.asyncio
async def test_lang_callback_pair_no_longer_pending_shows_expired(
    mock_callback_query, mock_callback_update, mock_context,
    mock_get_pair_by_code, mock_set_user_target,
):
    """Pair was unpaired between invite link click and language pick → recipient
    sees the 'expired' notice."""
    mock_callback_query.data = "plang_pair_abc_es"
    mock_get_pair_by_code.return_value = make_pair(
        status="unpaired", sender_id=2002, recipient_id=None, pair_code="pair_abc"
    )

    await bot.pair_lang_callback(mock_callback_update, mock_context)

    # User's target was still set (race-OK: better to have a setlang than lose it)
    mock_set_user_target.assert_called_once()
    body = mock_callback_query.edit_message_text.call_args.args[0]
    assert "expired" in body.lower() or "no longer valid" in body.lower()


# ---------- pair_accept_callback ----------

@pytest.mark.asyncio
async def test_accept_unknown_pair_no_op(
    mock_callback_query, mock_callback_update, mock_context,
    mock_get_pair_by_code, mock_accept_pair,
):
    """Pair vanished from DB → return silently (button click already answered)."""
    mock_callback_query.data = "paccept_pair_gone"
    mock_get_pair_by_code.return_value = None

    await bot.pair_accept_callback(mock_callback_update, mock_context)

    mock_accept_pair.assert_not_called()


@pytest.mark.asyncio
async def test_accept_already_accepted_shows_expired(
    mock_callback_query, mock_callback_update, mock_context,
    mock_get_pair_by_code, mock_accept_pair,
):
    """Pair status is no longer 'pending' (e.g., race condition where another
    accept landed first) → show expired message, don't re-accept."""
    mock_callback_query.data = "paccept_pair_x"
    mock_get_pair_by_code.return_value = make_pair(status="active", sender_id=2002, recipient_id=9999)

    await bot.pair_accept_callback(mock_callback_update, mock_context)

    mock_accept_pair.assert_not_called()
    body = mock_callback_query.edit_message_text.call_args.args[0]
    assert "expired" in body.lower() or "no longer valid" in body.lower()


@pytest.mark.asyncio
async def test_accept_happy_path_activates_and_notifies_both(
    mock_callback_query, mock_callback_update, mock_context,
    mock_get_pair_by_code, mock_accept_pair,
    mock_get_user_first_name, mock_get_user_prefs,
):
    """Successful accept: pair activated, recipient sees 'paired with X',
    sender gets a separate notification message."""
    mock_callback_query.data = "paccept_pair_xyz"
    pair = make_pair(status="pending", sender_id=2002, recipient_id=None, pair_code="pair_xyz")
    mock_get_pair_by_code.return_value = pair
    mock_get_user_first_name.return_value = "Gadi"
    mock_get_user_prefs.return_value = ("en", "es")

    await bot.pair_accept_callback(mock_callback_update, mock_context)

    # DB updated to active with this user as recipient
    mock_accept_pair.assert_called_once()
    args = mock_accept_pair.call_args.args
    assert args[1] == pair["id"]
    assert args[2] == 1001  # mock_user.id

    # Recipient saw confirmation
    recipient_msg = mock_callback_query.edit_message_text.call_args.args[0]
    assert "Paired with Gadi" in recipient_msg

    # Sender was notified separately
    mock_context.bot.send_message.assert_called_once()
    sender_msg = mock_context.bot.send_message.call_args.kwargs
    assert sender_msg["chat_id"] == 2002


@pytest.mark.asyncio
async def test_accept_sender_notification_failure_doesnt_block_accept(
    mock_callback_query, mock_callback_update, mock_context,
    mock_get_pair_by_code, mock_accept_pair,
    mock_get_user_first_name, mock_get_user_prefs,
):
    """If sender blocked the bot before recipient accepted, accept still goes
    through — recipient sees confirmation, sender notification fails silently."""
    mock_callback_query.data = "paccept_pair_xyz"
    mock_get_pair_by_code.return_value = make_pair(
        status="pending", sender_id=2002, recipient_id=None, pair_code="pair_xyz",
    )
    mock_get_user_first_name.return_value = "Gadi"
    mock_get_user_prefs.return_value = ("en", "es")
    mock_context.bot.send_message.side_effect = Exception("Forbidden: bot was blocked by the user")

    # Should NOT raise
    await bot.pair_accept_callback(mock_callback_update, mock_context)

    mock_accept_pair.assert_called_once()
    # Recipient still got their confirmation
    mock_callback_query.edit_message_text.assert_called_once()


# ---------- pair_decline_callback ----------

@pytest.mark.asyncio
async def test_decline_pending_unpairs_and_notifies_sender(
    mock_callback_query, mock_callback_update, mock_context,
    mock_get_pair_by_code, mock_unpair,
):
    """Decline on a pending invite → pair marked unpaired, sender notified."""
    mock_callback_query.data = "pdecline_pair_x"
    pair = make_pair(status="pending", sender_id=2002, recipient_id=None, pair_code="pair_x")
    mock_get_pair_by_code.return_value = pair

    await bot.pair_decline_callback(mock_callback_update, mock_context)

    mock_unpair.assert_called_once_with(mock_context.bot_data["pool"], pair["id"])
    mock_context.bot.send_message.assert_called_once()
    assert mock_context.bot.send_message.call_args.kwargs["chat_id"] == 2002


@pytest.mark.asyncio
async def test_decline_already_active_does_not_unpair(
    mock_callback_query, mock_callback_update, mock_context,
    mock_get_pair_by_code, mock_unpair,
):
    """Race: pair somehow already active → don't unpair (only pending pairs
    get unpaired by decline). User still sees the decline-done message."""
    mock_callback_query.data = "pdecline_pair_x"
    mock_get_pair_by_code.return_value = make_pair(
        status="active", sender_id=2002, recipient_id=1001, pair_code="pair_x",
    )

    await bot.pair_decline_callback(mock_callback_update, mock_context)

    mock_unpair.assert_not_called()  # status wasn't pending


# ---------- forward_undo_callback ----------

@pytest.mark.asyncio
async def test_undo_deletes_recipient_message_and_strips_button(
    mock_callback_query, mock_callback_update, mock_context, mock_pool,
):
    """Undo → bot.delete_message removes the forwarded message from recipient's
    chat; the sender's confirmation gets its Undo button stripped."""
    mock_callback_query.data = "undo_2002_555"

    await bot.forward_undo_callback(mock_callback_update, mock_context)

    mock_context.bot.delete_message.assert_called_once_with(
        chat_id=2002, message_id=555,
    )
    mock_callback_query.edit_message_reply_markup.assert_called_once_with(reply_markup=None)
    # forward_messages row purged so a Reply on the now-deleted message
    # doesn't try to route through the deleted forward
    delete_calls = [c for c in mock_pool.execute.call_args_list
                    if "DELETE FROM forward_messages" in c.args[0]]
    assert len(delete_calls) == 1


@pytest.mark.asyncio
async def test_undo_too_old_shows_alert(
    mock_callback_query, mock_callback_update, mock_context,
):
    """Telegram refuses to delete (>48h or already deleted) → user sees
    'too late to undo' alert via query.answer."""
    mock_callback_query.data = "undo_2002_555"
    mock_context.bot.delete_message.side_effect = Exception("Bad Request: message can't be deleted")

    await bot.forward_undo_callback(mock_callback_update, mock_context)

    # answer() called with show_alert=True somewhere
    answer_calls = mock_callback_query.answer.call_args_list
    assert any(c.kwargs.get("show_alert") is True for c in answer_calls)


@pytest.mark.asyncio
async def test_undo_malformed_callback_data_silent(
    mock_callback_query, mock_callback_update, mock_context,
):
    """Tampered callback_data → don't crash, just answer the query and exit."""
    mock_callback_query.data = "undo_garbage"

    # Should not raise
    await bot.forward_undo_callback(mock_callback_update, mock_context)

    mock_context.bot.delete_message.assert_not_called()
