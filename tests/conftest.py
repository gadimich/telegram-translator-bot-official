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
def mock_status_msg():
    """Mock for the 'Translating...' status message that handle_voice posts and
    later edits/deletes. Returned by msg.reply_text in mock_msg."""
    status = MagicMock()
    status.edit_text = AsyncMock()
    status.delete = AsyncMock()
    return status


@pytest.fixture
def mock_msg(mock_user, mock_status_msg, tmp_path):
    """Voice message from mock_user. reply_text returns mock_status_msg so
    handle_voice's `status = await msg.reply_text(...)` then `status.edit_text(...)`
    pattern works in tests."""
    msg = MagicMock()
    msg.chat_id = 1001
    msg.from_user = mock_user
    msg.audio = None
    msg.reply_to_message = None
    msg.reply_text = AsyncMock(return_value=mock_status_msg)

    # Audio object with download_to_drive that creates a tiny stub file.
    audio = MagicMock()
    audio.duration = 10

    async def fake_download(custom_path):
        # transcribe is mocked anyway; we just need the file to exist if anyone
        # opens it. (Currently nobody does — synthesize writes a *separate*
        # output file — but creating a stub is cheap insurance.)
        from pathlib import Path
        Path(custom_path).write_bytes(b"X")

    tg_file = MagicMock()
    tg_file.download_to_drive = fake_download
    audio.get_file = AsyncMock(return_value=tg_file)
    msg.voice = audio

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


@pytest.fixture(autouse=True)
def disable_allowed_users(monkeypatch):
    """Production .env sets ALLOWED_USERS to specific IDs which would reject
    any test user. Clear the allowlist for tests so handle_voice doesn't
    bail out at the auth gate."""
    monkeypatch.setattr(bot, "ALLOWED_USERS", set())


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


@pytest.fixture
def mock_get_message_count(monkeypatch):
    mock = AsyncMock(return_value=0)
    monkeypatch.setattr(bot, "get_message_count", mock)
    return mock


@pytest.fixture
def mock_increment_usage(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(bot, "increment_usage", mock)
    return mock


@pytest.fixture
def mock_record_forward(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(bot, "record_forward_message", mock)
    return mock


@pytest.fixture
def mock_get_pair_by_code(monkeypatch):
    mock = AsyncMock(return_value=None)
    monkeypatch.setattr(bot, "get_pair_by_code", mock)
    return mock


@pytest.fixture
def mock_accept_pair(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(bot, "accept_pair_in_db", mock)
    return mock


@pytest.fixture
def mock_set_user_target(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(bot, "set_user_target", mock)
    return mock


@pytest.fixture
def mock_update_user_info(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(bot, "update_user_info", mock)
    return mock


# ----- AI pipeline mocks (replace the OpenAI-backed helpers) -----

@pytest.fixture
def mock_transcribe(monkeypatch):
    """Returns ('Hello world', 'en') by default. Override .return_value per test."""
    mock = AsyncMock(return_value=("Hello world", "en"))
    monkeypatch.setattr(bot, "transcribe", mock)
    return mock


@pytest.fixture
def mock_translate(monkeypatch):
    """Returns ('Hola mundo', 10, 5) by default — translation, in_tok, out_tok."""
    mock = AsyncMock(return_value=("Hola mundo", 10, 5))
    monkeypatch.setattr(bot, "translate", mock)
    return mock


@pytest.fixture
def mock_synthesize(monkeypatch):
    """Writes a stub byte to out_path so the open(...) call after it works.
    Accepts the optional voice parameter that production synthesize takes."""
    async def fake_synth(text, out_path, voice="alloy"):
        out_path.write_bytes(b"AUDIO_STUB")
        return out_path

    monkeypatch.setattr(bot, "synthesize", fake_synth)
    return fake_synth


@pytest.fixture
def mock_get_user_voice(monkeypatch):
    mock = AsyncMock(return_value="alloy")
    monkeypatch.setattr(bot, "get_user_voice", mock)
    return mock


@pytest.fixture
def voice_pipeline(
    mock_transcribe, mock_translate, mock_synthesize,
    mock_increment_usage, mock_record_forward,
    mock_get_message_count, mock_get_plan, mock_get_user_prefs,
    mock_get_active_pair, mock_find_pair_by_replied,
    mock_get_user_first_name, mock_update_user_info, mock_get_user_voice,
):
    """Bundle of mocks needed for any handle_voice test. Pulls them all in
    so individual tests can request just `voice_pipeline` instead of
    listing 12 fixtures."""
    return {
        "transcribe": mock_transcribe,
        "translate": mock_translate,
        "synthesize": mock_synthesize,
        "increment_usage": mock_increment_usage,
        "record_forward": mock_record_forward,
        "get_message_count": mock_get_message_count,
        "get_plan": mock_get_plan,
        "get_user_prefs": mock_get_user_prefs,
        "get_active_pair": mock_get_active_pair,
        "find_pair_by_replied": mock_find_pair_by_replied,
        "get_user_first_name": mock_get_user_first_name,
        "update_user_info": mock_update_user_info,
        "get_user_voice": mock_get_user_voice,
    }


# ----- Callback query fixtures -----

@pytest.fixture
def mock_callback_query(mock_user):
    """A Telegram CallbackQuery (inline button click)."""
    query = MagicMock()
    query.from_user = mock_user
    query.data = ""
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    return query


@pytest.fixture
def mock_callback_update(mock_callback_query):
    update = MagicMock()
    update.callback_query = mock_callback_query
    update.effective_user = mock_callback_query.from_user
    update.message = None
    return update


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
