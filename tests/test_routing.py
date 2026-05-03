"""Tests for _determine_routing — the core voice-message routing decision.

Routing rules (from spec):
  - Reply to a previously-forwarded message → route through that pair
  - Else if user has an active outgoing pair (sender role) → auto-forward
  - Else → solo translation
"""
from unittest.mock import MagicMock

import pytest

import bot
from .conftest import make_pair


@pytest.mark.asyncio
async def test_no_pair_returns_solo(
    mock_pool, mock_msg, mock_get_active_pair, mock_find_pair_by_replied
):
    """No outgoing pair, no reply context → solo translation."""
    mock_get_active_pair.return_value = None
    mock_find_pair_by_replied.return_value = None

    mode, pair = await bot._determine_routing(mock_pool, user_id=1001, msg=mock_msg)

    assert mode == "solo"
    assert pair is None


@pytest.mark.asyncio
async def test_outgoing_pair_routes_through_it(
    mock_pool, mock_msg, mock_get_active_pair, mock_find_pair_by_replied
):
    """User has active outgoing pair (channel owner) → auto-forward."""
    pair = make_pair(status="active", sender_id=1001, recipient_id=2002)
    mock_get_active_pair.return_value = pair
    mock_find_pair_by_replied.return_value = None

    mode, returned = await bot._determine_routing(mock_pool, user_id=1001, msg=mock_msg)

    assert mode == "pair"
    assert returned["sender_id"] == 1001
    assert returned["recipient_id"] == 2002


@pytest.mark.asyncio
async def test_reply_to_forwarded_message_routes_to_replied_pair(
    mock_pool, mock_msg, mock_get_active_pair, mock_find_pair_by_replied
):
    """User replies to a forwarded message → routes through THAT pair regardless
    of any outgoing pair the user might also have."""
    # Set up reply context
    mock_msg.reply_to_message = MagicMock()
    mock_msg.reply_to_message.message_id = 999
    mock_msg.chat_id = 2002

    # The replied message belongs to pair_id=1
    pair_replied = make_pair(status="active", sender_id=1001, recipient_id=2002, paid_id=1)
    mock_find_pair_by_replied.return_value = pair_replied

    # User ALSO has an outgoing pair (paid_id=2) — reply should still take precedence
    pair_outgoing = make_pair(status="active", sender_id=2002, recipient_id=3003, paid_id=2)
    mock_get_active_pair.return_value = pair_outgoing

    mode, pair = await bot._determine_routing(mock_pool, user_id=2002, msg=mock_msg)

    assert mode == "pair"
    assert pair["id"] == 1, "Should use the replied pair, not the outgoing pair"


@pytest.mark.asyncio
async def test_recipient_only_user_fresh_voice_is_solo(
    mock_pool, mock_msg, mock_get_active_pair, mock_find_pair_by_replied
):
    """A user who is ONLY a recipient (not a sender) and sends a fresh voice
    (no reply) → falls through to solo. This is the spec's core safety: no
    auto-forwarding from the recipient side without an explicit reply."""
    # Maria is a recipient in some pair, but has no OUTGOING pair as sender.
    mock_get_active_pair.return_value = None
    mock_find_pair_by_replied.return_value = None

    mode, pair = await bot._determine_routing(mock_pool, user_id=2002, msg=mock_msg)

    assert mode == "solo"


@pytest.mark.asyncio
async def test_reply_to_non_pair_message_falls_through_to_outgoing(
    mock_pool, mock_msg, mock_get_active_pair, mock_find_pair_by_replied
):
    """User replies to a regular bot message that isn't from a pair (e.g.,
    a /help reply) → reply lookup returns None → falls through to outgoing
    pair check."""
    mock_msg.reply_to_message = MagicMock()
    mock_msg.reply_to_message.message_id = 555
    mock_find_pair_by_replied.return_value = None  # not in forward_messages

    pair_outgoing = make_pair(status="active", sender_id=1001, recipient_id=2002)
    mock_get_active_pair.return_value = pair_outgoing

    mode, pair = await bot._determine_routing(mock_pool, user_id=1001, msg=mock_msg)

    assert mode == "pair"
    assert pair["recipient_id"] == 2002


@pytest.mark.asyncio
async def test_inactive_pair_does_not_auto_forward(
    mock_pool, mock_msg, mock_get_active_pair, mock_find_pair_by_replied
):
    """If the user's outgoing pair is paused, get_active_pair_for_sender returns
    None and the user falls through to solo translation."""
    # get_active_pair_for_sender filters status='active' so a paused pair
    # would not be returned by that helper at all.
    mock_get_active_pair.return_value = None
    mock_find_pair_by_replied.return_value = None

    mode, pair = await bot._determine_routing(mock_pool, user_id=1001, msg=mock_msg)

    assert mode == "solo"
