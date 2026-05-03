"""Test configuration and shared fixtures.

bot.py reads BOT_TOKEN and OPENAI_API_KEY at module-import time, so we set
those in env BEFORE bot is imported anywhere. Tests never make real network
calls; the bot's external dependencies are mocked via the fixtures below.
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Set required env vars before any bot.py import.
os.environ.setdefault("BOT_TOKEN", "TEST:fake-token")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-fake")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")

# Make project root importable as `bot`/`strings`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402
from strings import UI_STRINGS  # noqa: E402


# ----- Pool / DB mocks -----

@pytest.fixture
def mock_pool():
    """Mock asyncpg.Pool. Tests can override fetchrow/execute/fetchval."""
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=None)
    pool.execute = AsyncMock(return_value=None)
    pool.fetchval = AsyncMock(return_value=None)
    return pool


# ----- Telegram primitive mocks -----

@pytest.fixture
def mock_user():
    """Sender user (default: Gadi, channel-owner)."""
    user = MagicMock()
    user.id = 1001
    user.first_name = "Gadi"
    user.username = "gadi"
    user.language_code = "en"
    return user


@pytest.fixture
def mock_msg(mock_user):
    """Voice message from mock_user."""
    msg = MagicMock()
    msg.chat_id = 1001
    msg.from_user = mock_user
    msg.voice = MagicMock(duration=10)
    msg.audio = None
    msg.reply_to_message = None
    msg.reply_text = AsyncMock()
    return msg


@pytest.fixture
def mock_update(mock_user, mock_msg):
    update = MagicMock()
    update.effective_user = mock_user
    update.message = mock_msg
    return update


@pytest.fixture
def mock_context(mock_pool):
    ctx = MagicMock()
    ctx.bot_data = {"pool": mock_pool}
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.bot.send_voice = AsyncMock()
    ctx.bot.delete_message = AsyncMock()
    ctx.args = []
    return ctx


# ----- bot helper monkeypatches -----
#
# Replace lazy-translated string lookups with the English UI_STRINGS dict, so
# tests assert against known English copy without round-tripping OpenAI.

@pytest.fixture(autouse=True)
def patch_strings(monkeypatch):
    async def fake_strings(pool, lang):
        return UI_STRINGS

    async def fake_strings_for(pool, user_id):
        return UI_STRINGS

    monkeypatch.setattr(bot, "_strings", fake_strings)
    monkeypatch.setattr(bot, "_strings_for", fake_strings_for)


# Direct monkeypatches for DB helpers — much cleaner than configuring deep
# fetchrow side_effects per test.

@pytest.fixture
def mock_get_plan(monkeypatch):
    mock = AsyncMock(return_value="basic")
    monkeypatch.setattr(bot, "get_plan", mock)
    return mock


@pytest.fixture
def mock_get_open_pair(monkeypatch):
    mock = AsyncMock(return_value=None)
    monkeypatch.setattr(bot, "get_open_pair_for_sender", mock)
    return mock


@pytest.fixture
def mock_get_active_pair(monkeypatch):
    mock = AsyncMock(return_value=None)
    monkeypatch.setattr(bot, "get_active_pair_for_sender", mock)
    return mock


@pytest.fixture
def mock_find_pair_by_replied(monkeypatch):
    mock = AsyncMock(return_value=None)
    monkeypatch.setattr(bot, "find_pair_by_replied_message", mock)
    return mock


@pytest.fixture
def mock_create_pending_pair(monkeypatch):
    mock = AsyncMock(return_value={
        "id": 99, "sender_id": 1001, "recipient_id": None,
        "pair_code": "pair_NEWCODE", "status": "pending",
        "paired_at": None, "paused_at": None,
        "expires_at": datetime.now(timezone.utc).replace(microsecond=0),
    })
    monkeypatch.setattr(bot, "create_pending_pair", mock)
    return mock


@pytest.fixture
def mock_pause_pair(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(bot, "pause_pair_db", mock)
    return mock


@pytest.fixture
def mock_resume_pair(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(bot, "resume_pair_db", mock)
    return mock


@pytest.fixture
def mock_unpair(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(bot, "unpair_db", mock)
    return mock


@pytest.fixture
def mock_get_user_first_name(monkeypatch):
    mock = AsyncMock(return_value="Maria")
    monkeypatch.setattr(bot, "get_user_first_name", mock)
    return mock


@pytest.fixture
def mock_get_user_prefs(monkeypatch):
    # Default: target language = Spanish
    mock = AsyncMock(return_value=("en", "es"))
    monkeypatch.setattr(bot, "get_user_prefs", mock)
    return mock


# ----- Helpers -----

def make_pair(
    status="active",
    sender_id=1001,
    recipient_id=2002,
    pair_code="pair_test",
    expires_at=None,
    paid_id=1,
):
    """Build a pair dict matching what the DB helpers return."""
    return {
        "id": paid_id,
        "sender_id": sender_id,
        "recipient_id": recipient_id,
        "pair_code": pair_code,
        "status": status,
        "paired_at": datetime.now(timezone.utc) if status in ("active", "paused_user") else None,
        "paused_at": datetime.now(timezone.utc) if status == "paused_user" else None,
        "expires_at": expires_at,
    }
