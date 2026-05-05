"""Tests for the Groups feature — _handle_group_voice routing and the
my_chat_member admission flow (Pro check, member-count cap, leave-on-fail)."""
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot


# ---------- Voice routing inside a group ----------

@pytest.fixture
def group_msg(mock_msg):
    """Reuse the mock voice message but mark it as being in a group chat."""
    mock_msg.chat = MagicMock()
    mock_msg.chat.id = -100500
    mock_msg.chat.type = "supergroup"
    mock_msg.chat_id = -100500
    mock_msg.message_id = 42
    return mock_msg


@pytest.fixture
def group_update(mock_user, group_msg):
    update = MagicMock()
    update.effective_user = mock_user
    update.message = group_msg
    return update


@pytest.fixture
def group_pipeline(voice_pipeline, monkeypatch):
    """voice_pipeline + group-specific helper mocks."""
    get_owner = AsyncMock(return_value=2002)  # owner != speaker by default
    monkeypatch.setattr(bot, "get_group_owner", get_owner)
    remove_sub = AsyncMock()
    monkeypatch.setattr(bot, "remove_group_sub", remove_sub)
    voice_pipeline["get_group_owner"] = get_owner
    voice_pipeline["remove_group_sub"] = remove_sub
    voice_pipeline["get_plan"].return_value = "pro"
    return voice_pipeline


@pytest.mark.asyncio
async def test_non_owner_voice_translates_to_owners_source(
    group_update, mock_context, group_pipeline,
):
    """A random group member's voice should be translated into the owner's
    source language (so the owner understands), reply to the original."""
    # Owner speaks Spanish, target English. Speaker (1001) != owner (2002).
    group_pipeline["get_user_prefs"].return_value = ("es", "en")
    group_pipeline["get_group_owner"].return_value = 2002
    group_pipeline["transcribe"].return_value = ("Hello", "en")

    await bot.handle_voice(group_update, mock_context)

    # Translated to owner's source = "es"
    assert group_pipeline["translate"].call_args.args[2] == "es"
    sent = mock_context.bot.send_voice.call_args
    assert sent.kwargs["chat_id"] == -100500
    assert sent.kwargs["reply_to_message_id"] == 42
    # Bill the owner, not the speaker
    assert group_pipeline["increment_usage"].call_args.args[1] == 2002


@pytest.mark.asyncio
async def test_owner_voice_translates_to_target(
    group_update, mock_context, group_pipeline, mock_user,
):
    """When the owner speaks in their own group, translate to their target
    language so the others can understand."""
    mock_user.id = 2002  # speaker IS the owner
    group_pipeline["get_user_prefs"].return_value = ("es", "en")
    group_pipeline["get_group_owner"].return_value = 2002
    group_pipeline["transcribe"].return_value = ("Hola", "es")

    await bot.handle_voice(group_update, mock_context)

    assert group_pipeline["translate"].call_args.args[2] == "en"


@pytest.mark.asyncio
async def test_no_sub_no_op(group_update, mock_context, group_pipeline):
    """If the bot is in a group with no active sub, ignore voice silently."""
    group_pipeline["get_group_owner"].return_value = None

    await bot.handle_voice(group_update, mock_context)

    group_pipeline["transcribe"].assert_not_called()
    mock_context.bot.send_voice.assert_not_called()


@pytest.mark.asyncio
async def test_owner_lost_pro_leaves_group(
    group_update, mock_context, group_pipeline,
):
    """Owner downgraded → drop the sub and leave the chat. No translation."""
    group_pipeline["get_group_owner"].return_value = 2002
    group_pipeline["get_plan"].return_value = "free"
    mock_context.bot.leave_chat = AsyncMock()

    await bot.handle_voice(group_update, mock_context)

    group_pipeline["remove_group_sub"].assert_called_once()
    mock_context.bot.leave_chat.assert_called_once_with(-100500)
    group_pipeline["transcribe"].assert_not_called()


@pytest.mark.asyncio
async def test_skip_when_already_in_target_lang(
    group_update, mock_context, group_pipeline, mock_user,
):
    """If detected source already equals the target, skip TTS — nothing to do."""
    mock_user.id = 2002  # owner speaking
    group_pipeline["get_user_prefs"].return_value = ("es", "en")
    group_pipeline["get_group_owner"].return_value = 2002
    # Owner's voice is already in their target ("en") — nothing to translate
    group_pipeline["transcribe"].return_value = ("Hello", "en")

    await bot.handle_voice(group_update, mock_context)

    group_pipeline["translate"].assert_not_called()
    mock_context.bot.send_voice.assert_not_called()


# ---------- my_chat_member admission flow ----------

def _make_my_chat_member_update(
    *, chat_type="supergroup", new_status="member", old_status="left",
    chat_id=-100500, adder_id=1001, chat_title="Test Group",
):
    update = MagicMock()
    cmu = MagicMock()
    chat = MagicMock()
    chat.id = chat_id
    chat.type = chat_type
    chat.title = chat_title
    cmu.chat = chat
    cmu.new_chat_member = MagicMock(status=new_status)
    cmu.old_chat_member = MagicMock(status=old_status)
    adder = MagicMock()
    adder.id = adder_id
    adder.first_name = "Adder"
    adder.username = "adder"
    cmu.from_user = adder
    update.my_chat_member = cmu
    return update


@pytest.fixture
def admission_context(mock_context, monkeypatch):
    mock_context.bot.get_chat_member_count = AsyncMock(return_value=5)
    mock_context.bot.leave_chat = AsyncMock()
    mock_context.bot.send_message = AsyncMock()
    monkeypatch.setattr(bot, "_notify_admin", AsyncMock())
    return mock_context


@pytest.mark.asyncio
async def test_added_by_pro_user_activates_group(
    admission_context, monkeypatch,
):
    monkeypatch.setattr(bot, "get_plan", AsyncMock(return_value="pro"))
    monkeypatch.setattr(bot, "get_user_prefs", AsyncMock(return_value=("es", "en")))
    add_sub = AsyncMock()
    monkeypatch.setattr(bot, "add_group_sub", add_sub)

    update = _make_my_chat_member_update()
    await bot.my_chat_member_handler(update, admission_context)

    add_sub.assert_called_once()
    assert add_sub.call_args.args[1] == -100500  # chat_id
    assert add_sub.call_args.args[2] == 1001     # owner_user_id
    admission_context.bot.leave_chat.assert_not_called()


@pytest.mark.asyncio
async def test_added_by_non_pro_leaves(admission_context, monkeypatch):
    monkeypatch.setattr(bot, "get_plan", AsyncMock(return_value="basic"))
    add_sub = AsyncMock()
    monkeypatch.setattr(bot, "add_group_sub", add_sub)

    update = _make_my_chat_member_update()
    await bot.my_chat_member_handler(update, admission_context)

    add_sub.assert_not_called()
    admission_context.bot.leave_chat.assert_called_once_with(-100500)


@pytest.mark.asyncio
async def test_too_many_members_leaves(admission_context, monkeypatch):
    monkeypatch.setattr(bot, "get_plan", AsyncMock(return_value="pro"))
    monkeypatch.setattr(bot, "get_user_prefs", AsyncMock(return_value=("es", "en")))
    add_sub = AsyncMock()
    monkeypatch.setattr(bot, "add_group_sub", add_sub)
    admission_context.bot.get_chat_member_count.return_value = 25

    update = _make_my_chat_member_update()
    await bot.my_chat_member_handler(update, admission_context)

    add_sub.assert_not_called()
    admission_context.bot.leave_chat.assert_called_once()


@pytest.mark.asyncio
async def test_owner_without_setup_leaves(admission_context, monkeypatch):
    monkeypatch.setattr(bot, "get_plan", AsyncMock(return_value="pro"))
    monkeypatch.setattr(bot, "get_user_prefs", AsyncMock(return_value=(None, None)))
    add_sub = AsyncMock()
    monkeypatch.setattr(bot, "add_group_sub", add_sub)

    update = _make_my_chat_member_update()
    await bot.my_chat_member_handler(update, admission_context)

    add_sub.assert_not_called()
    admission_context.bot.leave_chat.assert_called_once()


@pytest.mark.asyncio
async def test_bot_kicked_drops_sub(admission_context, monkeypatch):
    remove_sub = AsyncMock()
    monkeypatch.setattr(bot, "remove_group_sub", remove_sub)

    update = _make_my_chat_member_update(
        new_status="kicked", old_status="member"
    )
    await bot.my_chat_member_handler(update, admission_context)

    remove_sub.assert_called_once_with(admission_context.bot_data["pool"], -100500)


@pytest.mark.asyncio
async def test_private_chat_my_chat_member_ignored(admission_context, monkeypatch):
    """Private chat (not a group) — handler is a no-op, doesn't touch the DB."""
    add_sub = AsyncMock()
    remove_sub = AsyncMock()
    monkeypatch.setattr(bot, "add_group_sub", add_sub)
    monkeypatch.setattr(bot, "remove_group_sub", remove_sub)

    update = _make_my_chat_member_update(chat_type="private")
    await bot.my_chat_member_handler(update, admission_context)

    add_sub.assert_not_called()
    remove_sub.assert_not_called()
