"""Tests for /forward and /unforward — including the four bug fixes from
the post-implementation code review.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

import bot
from .conftest import make_pair


# ---------- /forward setup ----------

@pytest.mark.asyncio
async def test_setup_free_user_blocked(
    mock_msg, mock_update, mock_context, mock_get_plan, mock_get_open_pair,
    mock_create_pending_pair,
):
    """Free-tier users see the paid-only message and don't reach pair creation."""
    mock_get_plan.return_value = "free"
    mock_context.args = ["setup"]

    await bot.forward_cmd(mock_update, mock_context)

    mock_msg.reply_text.assert_called_once()
    body = mock_msg.reply_text.call_args.args[0]
    assert "Basic and Pro" in body
    mock_create_pending_pair.assert_not_called()


@pytest.mark.asyncio
async def test_setup_no_pair_creates_new(
    mock_msg, mock_update, mock_context, mock_get_plan, mock_get_open_pair,
    mock_create_pending_pair, mock_get_user_prefs,
):
    """Basic/Pro user with no existing pair → fresh pair created, link returned."""
    mock_get_plan.return_value = "basic"
    mock_get_open_pair.return_value = None
    mock_context.args = ["setup"]

    await bot.forward_cmd(mock_update, mock_context)

    mock_create_pending_pair.assert_called_once()
    body = mock_msg.reply_text.call_args.args[0]
    assert "t.me/TryRespeakBot?start=pair_NEWCODE" in body
    # Tutorial URL is included and matches the user's source language
    assert "tryrespeak.com" in body and "auto-forward-voice-messages-telegram" in body


@pytest.mark.asyncio
async def test_setup_includes_localized_tutorial_url_for_spanish_user(
    mock_msg, mock_update, mock_context, mock_get_plan, mock_get_open_pair,
    mock_create_pending_pair, mock_get_user_prefs,
):
    """Spanish-speaking user gets the /es/ tutorial URL, not the English one."""
    mock_get_plan.return_value = "basic"
    mock_get_open_pair.return_value = None
    mock_get_user_prefs.return_value = ("es", "es")
    mock_context.args = ["setup"]

    await bot.forward_cmd(mock_update, mock_context)

    body = mock_msg.reply_text.call_args.args[0]
    assert "tryrespeak.com/es/blog/auto-forward-voice-messages-telegram" in body


@pytest.mark.asyncio
async def test_setup_english_user_gets_root_tutorial_url(
    mock_msg, mock_update, mock_context, mock_get_plan, mock_get_open_pair,
    mock_create_pending_pair, mock_get_user_prefs,
):
    """English-speaking user gets the no-prefix URL."""
    mock_get_plan.return_value = "basic"
    mock_get_open_pair.return_value = None
    mock_get_user_prefs.return_value = ("en", "es")
    mock_context.args = ["setup"]

    await bot.forward_cmd(mock_update, mock_context)

    body = mock_msg.reply_text.call_args.args[0]
    assert "tryrespeak.com/blog/auto-forward-voice-messages-telegram" in body
    # Make sure we did NOT accidentally use a /en/ prefix
    assert "tryrespeak.com/en/" not in body


@pytest.mark.asyncio
async def test_setup_pending_shows_existing_link(
    mock_msg, mock_update, mock_context, mock_get_plan, mock_get_open_pair,
    mock_create_pending_pair,
):
    """User with a still-fresh pending invite sees the existing link, not a new one."""
    future = datetime.now(timezone.utc) + timedelta(hours=24)
    mock_get_plan.return_value = "basic"
    mock_get_open_pair.return_value = make_pair(
        status="pending", recipient_id=None, pair_code="pair_OLD", expires_at=future
    )
    mock_context.args = ["setup"]

    await bot.forward_cmd(mock_update, mock_context)

    mock_create_pending_pair.assert_not_called()
    body = mock_msg.reply_text.call_args.args[0]
    assert "pair_OLD" in body
    assert "Waiting for them" in body


@pytest.mark.asyncio
async def test_setup_active_pair_shows_status(
    mock_msg, mock_update, mock_context, mock_get_plan, mock_get_open_pair,
    mock_create_pending_pair, mock_get_user_first_name, mock_get_user_prefs,
):
    """Active pair → /forward setup shows the active status, not creates a new pair."""
    mock_get_plan.return_value = "basic"
    mock_get_open_pair.return_value = make_pair(status="active")
    mock_get_user_first_name.return_value = "Maria"
    mock_get_user_prefs.return_value = ("en", "es")
    mock_context.args = ["setup"]

    await bot.forward_cmd(mock_update, mock_context)

    mock_create_pending_pair.assert_not_called()
    body = mock_msg.reply_text.call_args.args[0]
    assert "Forwarding active to Maria" in body


@pytest.mark.asyncio
async def test_setup_paused_pair_shows_paused_message(
    mock_msg, mock_update, mock_context, mock_get_plan, mock_get_open_pair,
    mock_create_pending_pair,
):
    """BUG FIX: /forward setup on a paused pair previously showed the 'active'
    status message — it should show the paused-state message instead."""
    mock_get_plan.return_value = "basic"
    mock_get_open_pair.return_value = make_pair(status="paused_user")
    mock_context.args = ["setup"]

    await bot.forward_cmd(mock_update, mock_context)

    mock_create_pending_pair.assert_not_called()
    body = mock_msg.reply_text.call_args.args[0]
    assert "Forwarding paused" in body
    assert "Forwarding active to" not in body


@pytest.mark.asyncio
async def test_setup_expired_pending_auto_cleaned_then_creates_new(
    mock_msg, mock_update, mock_context, mock_get_plan, mock_get_open_pair,
    mock_create_pending_pair, mock_unpair, mock_get_user_prefs,
):
    """BUG FIX: Pending pair past its 72h expiry should be auto-marked unpaired
    so the user can immediately create a new one via the same command."""
    expired = datetime.now(timezone.utc) - timedelta(hours=1)
    expired_pair = make_pair(
        status="pending", recipient_id=None, pair_code="pair_OLD",
        expires_at=expired,
    )
    mock_get_plan.return_value = "basic"
    mock_get_open_pair.return_value = expired_pair
    mock_context.args = ["setup"]

    await bot.forward_cmd(mock_update, mock_context)

    mock_unpair.assert_called_once_with(mock_context.bot_data["pool"], expired_pair["id"])
    mock_create_pending_pair.assert_called_once()
    body = mock_msg.reply_text.call_args.args[0]
    assert "pair_NEWCODE" in body


# ---------- /forward pause ----------

@pytest.mark.asyncio
async def test_pause_no_pair_says_no_pair(
    mock_msg, mock_update, mock_context, mock_get_plan, mock_get_open_pair,
    mock_pause_pair,
):
    mock_get_plan.return_value = "basic"
    mock_get_open_pair.return_value = None
    mock_context.args = ["pause"]

    await bot.forward_cmd(mock_update, mock_context)

    body = mock_msg.reply_text.call_args.args[0]
    assert "don't have a forwarding pair" in body
    mock_pause_pair.assert_not_called()


@pytest.mark.asyncio
async def test_pause_pending_pair_shows_special_message(
    mock_msg, mock_update, mock_context, mock_get_plan, mock_get_open_pair,
    mock_pause_pair,
):
    """BUG FIX: pausing a not-yet-accepted invite previously said 'no pair',
    misleading. Should explain the invite hasn't been accepted yet."""
    mock_get_plan.return_value = "basic"
    mock_get_open_pair.return_value = make_pair(status="pending", recipient_id=None)
    mock_context.args = ["pause"]

    await bot.forward_cmd(mock_update, mock_context)

    body = mock_msg.reply_text.call_args.args[0]
    assert "hasn't been accepted yet" in body
    mock_pause_pair.assert_not_called()


@pytest.mark.asyncio
async def test_pause_active_pair_pauses(
    mock_msg, mock_update, mock_context, mock_get_plan, mock_get_open_pair,
    mock_pause_pair,
):
    pair = make_pair(status="active")
    mock_get_plan.return_value = "basic"
    mock_get_open_pair.return_value = pair
    mock_context.args = ["pause"]

    await bot.forward_cmd(mock_update, mock_context)

    mock_pause_pair.assert_called_once_with(
        mock_context.bot_data["pool"], pair["id"], "paused_user"
    )
    body = mock_msg.reply_text.call_args.args[0]
    assert "Forwarding paused" in body


@pytest.mark.asyncio
async def test_pause_already_paused_does_nothing(
    mock_msg, mock_update, mock_context, mock_get_plan, mock_get_open_pair,
    mock_pause_pair,
):
    mock_get_plan.return_value = "basic"
    mock_get_open_pair.return_value = make_pair(status="paused_user")
    mock_context.args = ["pause"]

    await bot.forward_cmd(mock_update, mock_context)

    mock_pause_pair.assert_not_called()
    body = mock_msg.reply_text.call_args.args[0]
    assert "Forwarding paused" in body  # status message


# ---------- /forward resume ----------

@pytest.mark.asyncio
async def test_resume_paused_pair_resumes(
    mock_msg, mock_update, mock_context, mock_get_plan, mock_get_open_pair,
    mock_resume_pair, mock_get_user_first_name,
):
    pair = make_pair(status="paused_user")
    mock_get_plan.return_value = "basic"
    mock_get_open_pair.return_value = pair
    mock_get_user_first_name.return_value = "Maria"
    mock_context.args = ["resume"]

    await bot.forward_cmd(mock_update, mock_context)

    mock_resume_pair.assert_called_once_with(mock_context.bot_data["pool"], pair["id"])
    body = mock_msg.reply_text.call_args.args[0]
    assert "Maria" in body and "resumed" in body.lower()


@pytest.mark.asyncio
async def test_resume_active_pair_says_nothing_to_resume(
    mock_msg, mock_update, mock_context, mock_get_plan, mock_get_open_pair,
    mock_resume_pair,
):
    mock_get_plan.return_value = "basic"
    mock_get_open_pair.return_value = make_pair(status="active")
    mock_context.args = ["resume"]

    await bot.forward_cmd(mock_update, mock_context)

    mock_resume_pair.assert_not_called()
    body = mock_msg.reply_text.call_args.args[0]
    assert "Nothing to resume" in body


# ---------- /unforward ----------

@pytest.mark.asyncio
async def test_unforward_no_pair(
    mock_msg, mock_update, mock_context, mock_get_open_pair, mock_unpair,
):
    mock_get_open_pair.return_value = None

    await bot.unforward_cmd(mock_update, mock_context)

    mock_unpair.assert_not_called()
    body = mock_msg.reply_text.call_args.args[0]
    assert "don't have an active forwarding pair" in body


@pytest.mark.asyncio
async def test_unforward_active_pair_unpairs_and_notifies_recipient(
    mock_msg, mock_update, mock_context, mock_get_open_pair, mock_unpair,
    mock_get_user_first_name,
):
    pair = make_pair(status="active", sender_id=1001, recipient_id=2002)
    mock_get_open_pair.return_value = pair
    mock_get_user_first_name.return_value = "Maria"

    await bot.unforward_cmd(mock_update, mock_context)

    mock_unpair.assert_called_once_with(mock_context.bot_data["pool"], pair["id"])
    # Recipient was notified
    mock_context.bot.send_message.assert_called_once()
    call = mock_context.bot.send_message.call_args
    assert call.kwargs["chat_id"] == 2002
    # Sender got confirmation
    body = mock_msg.reply_text.call_args.args[0]
    assert "Unpaired from Maria" in body


@pytest.mark.asyncio
async def test_unforward_pending_cancels_invite(
    mock_msg, mock_update, mock_context, mock_get_open_pair, mock_unpair,
):
    """A pending (un-accepted) invite gets cleanly cancelled by /unforward."""
    pair = make_pair(status="pending", recipient_id=None)
    mock_get_open_pair.return_value = pair

    await bot.unforward_cmd(mock_update, mock_context)

    mock_unpair.assert_called_once_with(mock_context.bot_data["pool"], pair["id"])
    # No recipient yet, so no notification was sent
    mock_context.bot.send_message.assert_not_called()
    body = mock_msg.reply_text.call_args.args[0]
    assert "pending invite" in body
