"""Tests for admin-only commands."""
from unittest.mock import AsyncMock

import pytest

import bot


@pytest.fixture
def mock_set_plan(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(bot, "set_plan", mock)
    return mock


@pytest.mark.asyncio
async def test_setplan_non_admin_ignored(
    mock_msg, mock_update, mock_context, mock_set_plan,
):
    """A non-admin caller gets no response and no plan change."""
    mock_update.effective_user.id = 1001  # not ADMIN_ID
    mock_context.args = ["pro"]

    await bot.setplan_cmd(mock_update, mock_context)

    mock_set_plan.assert_not_called()
    mock_msg.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_setplan_bad_arg_shows_usage(
    mock_msg, mock_update, mock_context, mock_set_plan,
):
    """An unrecognized plan name shows usage and changes nothing."""
    mock_update.effective_user.id = bot.ADMIN_ID
    mock_context.args = ["platinum"]

    await bot.setplan_cmd(mock_update, mock_context)

    mock_set_plan.assert_not_called()
    assert "Usage:" in mock_msg.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_setplan_defaults_to_admin_self(
    mock_msg, mock_update, mock_context, mock_set_plan,
):
    """With no user_id, the admin's own account is changed."""
    mock_update.effective_user.id = bot.ADMIN_ID
    mock_context.args = ["pro"]

    await bot.setplan_cmd(mock_update, mock_context)

    mock_set_plan.assert_called_once()
    assert mock_set_plan.call_args.args[1] == bot.ADMIN_ID
    assert mock_set_plan.call_args.args[2] == "pro"


@pytest.mark.asyncio
async def test_setplan_targets_given_user_id(
    mock_msg, mock_update, mock_context, mock_set_plan,
):
    """An explicit user_id argument is the account that gets changed."""
    mock_update.effective_user.id = bot.ADMIN_ID
    mock_context.args = ["basic", "2002"]

    await bot.setplan_cmd(mock_update, mock_context)

    assert mock_set_plan.call_args.args[1] == 2002
    assert mock_set_plan.call_args.args[2] == "basic"
