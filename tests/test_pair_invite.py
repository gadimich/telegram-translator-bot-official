"""Tests for _handle_pair_invite — the entry point for /start pair_<code>.

Recipients hit this when they click a sender's pair link. The function decides
between five outcomes:
 - Code unrecognized → return False (caller falls back to normal welcome)
 - Code valid but already-active for this user → 'already paired'
 - Code valid but pair status != pending (decline/unpaired/etc) → 'expired'
 - Code valid but past 72h expiry → 'expired' + auto-mark-unpaired in DB
 - Code valid + recipient has no setlang → show language picker first
 - Code valid + recipient has setlang → show Accept/Decline buttons
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

import bot
from .conftest import make_pair


@pytest.mark.asyncio
async def test_invalid_code_returns_false(
    mock_msg, mock_update, mock_context, mock_get_pair_by_code,
):
    """Unknown pair code → False, so caller falls through to normal welcome."""
    mock_get_pair_by_code.return_value = None

    result = await bot._handle_pair_invite(
        mock_update, mock_context, "pair_unknown"
    )

    assert result is False
    mock_msg.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_already_active_for_this_user(
    mock_msg, mock_update, mock_context, mock_get_pair_by_code,
    mock_get_user_first_name,
):
    """User clicks their own already-accepted pair → friendly 'already paired'."""
    pair = make_pair(status="active", sender_id=2002, recipient_id=1001)
    mock_get_pair_by_code.return_value = pair

    result = await bot._handle_pair_invite(mock_update, mock_context, "pair_x")

    assert result is True
    body = mock_msg.reply_text.call_args.args[0]
    assert "already paired" in body.lower()


@pytest.mark.asyncio
async def test_unpaired_status_shown_as_expired(
    mock_msg, mock_update, mock_context, mock_get_pair_by_code,
    mock_get_user_first_name,
):
    """A pair that was already declined / unpaired → 'expired, ask for new link'."""
    pair = make_pair(status="unpaired", sender_id=2002, recipient_id=None)
    mock_get_pair_by_code.return_value = pair
    mock_get_user_first_name.return_value = "Gadi"

    result = await bot._handle_pair_invite(mock_update, mock_context, "pair_x")

    assert result is True
    body = mock_msg.reply_text.call_args.args[0]
    assert "expired" in body.lower() or "no longer valid" in body.lower()


@pytest.mark.asyncio
async def test_expired_pending_marks_unpaired_and_messages(
    mock_msg, mock_update, mock_context, mock_get_pair_by_code,
    mock_get_user_first_name, mock_pool,
):
    """A pending pair that's past its 72h expiry → 'expired' + auto-mark-unpaired."""
    expired = datetime.now(timezone.utc) - timedelta(hours=1)
    pair = make_pair(
        status="pending", sender_id=2002, recipient_id=None,
        pair_code="pair_old", expires_at=expired,
    )
    mock_get_pair_by_code.return_value = pair
    mock_get_user_first_name.return_value = "Gadi"

    result = await bot._handle_pair_invite(mock_update, mock_context, "pair_old")

    assert result is True
    # Should issue an UPDATE to mark the pair unpaired
    mock_pool.execute.assert_called_once()
    sql = mock_pool.execute.call_args.args[0]
    assert "unpaired" in sql.lower() or "UPDATE forward_pairs" in sql
    body = mock_msg.reply_text.call_args.args[0]
    assert "expired" in body.lower() or "no longer valid" in body.lower()


@pytest.mark.asyncio
async def test_no_setlang_shows_language_picker(
    mock_msg, mock_update, mock_context, mock_get_pair_by_code,
    mock_get_user_first_name, mock_get_user_prefs,
):
    """Recipient hasn't picked their language yet → show language picker keyboard."""
    future = datetime.now(timezone.utc) + timedelta(hours=24)
    pair = make_pair(
        status="pending", sender_id=2002, recipient_id=None,
        pair_code="pair_xyz", expires_at=future,
    )
    mock_get_pair_by_code.return_value = pair
    mock_get_user_first_name.return_value = "Gadi"
    # No tgt set
    mock_get_user_prefs.return_value = (None, None)

    result = await bot._handle_pair_invite(mock_update, mock_context, "pair_xyz")

    assert result is True
    # The keyboard should have been passed; we sniff for "language" in the prompt
    body = mock_msg.reply_text.call_args.args[0]
    assert "language" in body.lower()
    kwargs = mock_msg.reply_text.call_args.kwargs
    assert "reply_markup" in kwargs
    # Buttons should use plang_<code>_<lang> callback_data
    buttons = kwargs["reply_markup"].inline_keyboard
    flat_data = [btn.callback_data for row in buttons for btn in row]
    assert all(d.startswith("plang_pair_xyz_") for d in flat_data), \
        f"All buttons should use plang_<code>_ prefix, got: {flat_data[:3]}..."


@pytest.mark.asyncio
async def test_with_setlang_shows_accept_decline(
    mock_msg, mock_update, mock_context, mock_get_pair_by_code,
    mock_get_user_first_name, mock_get_user_prefs,
):
    """Recipient has a setlang → skip language picker, show Accept/Decline."""
    future = datetime.now(timezone.utc) + timedelta(hours=24)
    pair = make_pair(
        status="pending", sender_id=2002, recipient_id=None,
        pair_code="pair_abc", expires_at=future,
    )
    mock_get_pair_by_code.return_value = pair
    mock_get_user_first_name.return_value = "Gadi"
    mock_get_user_prefs.return_value = ("en", "es")

    result = await bot._handle_pair_invite(mock_update, mock_context, "pair_abc")

    assert result is True
    kwargs = mock_msg.reply_text.call_args.kwargs
    assert "reply_markup" in kwargs
    buttons = kwargs["reply_markup"].inline_keyboard
    callbacks = [btn.callback_data for row in buttons for btn in row]
    assert "paccept_pair_abc" in callbacks
    assert "pdecline_pair_abc" in callbacks
