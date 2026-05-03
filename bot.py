"""
Telegram Voice Translator Bot
------------------------------
Each user sets their target language (what they want translations in).
Source language is auto-detected by Whisper, and optionally inferred from
the user's Telegram language setting for UI localisation.

Pipeline: Telegram voice -> Whisper -> GPT-4o-mini -> TTS -> voice reply
"""

from __future__ import annotations

import os
import logging
import tempfile
from pathlib import Path

import asyncpg
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)
from openai import AsyncOpenAI
from dotenv import load_dotenv

from strings import get_strings, UI_STRINGS

# ---------- Setup ----------
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("voice-translator")

BOT_TOKEN = os.environ["BOT_TOKEN"]
openai_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])

FORWARD_TO = os.getenv("FORWARD_TO", "").strip()
ALLOWED_USERS = {
    int(x) for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip()
}
TTS_VOICE = os.getenv("TTS_VOICE", "alloy")

# ---------- Language config ----------
SUPPORTED: dict[str, dict] = {
    "en": {"name": "English",    "flag": "🇬🇧"},
    "es": {"name": "Spanish",    "flag": "🇪🇸"},
    "fr": {"name": "French",     "flag": "🇫🇷"},
    "de": {"name": "German",     "flag": "🇩🇪"},
    "pt": {"name": "Portuguese", "flag": "🇵🇹"},
    "it": {"name": "Italian",    "flag": "🇮🇹"},
    "ru": {"name": "Russian",    "flag": "🇷🇺"},
    "zh": {"name": "Chinese",    "flag": "🇨🇳"},
    "ja": {"name": "Japanese",   "flag": "🇯🇵"},
    "ko": {"name": "Korean",     "flag": "🇰🇷"},
    "ar": {"name": "Arabic",     "flag": "🇸🇦"},
    "hi": {"name": "Hindi",      "flag": "🇮🇳"},
    "tr": {"name": "Turkish",    "flag": "🇹🇷"},
    "pl": {"name": "Polish",     "flag": "🇵🇱"},
    "nl": {"name": "Dutch",      "flag": "🇳🇱"},
    "uk": {"name": "Ukrainian",  "flag": "🇺🇦"},
    "he": {"name": "Hebrew",     "flag": "🇮🇱"},
    "fa": {"name": "Persian",    "flag": "🇮🇷"},
    "id": {"name": "Indonesian", "flag": "🇮🇩"},
    "vi": {"name": "Vietnamese", "flag": "🇻🇳"},
}

WHISPER_TO_CODE: dict[str, str] = {
    "english": "en",    "spanish": "es",    "french": "fr",
    "german": "de",     "portuguese": "pt", "italian": "it",
    "russian": "ru",    "chinese": "zh",    "japanese": "ja",
    "korean": "ko",     "arabic": "ar",     "hindi": "hi",
    "turkish": "tr",    "polish": "pl",     "dutch": "nl",
    "ukrainian": "uk",  "hebrew": "he",     "persian": "fa",
    "indonesian": "id", "vietnamese": "vi",
    **{code: code for code in SUPPORTED},
}


def lang_label(code: str) -> str:
    info = SUPPORTED.get(code, {})
    return f"{info.get('flag', '🌐')} {info.get('name', code)}"


# ---------- Keyboards ----------
def lang_keyboard(prefix: str, exclude: str | None = None) -> InlineKeyboardMarkup:
    codes = [c for c in SUPPORTED if c != exclude]
    buttons = [
        InlineKeyboardButton(lang_label(c), callback_data=f"{prefix}{c}")
        for c in codes
    ]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    return InlineKeyboardMarkup(rows)


def target_keyboard(s: dict, exclude: str | None = None, source_btn: str | None = None) -> InlineKeyboardMarkup:
    grid = list(lang_keyboard("tgt_", exclude=exclude).inline_keyboard)
    if source_btn:
        grid += [[InlineKeyboardButton(source_btn, callback_data="change_src")]]
    return InlineKeyboardMarkup(grid)


def settings_keyboard(s: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(s["change_source"], callback_data="change_src"),
        InlineKeyboardButton(s["change_target"], callback_data="change_tgt"),
    ]])


# ---------- Plans ----------
FREE_LIMIT  = int(os.getenv("FREE_LIMIT",  "10"))
BASIC_LIMIT = int(os.getenv("BASIC_LIMIT", "100"))
PLAN_LIMITS: dict[str, int | None] = {"free": FREE_LIMIT, "basic": BASIC_LIMIT, "pro": None}
PLAN_STARS:  dict[str, int]        = {"basic": int(os.getenv("BASIC_STARS", "460")), "pro": int(os.getenv("PRO_STARS", "1538"))}


def upgrade_keyboard(s: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(s["upgrade_basic_btn"].format(limit=BASIC_LIMIT), callback_data="buy_basic")],
        [InlineKeyboardButton(s["upgrade_pro_btn"],   callback_data="buy_pro")],
    ])


# ---------- DB helpers ----------
async def init_db(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_prefs (
                user_id     BIGINT PRIMARY KEY,
                source_lang TEXT,
                target_lang TEXT,
                updated_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("ALTER TABLE user_prefs ALTER COLUMN source_lang DROP NOT NULL")
        await conn.execute("ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS target_lang TEXT")
        await conn.execute("ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS plan TEXT DEFAULT 'free'")
        await conn.execute("ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS plan_until TIMESTAMPTZ")
        await conn.execute("ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS first_name TEXT")
        await conn.execute("ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS username TEXT")
        await conn.execute(
            "ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS voice TEXT DEFAULT 'alloy'"
        )
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS usage (
                user_id  BIGINT,
                period   TEXT,
                count    INT DEFAULT 0,
                cost_usd REAL DEFAULT 0,
                PRIMARY KEY (user_id, period)
            )
        """)
        await conn.execute("ALTER TABLE usage ADD COLUMN IF NOT EXISTS cost_usd REAL DEFAULT 0")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id       SERIAL PRIMARY KEY,
                user_id  BIGINT,
                plan     TEXT,
                stars    INT,
                paid_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ui_strings_cache (
                lang        TEXT PRIMARY KEY,
                hash        TEXT NOT NULL,
                strings     TEXT NOT NULL,
                updated_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # Forward feature: pairs of (sender, recipient) where sender's voice
        # messages auto-forward to recipient. recipient_id is NULL until the
        # invite is accepted via /start pair_<code>.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS forward_pairs (
                id              SERIAL PRIMARY KEY,
                sender_id       BIGINT NOT NULL,
                recipient_id    BIGINT,
                pair_code       TEXT NOT NULL UNIQUE,
                status          TEXT NOT NULL,
                paired_at       TIMESTAMPTZ,
                paused_at       TIMESTAMPTZ,
                expires_at      TIMESTAMPTZ
            )
        """)
        # Only one open pair per sender at a time (allows re-pairing after unpair).
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_pair_per_sender
                ON forward_pairs (sender_id) WHERE status != 'unpaired'
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_forward_pairs_recipient
                ON forward_pairs (recipient_id, status)
        """)
        # Maps a delivered message in a chat to the pair it came from, so a
        # Telegram-native Reply on a forwarded message routes voice back to
        # the right pair.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS forward_messages (
                delivered_chat_id    BIGINT NOT NULL,
                delivered_message_id BIGINT NOT NULL,
                pair_id              INT NOT NULL REFERENCES forward_pairs(id) ON DELETE CASCADE,
                created_at           TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (delivered_chat_id, delivered_message_id)
            )
        """)


async def get_user_prefs(pool: asyncpg.Pool, user_id: int) -> tuple[str | None, str | None]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT source_lang, target_lang FROM user_prefs WHERE user_id = $1", user_id
        )
    return (row["source_lang"], row["target_lang"]) if row else (None, None)


async def set_user_source(pool: asyncpg.Pool, user_id: int, lang: str | None) -> None:
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO user_prefs (user_id, source_lang, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (user_id) DO UPDATE SET source_lang = $2, updated_at = NOW()
        """, user_id, lang)


async def set_user_target(pool: asyncpg.Pool, user_id: int, lang: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO user_prefs (user_id, target_lang, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (user_id) DO UPDATE SET target_lang = $2, updated_at = NOW()
        """, user_id, lang)


SUPPORTED_VOICES = {"alloy", "nova"}


async def get_user_voice(pool: asyncpg.Pool, user_id: int) -> str:
    """Return the user's chosen TTS voice. Falls back to the global default
    (TTS_VOICE env var, default 'alloy') for users who haven't picked yet."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT voice FROM user_prefs WHERE user_id = $1", user_id
        )
    voice = row["voice"] if row and row["voice"] else None
    if voice in SUPPORTED_VOICES:
        return voice
    return TTS_VOICE if TTS_VOICE in SUPPORTED_VOICES else "alloy"


async def set_user_voice(pool: asyncpg.Pool, user_id: int, voice: str) -> None:
    if voice not in SUPPORTED_VOICES:
        raise ValueError(f"Unsupported voice: {voice}")
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO user_prefs (user_id, voice, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (user_id) DO UPDATE SET voice = $2, updated_at = NOW()
        """, user_id, voice)


async def get_plan(pool: asyncpg.Pool, user_id: int) -> str:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT plan, plan_until FROM user_prefs WHERE user_id = $1", user_id
        )
    if not row:
        return "free"
    plan = row["plan"] or "free"
    if plan != "free" and (row["plan_until"] is None or row["plan_until"].timestamp() < __import__("time").time()):
        return "free"
    return plan


async def set_plan(pool: asyncpg.Pool, user_id: int, plan: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO user_prefs (user_id, plan, plan_until, updated_at)
            VALUES ($1, $2, NOW() + INTERVAL '1 month', NOW())
            ON CONFLICT (user_id) DO UPDATE
                SET plan = $2, plan_until = NOW() + INTERVAL '1 month', updated_at = NOW()
        """, user_id, plan)


async def get_message_count(pool: asyncpg.Pool, user_id: int) -> int:
    from datetime import datetime
    period = datetime.utcnow().strftime("%Y-%m")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT count FROM usage WHERE user_id = $1 AND period = $2", user_id, period
        )
    return row["count"] if row else 0


async def increment_usage(pool: asyncpg.Pool, user_id: int, cost_usd: float = 0.0) -> None:
    from datetime import datetime
    period = datetime.utcnow().strftime("%Y-%m")
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO usage (user_id, period, count, cost_usd) VALUES ($1, $2, 1, $3)
            ON CONFLICT (user_id, period) DO UPDATE
                SET count = usage.count + 1, cost_usd = usage.cost_usd + $3
        """, user_id, period, cost_usd)


async def update_user_info(pool: asyncpg.Pool, user_id: int, first_name: str | None, username: str | None) -> None:
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO user_prefs (user_id, first_name, username, updated_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (user_id) DO UPDATE SET first_name = $2, username = $3
        """, user_id, first_name, username)


async def record_payment(pool: asyncpg.Pool, user_id: int, plan: str, stars: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO payments (user_id, plan, stars) VALUES ($1, $2, $3)",
            user_id, plan, stars,
        )


# ---------- Forward pair helpers ----------
import secrets


async def create_pending_pair(pool: asyncpg.Pool, sender_id: int) -> dict:
    """Create a new pending pair with a fresh code. Returns the row as a dict."""
    while True:
        code = "pair_" + secrets.token_urlsafe(9)
        try:
            row = await pool.fetchrow(
                """INSERT INTO forward_pairs (sender_id, pair_code, status, expires_at)
                   VALUES ($1, $2, 'pending', NOW() + INTERVAL '72 hours')
                   RETURNING *""",
                sender_id, code,
            )
            return dict(row)
        except asyncpg.UniqueViolationError as e:
            # Could be the pair_code collision (retry) or the
            # one-open-pair-per-sender index (caller should have checked first).
            if "pair_code" in str(e):
                continue
            raise


async def get_open_pair_for_sender(pool: asyncpg.Pool, sender_id: int) -> dict | None:
    """Get the user's currently open outgoing pair (any status except 'unpaired')."""
    row = await pool.fetchrow(
        "SELECT * FROM forward_pairs WHERE sender_id = $1 AND status != 'unpaired' LIMIT 1",
        sender_id,
    )
    return dict(row) if row else None


async def get_active_pair_for_sender(pool: asyncpg.Pool, sender_id: int) -> dict | None:
    """Get the sender's pair that's actively forwarding right now."""
    row = await pool.fetchrow(
        "SELECT * FROM forward_pairs WHERE sender_id = $1 AND status = 'active' LIMIT 1",
        sender_id,
    )
    return dict(row) if row else None


async def get_pair_by_code(pool: asyncpg.Pool, code: str) -> dict | None:
    row = await pool.fetchrow("SELECT * FROM forward_pairs WHERE pair_code = $1", code)
    return dict(row) if row else None


async def find_pair_by_replied_message(
    pool: asyncpg.Pool, chat_id: int, message_id: int
) -> dict | None:
    """For reply-routing: a Telegram Reply on a forwarded message reveals which
    pair it came from. Returns the active pair, or None."""
    row = await pool.fetchrow(
        """SELECT fp.* FROM forward_messages fm
           JOIN forward_pairs fp ON fp.id = fm.pair_id
           WHERE fm.delivered_chat_id = $1 AND fm.delivered_message_id = $2
             AND fp.status = 'active'""",
        chat_id, message_id,
    )
    return dict(row) if row else None


async def accept_pair_in_db(pool: asyncpg.Pool, pair_id: int, recipient_id: int) -> None:
    await pool.execute(
        """UPDATE forward_pairs
           SET recipient_id = $2, status = 'active', paired_at = NOW(), expires_at = NULL
           WHERE id = $1""",
        pair_id, recipient_id,
    )


async def pause_pair_db(pool: asyncpg.Pool, pair_id: int, status: str = "paused_user") -> None:
    await pool.execute(
        "UPDATE forward_pairs SET status = $2, paused_at = NOW() WHERE id = $1",
        pair_id, status,
    )


async def resume_pair_db(pool: asyncpg.Pool, pair_id: int) -> None:
    await pool.execute(
        "UPDATE forward_pairs SET status = 'active', paused_at = NULL WHERE id = $1",
        pair_id,
    )


async def unpair_db(pool: asyncpg.Pool, pair_id: int) -> None:
    await pool.execute(
        "UPDATE forward_pairs SET status = 'unpaired' WHERE id = $1",
        pair_id,
    )


async def record_forward_message(
    pool: asyncpg.Pool, pair_id: int, chat_id: int, message_id: int
) -> None:
    await pool.execute(
        """INSERT INTO forward_messages (delivered_chat_id, delivered_message_id, pair_id)
           VALUES ($1, $2, $3)
           ON CONFLICT DO NOTHING""",
        chat_id, message_id, pair_id,
    )


async def get_user_first_name(pool: asyncpg.Pool, user_id: int) -> str | None:
    return await pool.fetchval(
        "SELECT first_name FROM user_prefs WHERE user_id = $1", user_id
    )


def _tutorial_url_for(lang: str | None) -> str:
    """Return the localized URL of the auto-forward tutorial.

    Falls back to English if the user's source language isn't one of the
    languages the tutorial is translated into."""
    base = "https://tryrespeak.com"
    path = "/blog/auto-forward-voice-messages-telegram"
    if lang in ("es", "fr", "de", "pt"):
        return f"{base}/{lang}{path}"
    return f"{base}{path}"


async def is_new_user(pool: asyncpg.Pool, user_id: int) -> bool:
    """True if no row exists in user_prefs for this user. Must be called BEFORE
    update_user_info — that helper UPSERTs and would mask the brand-new state."""
    row = await pool.fetchrow("SELECT 1 FROM user_prefs WHERE user_id = $1", user_id)
    return row is None


async def _notify_admin(context, text: str) -> None:
    """Best-effort DM to the bot owner. Logs but never raises — admin
    notifications are a nice-to-have, not load-bearing."""
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=text)
    except Exception as e:
        log.warning("Admin notification failed: %s", e)


# ---------- UI string helpers ----------
def _tg_lang(user) -> str | None:
    code = (user.language_code or "").split("-")[0].lower()
    return code if code in SUPPORTED else None


async def _strings(pool: asyncpg.Pool, lang: str) -> dict[str, str]:
    lang_name = SUPPORTED.get(lang, SUPPORTED["en"])["name"]
    return await get_strings(lang, lang_name, openai_client, pool)


async def _strings_for(pool: asyncpg.Pool, user_id: int) -> dict[str, str]:
    src, _ = await get_user_prefs(pool, user_id)
    return await _strings(pool, src or "en")


async def _plan_status(pool: asyncpg.Pool, user_id: int, s: dict) -> str:
    from datetime import datetime
    plan = await get_plan(pool, user_id)
    if plan == "pro":
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT plan_until FROM user_prefs WHERE user_id = $1", user_id)
        date = row["plan_until"].strftime("%b %-d") if row and row["plan_until"] else "?"
        return s["plan_status_pro"].format(date=date)
    elif plan == "basic":
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT plan_until FROM user_prefs WHERE user_id = $1", user_id)
        date = row["plan_until"].strftime("%b %-d") if row and row["plan_until"] else "?"
        count = await get_message_count(pool, user_id)
        return s["plan_status_basic"].format(count=count, limit=BASIC_LIMIT, date=date)
    else:
        count = await get_message_count(pool, user_id)
        return s["plan_status_free"].format(count=count, limit=FREE_LIMIT)


# ---------- Pipeline ----------
async def transcribe(audio_path: Path, hint: str | None = None) -> tuple[str, str]:
    if hint and hint in SUPPORTED:
        log.info("Transcribing %s (lang=%s, user-set)...", audio_path.name, hint)
        with open(audio_path, "rb") as f:
            resp = await openai_client.audio.transcriptions.create(
                model="whisper-1", file=f, language=hint,
            )
        return resp.text.strip(), hint

    log.info("Transcribing %s (auto-detect)...", audio_path.name)
    with open(audio_path, "rb") as f:
        resp = await openai_client.audio.transcriptions.create(
            model="whisper-1", file=f, response_format="verbose_json",
        )
    text = resp.text.strip()
    detected = (resp.language or "").lower()
    lang = WHISPER_TO_CODE.get(detected, "en")
    log.info("Whisper detected %r -> %s | %s", detected, lang, text)
    return text, lang


async def translate(text: str, src: str, tgt: str) -> tuple[str, int, int]:
    src_name = SUPPORTED.get(src, {}).get("name", src)
    tgt_name = SUPPORTED.get(tgt, {}).get("name", tgt)
    log.info("Translating %s -> %s ...", src_name, tgt_name)
    resp = await openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    f"You are a professional translator. Translate the user's "
                    f"{src_name} text to natural, conversational {tgt_name}. "
                    "Preserve tone and register. "
                    "Output ONLY the translation — no quotes, no commentary."
                ),
            },
            {"role": "user", "content": text},
        ],
        temperature=0.3,
    )
    result = resp.choices[0].message.content.strip()
    log.info("Translation: %s", result)
    in_tok  = resp.usage.prompt_tokens     if resp.usage else 0
    out_tok = resp.usage.completion_tokens if resp.usage else 0
    return result, in_tok, out_tok


async def synthesize(text: str, out_path: Path, voice: str = TTS_VOICE) -> Path:
    log.info("Synthesizing speech with voice=%s...", voice)
    resp = await openai_client.audio.speech.create(
        model="tts-1", voice=voice, input=text, response_format="opus",
    )
    out_path.write_bytes(resp.content)
    log.info("Wrote %s (%d bytes)", out_path.name, out_path.stat().st_size)
    return out_path


# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    pool: asyncpg.Pool = context.bot_data["pool"]

    # Detect first-ever interaction BEFORE update_user_info upserts the row.
    new_user = await is_new_user(pool, user.id)

    await update_user_info(pool, user.id, user.first_name, user.username)

    if new_user:
        username_str = f" (@{user.username})" if user.username else ""
        await _notify_admin(
            context,
            f"🆕 New user: {user.first_name or 'Unknown'}{username_str}\n"
            f"ID: {user.id}\n"
            f"Telegram lang: {user.language_code or 'unknown'}",
        )

    # /start pair_<code> — accepting a forwarding invite
    if context.args and context.args[0].startswith("pair_"):
        handled = await _handle_pair_invite(update, context, context.args[0])
        if handled:
            return
        # Code wasn't valid — fall through to the normal welcome flow

    src, tgt = await get_user_prefs(pool, user.id)

    detected = _tg_lang(user)
    ui_lang = src or detected or "en"
    s = await _strings(pool, ui_lang)

    if tgt:
        src_label = lang_label(src) if src and src in SUPPORTED else "🔄 Auto-detect"
        plan_status = await _plan_status(pool, user.id, s)
        await update.message.reply_text(
            s["welcome_back"].format(name=user.first_name, src=src_label, tgt=lang_label(tgt))
            + f"\n\n{plan_status}",
            reply_markup=settings_keyboard(s),
        )
        return

    # Save detected source lang if not already set
    if detected and not src:
        await set_user_source(pool, user.id, detected)
        ui_lang = detected

    if detected:
        prompt = s["detected_lang_prompt"].format(lang=lang_label(ui_lang))
        src_btn = s["dont_speak"].format(lang=SUPPORTED[ui_lang]["name"])
        kb = target_keyboard(s, exclude=ui_lang, source_btn=src_btn)
    else:
        prompt = s["unknown_lang_prompt"]
        kb = target_keyboard(s, source_btn=s["set_source"])

    await update.message.reply_text(
        f"{s['welcome_new'].format(name=user.first_name)}\n\n{prompt}",
        reply_markup=kb,
    )


async def _handle_pair_invite(
    update: Update, context: ContextTypes.DEFAULT_TYPE, code: str
) -> bool:
    """Handle a /start pair_<code> invite. Returns True if the code was a valid
    pair code (and we showed an invite UI), False if the code was unrecognized
    or expired (caller should fall back to normal welcome)."""
    user = update.effective_user
    pool: asyncpg.Pool = context.bot_data["pool"]

    pair = await get_pair_by_code(pool, code)
    if not pair:
        return False

    # Quick fail-cases
    s = await _strings_for(pool, user.id)
    sender_name = await get_user_first_name(pool, pair["sender_id"]) or "Someone"

    if pair["status"] == "active" and pair["recipient_id"] == user.id:
        await update.message.reply_text(s["pair_invite_already_active"])
        return True

    if pair["status"] != "pending":
        await update.message.reply_text(
            s["pair_invite_expired"].format(sender_name=sender_name)
        )
        return True

    # Expiry check
    from datetime import datetime, timezone
    if pair["expires_at"] and pair["expires_at"] < datetime.now(timezone.utc):
        await pool.execute(
            "UPDATE forward_pairs SET status = 'unpaired' WHERE id = $1",
            pair["id"],
        )
        await update.message.reply_text(
            s["pair_invite_expired"].format(sender_name=sender_name)
        )
        return True

    # If the recipient hasn't picked their /setlang yet, ask them first
    _, recipient_tgt = await get_user_prefs(pool, user.id)
    if not recipient_tgt:
        # Use Telegram-detected lang for UI
        ui_s = await _strings(pool, _tg_lang(user) or "en")
        await update.message.reply_text(
            ui_s["pair_invite_lang_prompt"].format(sender_name=sender_name),
            reply_markup=lang_keyboard(f"plang_{code}_"),
        )
        return True

    # Show Accept / Decline
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(s["pair_accept_btn"], callback_data=f"paccept_{code}"),
        InlineKeyboardButton(s["pair_decline_btn"], callback_data=f"pdecline_{code}"),
    ]])
    await update.message.reply_text(
        s["pair_invite_received"].format(
            sender_name=sender_name, lang=lang_label(recipient_tgt)
        ),
        reply_markup=keyboard,
    )
    return True


# ---------- Forward feature commands ----------

async def forward_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    pool: asyncpg.Pool = context.bot_data["pool"]
    s = await _strings_for(pool, user.id)

    args = context.args or []
    sub = args[0] if args else "status"
    if sub not in ("setup", "pause", "resume", "status"):
        sub = "status"

    # Plan gate: free tier doesn't get forwarding
    plan = await get_plan(pool, user.id)
    if plan == "free":
        await update.message.reply_text(
            s["forward_paid_only"], reply_markup=upgrade_keyboard(s)
        )
        return

    pair = await get_open_pair_for_sender(pool, user.id)

    # Auto-clean up an expired pending pair so the user can create a new one.
    if pair and pair["status"] == "pending" and pair["expires_at"]:
        from datetime import datetime, timezone
        if pair["expires_at"] < datetime.now(timezone.utc):
            await unpair_db(pool, pair["id"])
            pair = None

    if sub == "setup":
        if pair and pair["status"] == "pending":
            link = f"https://t.me/TryRespeakBot?start={pair['pair_code']}"
            await update.message.reply_text(
                s["forward_pending"].format(link=link), disable_web_page_preview=True
            )
            return
        if pair and pair["status"] == "active":
            recipient_name = (
                await get_user_first_name(pool, pair["recipient_id"]) or "user"
            )
            _, rec_lang = await get_user_prefs(pool, pair["recipient_id"])
            await update.message.reply_text(
                s["forward_active_status"].format(
                    name=recipient_name,
                    lang=lang_label(rec_lang) if rec_lang else "?",
                )
            )
            return
        if pair and pair["status"] == "paused_user":
            await update.message.reply_text(s["forward_paused_status"])
            return
        new_pair = await create_pending_pair(pool, user.id)
        link = f"https://t.me/TryRespeakBot?start={new_pair['pair_code']}"
        # Tutorial URL adapts to the user's source language so the guide
        # opens in a language they can read.
        src, _ = await get_user_prefs(pool, user.id)
        tutorial_url = _tutorial_url_for(src)
        await update.message.reply_text(
            s["forward_setup_link"].format(link=link, tutorial_url=tutorial_url),
            disable_web_page_preview=True,
        )
        return

    if sub == "pause":
        if not pair:
            await update.message.reply_text(s["forward_no_pair"])
            return
        if pair["status"] == "pending":
            await update.message.reply_text(s["forward_pause_pending"])
            return
        if pair["status"] == "paused_user":
            await update.message.reply_text(s["forward_paused_status"])
            return
        if pair["status"] != "active":
            await update.message.reply_text(s["forward_no_pair"])
            return
        await pause_pair_db(pool, pair["id"], "paused_user")
        await update.message.reply_text(s["forward_pause_done"])
        return

    if sub == "resume":
        if not pair or pair["status"] != "paused_user":
            await update.message.reply_text(s["forward_resume_no_pair"])
            return
        await resume_pair_db(pool, pair["id"])
        recipient_name = (
            await get_user_first_name(pool, pair["recipient_id"]) or "user"
        )
        await update.message.reply_text(
            s["forward_resume_done"].format(name=recipient_name)
        )
        return

    # status
    if not pair:
        await update.message.reply_text(s["forward_no_pair"])
        return
    if pair["status"] == "pending":
        link = f"https://t.me/TryRespeakBot?start={pair['pair_code']}"
        await update.message.reply_text(
            s["forward_pending"].format(link=link), disable_web_page_preview=True
        )
        return
    if pair["status"] == "active":
        recipient_name = (
            await get_user_first_name(pool, pair["recipient_id"]) or "user"
        )
        _, rec_lang = await get_user_prefs(pool, pair["recipient_id"])
        await update.message.reply_text(
            s["forward_active_status"].format(
                name=recipient_name,
                lang=lang_label(rec_lang) if rec_lang else "?",
            )
        )
        return
    # paused
    await update.message.reply_text(s["forward_paused_status"])


async def unforward_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    pool: asyncpg.Pool = context.bot_data["pool"]
    s = await _strings_for(pool, user.id)

    pair = await get_open_pair_for_sender(pool, user.id)
    if not pair or pair["status"] == "pending":
        # No active pair (we don't surface "you can cancel your pending invite"
        # — they can just create a new one which replaces the old)
        if pair and pair["status"] == "pending":
            await unpair_db(pool, pair["id"])
            await update.message.reply_text(s["unforward_done"].format(name="(pending invite)"))
            return
        await update.message.reply_text(s["unforward_no_pair"])
        return

    recipient_id = pair["recipient_id"]
    recipient_name = await get_user_first_name(pool, recipient_id) if recipient_id else None

    await unpair_db(pool, pair["id"])

    # Notify the recipient (best-effort)
    if recipient_id:
        try:
            sender_name = await get_user_first_name(pool, user.id) or "Your sender"
            r_strings = await _strings_for(pool, recipient_id)
            await context.bot.send_message(
                chat_id=recipient_id,
                text=r_strings["unforward_recipient_notice"].format(sender_name=sender_name),
            )
        except Exception as e:
            log.warning("Failed to notify recipient %s of unpair: %s", recipient_id, e)

    await update.message.reply_text(
        s["unforward_done"].format(name=recipient_name or "user")
    )


# ---------- Forward feature callbacks ----------

async def pair_lang_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User picks their receive-language during pair invite acceptance."""
    query = update.callback_query
    await query.answer()
    # Format: plang_<code>_<lang>  (code itself is "pair_<...>" so contains _)
    # Strip the "plang_" prefix, then split off the trailing "_<lang>"
    payload = query.data[len("plang_"):]
    try:
        code, lang = payload.rsplit("_", 1)
    except ValueError:
        log.warning("Malformed plang callback: %s", query.data)
        return
    user = query.from_user
    pool: asyncpg.Pool = context.bot_data["pool"]

    if lang not in SUPPORTED:
        return
    await set_user_target(pool, user.id, lang)

    pair = await get_pair_by_code(pool, code)
    if not pair or pair["status"] != "pending":
        s = await _strings_for(pool, user.id)
        await query.edit_message_text(
            s["pair_invite_expired"].format(sender_name="the sender")
        )
        return

    sender_name = await get_user_first_name(pool, pair["sender_id"]) or "Someone"
    s = await _strings_for(pool, user.id)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(s["pair_accept_btn"], callback_data=f"paccept_{code}"),
        InlineKeyboardButton(s["pair_decline_btn"], callback_data=f"pdecline_{code}"),
    ]])
    await query.edit_message_text(
        s["pair_invite_received"].format(sender_name=sender_name, lang=lang_label(lang)),
        reply_markup=keyboard,
    )


async def pair_accept_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    code = query.data[len("paccept_"):]
    user = query.from_user
    pool: asyncpg.Pool = context.bot_data["pool"]

    pair = await get_pair_by_code(pool, code)
    if not pair:
        return
    s_recipient = await _strings_for(pool, user.id)
    if pair["status"] != "pending":
        await query.edit_message_text(
            s_recipient["pair_invite_expired"].format(sender_name="the sender")
        )
        return

    sender_id = pair["sender_id"]
    await accept_pair_in_db(pool, pair["id"], user.id)

    # Look up languages for the confirmation messages
    _, recipient_lang = await get_user_prefs(pool, user.id)
    _, sender_lang = await get_user_prefs(pool, sender_id)
    sender_name = await get_user_first_name(pool, sender_id) or "your sender"

    # Confirm to recipient (replace the invite message in their chat)
    await query.edit_message_text(
        s_recipient["pair_accepted_to_recipient"].format(
            sender_name=sender_name,
            lang=lang_label(recipient_lang) if recipient_lang else "?",
            sender_lang=lang_label(sender_lang) if sender_lang else "?",
        )
    )

    # Notify the sender
    try:
        s_sender = await _strings_for(pool, sender_id)
        recipient_name = user.first_name or "the recipient"
        await context.bot.send_message(
            chat_id=sender_id,
            text=s_sender["pair_accepted_to_sender"].format(
                recipient_name=recipient_name,
                lang=lang_label(recipient_lang) if recipient_lang else "?",
            ),
        )
    except Exception as e:
        log.warning("Failed to notify sender %s of accept: %s", sender_id, e)


async def pair_decline_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    code = query.data[len("pdecline_"):]
    user = query.from_user
    pool: asyncpg.Pool = context.bot_data["pool"]

    pair = await get_pair_by_code(pool, code)
    if not pair:
        return

    sender_id = pair["sender_id"]
    if pair["status"] == "pending":
        await unpair_db(pool, pair["id"])

    s_recipient = await _strings_for(pool, user.id)
    await query.edit_message_text(s_recipient["pair_decline_done"])

    # Notify the sender
    try:
        s_sender = await _strings_for(pool, sender_id)
        recipient_name = user.first_name or "the recipient"
        await context.bot.send_message(
            chat_id=sender_id,
            text=s_sender["pair_declined_to_sender"].format(recipient_name=recipient_name),
        )
    except Exception as e:
        log.warning("Failed to notify sender %s of decline: %s", sender_id, e)


async def forward_undo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Undo a recent forward by deleting the message in the recipient's chat."""
    query = update.callback_query
    # Format: undo_<recipient_chat_id>_<message_id>
    parts = query.data.split("_")
    try:
        recipient_chat_id = int(parts[1])
        message_id = int(parts[2])
    except (IndexError, ValueError):
        await query.answer()
        return

    pool: asyncpg.Pool = context.bot_data["pool"]
    s = await _strings_for(pool, query.from_user.id)
    try:
        await context.bot.delete_message(
            chat_id=recipient_chat_id, message_id=message_id
        )
        await query.answer(s["forward_undo_done"], show_alert=False)
        # Strip the Undo button from the sender's confirmation
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        # Drop the row from forward_messages so a reply won't route through it later
        await pool.execute(
            "DELETE FROM forward_messages WHERE delivered_chat_id = $1 AND delivered_message_id = $2",
            recipient_chat_id, message_id,
        )
    except Exception as e:
        log.info("Undo failed (likely too old): %s", e)
        await query.answer(s["forward_undo_too_late"], show_alert=True)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pool: asyncpg.Pool = context.bot_data["pool"]
    s = await _strings_for(pool, update.effective_user.id)
    await update.message.reply_text(s["help_text"])


async def support_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pool: asyncpg.Pool = context.bot_data["pool"]
    s = await _strings_for(pool, update.effective_user.id)
    await update.message.reply_text(s["support_text"], disable_web_page_preview=True)


async def terms_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pool: asyncpg.Pool = context.bot_data["pool"]
    s = await _strings_for(pool, update.effective_user.id)
    await update.message.reply_text(s["terms_text"], disable_web_page_preview=True)


async def paysupport_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pool: asyncpg.Pool = context.bot_data["pool"]
    s = await _strings_for(pool, update.effective_user.id)
    await update.message.reply_text(s["paysupport_text"], disable_web_page_preview=True)


async def voice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show two preview voice messages, each with a 'Use this voice' button.
    User listens, taps the one they want."""
    pool: asyncpg.Pool = context.bot_data["pool"]
    user = update.effective_user
    s = await _strings_for(pool, user.id)
    await update_user_info(pool, user.id, user.first_name, user.username)

    await update.message.reply_text(s["voice_prompt"])

    previews_dir = Path(__file__).parent / "voice_previews"
    for voice_id, label in [("alloy", s["voice_label_alloy"]), ("nova", s["voice_label_nova"])]:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"{s['voice_use_btn']} ({label})",
                callback_data=f"voice_{voice_id}",
            )
        ]])
        try:
            with open(previews_dir / f"{voice_id}.mp3", "rb") as f:
                await context.bot.send_voice(
                    chat_id=update.effective_chat.id,
                    voice=f,
                    caption=label,
                    reply_markup=kb,
                )
        except FileNotFoundError:
            log.warning("Voice preview missing: %s", voice_id)


async def voice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle taps on the 'Use this voice' button under each preview."""
    query = update.callback_query
    await query.answer()
    voice_id = query.data[len("voice_"):]
    if voice_id not in SUPPORTED_VOICES:
        return
    pool: asyncpg.Pool = context.bot_data["pool"]
    user = query.from_user
    s = await _strings_for(pool, user.id)

    current = await get_user_voice(pool, user.id)
    if current == voice_id:
        await query.answer(s[f"voice_already_set_{voice_id}"], show_alert=False)
        return

    await set_user_voice(pool, user.id, voice_id)
    await context.bot.send_message(
        chat_id=user.id,
        text=s[f"voice_set_{voice_id}"],
    )


async def lang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pool: asyncpg.Pool = context.bot_data["pool"]
    user = update.effective_user
    src, tgt = await get_user_prefs(pool, user.id)
    s = await _strings_for(pool, user.id)

    if tgt:
        src_label = lang_label(src) if src and src in SUPPORTED else "🔄 Auto-detect"
        plan_status = await _plan_status(pool, user.id, s)
        await update.message.reply_text(
            s["current_settings"].format(src=src_label, tgt=lang_label(tgt))
            + f"\n\n{plan_status}",
            reply_markup=settings_keyboard(s),
        )
    elif src:
        await update.message.reply_text(
            s["what_translate_to"],
            reply_markup=target_keyboard(s, exclude=src),
        )
    else:
        await update.message.reply_text(
            s["what_do_you_speak"],
            reply_markup=lang_keyboard("src_"),
        )


async def setlang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await lang_cmd(update, context)


# ---------- Callbacks ----------
async def src_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lang = query.data[4:]  # strip "src_"
    pool: asyncpg.Pool = context.bot_data["pool"]

    await set_user_source(pool, query.from_user.id, lang)
    s = await _strings(pool, lang)

    await query.edit_message_text(
        s["what_translate_to"],
        reply_markup=target_keyboard(s, exclude=lang),
    )


async def tgt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    tgt = query.data[4:]  # strip "tgt_"
    pool: asyncpg.Pool = context.bot_data["pool"]
    user = query.from_user

    await set_user_target(pool, user.id, tgt)
    src, _ = await get_user_prefs(pool, user.id)

    ui_lang = src or "en"
    s = await _strings(pool, ui_lang)

    src_label = lang_label(src) if src and src in SUPPORTED else "?"
    await query.edit_message_text(
        s["all_set"].format(src=src_label, tgt=lang_label(tgt))
    )


async def change_src_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    pool: asyncpg.Pool = context.bot_data["pool"]
    s = await _strings_for(pool, query.from_user.id)

    await query.edit_message_text(
        s["what_do_you_speak"],
        reply_markup=lang_keyboard("src_"),
    )


async def change_tgt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    pool: asyncpg.Pool = context.bot_data["pool"]
    user = query.from_user

    src, _ = await get_user_prefs(pool, user.id)
    s = await _strings_for(pool, user.id)

    await query.edit_message_text(
        s["what_translate_to"],
        reply_markup=lang_keyboard("tgt_", exclude=src),
    )


# ---------- Payment callbacks ----------
async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    plan = query.data[4:]  # "basic" or "pro"
    stars = PLAN_STARS[plan]
    title = "Basic Plan" if plan == "basic" else "Pro Plan"
    plan_desc = "100 messages per month" if plan == "basic" else "Unlimited messages per month"
    # Terms reference appended so the user explicitly accepts by tapping Pay
    # (Telegram Stars policy requires confirmation that user has read T&Cs).
    description = f"{plan_desc} + auto-forward to a contact. Tap Pay to agree to our Terms: tryrespeak.com/terms"
    try:
        await context.bot.send_invoice(
            chat_id=query.from_user.id,
            title=title,
            description=description,
            payload=plan,
            currency="XTR",
            prices=[LabeledPrice(title, stars)],
        )
    except Exception as e:
        log.exception("send_invoice failed: %s", e)
        await context.bot.send_message(chat_id=query.from_user.id, text=f"Invoice error: {e}")


async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.pre_checkout_query.answer(ok=True)


async def payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    pool: asyncpg.Pool = context.bot_data["pool"]
    plan  = msg.successful_payment.invoice_payload  # "basic" or "pro"
    stars = msg.successful_payment.total_amount
    await set_plan(pool, msg.from_user.id, plan)
    await record_payment(pool, msg.from_user.id, plan, stars)
    await update_user_info(pool, msg.from_user.id, msg.from_user.first_name, msg.from_user.username)
    s = await _strings_for(pool, msg.from_user.id)
    from datetime import datetime, timedelta
    expiry = (datetime.utcnow() + timedelta(days=30)).strftime("%b %-d")
    plan_label = "Basic" if plan == "basic" else "Pro"
    await msg.reply_text(s["plan_activated"].format(plan=plan_label, date=expiry))

    user = msg.from_user
    username_str = f" (@{user.username})" if user.username else ""
    await _notify_admin(
        context,
        f"💸 Payment: {plan_label} (⭐ {stars})\n"
        f"User: {user.first_name or 'Unknown'}{username_str}\n"
        f"ID: {user.id}",
    )


# ---------- Voice handler ----------
async def _determine_routing(
    pool: asyncpg.Pool, user_id: int, msg
) -> tuple[str, dict | None]:
    """Decide whether this voice should auto-forward through a pair, or be a
    solo translation.

    Returns ("solo", None) or ("pair", pair_dict).

    Order of precedence:
      1. If the message is a Telegram-native Reply on a previously-forwarded
         message, route through that pair (works regardless of role).
      2. Else, if the user is the channel-owner of an active outgoing pair,
         auto-forward through it.
      3. Else, solo translation.
    """
    if msg.reply_to_message:
        pair = await find_pair_by_replied_message(
            pool, msg.chat_id, msg.reply_to_message.message_id
        )
        if pair:
            return ("pair", pair)
    pair = await get_active_pair_for_sender(pool, user_id)
    if pair:
        return ("pair", pair)
    return ("solo", None)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    user = update.effective_user
    pool: asyncpg.Pool = context.bot_data["pool"]

    if ALLOWED_USERS and user.id not in ALLOWED_USERS:
        log.warning("Unauthorized user %s (%s)", user.id, user.username)
        s = await _strings_for(pool, user.id)
        await msg.reply_text(s["private_bot"])
        return

    if not (msg.voice or msg.audio):
        return

    await update_user_info(pool, user.id, user.first_name, user.username)
    src, tgt = await get_user_prefs(pool, user.id)
    s = await _strings_for(pool, user.id)

    # --- Routing decision ---
    routing_mode, pair = await _determine_routing(pool, user.id, msg)

    target_lang: str | None = None
    other_user_id: int | None = None
    billing_user_id: int = user.id

    if routing_mode == "pair":
        # Determine the OTHER side of the pair (where the voice goes)
        other_user_id = (
            pair["recipient_id"] if user.id == pair["sender_id"] else pair["sender_id"]
        )
        # The channel-owner (sender_id) always pays
        billing_user_id = pair["sender_id"]
        # Translate INTO the recipient-side's language
        _, target_lang = await get_user_prefs(pool, other_user_id)
        if not target_lang:
            # Other user lost their setlang somehow — fall back to solo so we
            # don't drop the message
            log.warning(
                "Pair %d: other user %d has no tgt — falling back to solo",
                pair["id"], other_user_id,
            )
            routing_mode = "solo"
            other_user_id = None
            billing_user_id = user.id

    if routing_mode == "solo":
        if not tgt:
            await msg.reply_text(s["setup_first"])
            return
        target_lang = tgt

    # --- Plan / quota gate (against billing user) ---
    plan = await get_plan(pool, billing_user_id)
    limit = PLAN_LIMITS[plan]
    if limit is not None:
        count = await get_message_count(pool, billing_user_id)
        if count >= limit:
            if routing_mode == "pair" and billing_user_id != user.id:
                # The actual voice-sender is the recipient side; tell them the
                # channel-owner's quota is exhausted.
                await msg.reply_text(s["forward_failed_quota"])
            else:
                await msg.reply_text(
                    s["limit_hit"].format(limit=limit),
                    reply_markup=upgrade_keyboard(s),
                )
            return

    audio_obj = msg.voice or msg.audio
    duration = getattr(audio_obj, "duration", 0)
    log.info(
        "Voice from %s (%s): %ds [route=%s]",
        user.username or user.first_name, user.id, duration, routing_mode,
    )

    if duration > 300:
        await msg.reply_text(s["too_long"].format(duration=duration))
        return

    status = await msg.reply_text(s["translating"])

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        in_path = td / "input.ogg"
        out_path = td / "translated.ogg"

        try:
            tg_file = await audio_obj.get_file()
            await tg_file.download_to_drive(custom_path=str(in_path))

            transcript, src_lang = await transcribe(in_path, hint=src)
            log.info("%s -> %s", src_lang, target_lang)

            src_flag = SUPPORTED.get(src_lang, {}).get("flag", "🌐")
            tgt_flag = SUPPORTED.get(target_lang, {}).get("flag", "🌐")
            await status.edit_text(
                s["translating_langs"].format(src=src_flag, tgt=tgt_flag)
            )

            if not transcript:
                await status.edit_text(s["no_speech"])
                return

            translation, in_tok, out_tok = await translate(transcript, src_lang, target_lang)
            # Voice belongs to the speaker. For solo, that's the current user.
            # For pair routing, that's still the current user (the one whose
            # voice was just transcribed) — the listener doesn't get to choose.
            speaker_voice = await get_user_voice(pool, user.id)
            await synthesize(translation, out_path, voice=speaker_voice)

            whisper_cost = max(duration, 1) * 0.006 / 60
            gpt_cost     = (in_tok * 0.150 + out_tok * 0.600) / 1_000_000
            tts_cost     = len(translation) * 15.0 / 1_000_000
            total_cost   = whisper_cost + gpt_cost + tts_cost

            if routing_mode == "pair":
                # Send only the voice to the other side (no text — voice-only spec)
                try:
                    with open(out_path, "rb") as f:
                        sent = await context.bot.send_voice(
                            chat_id=other_user_id, voice=f
                        )
                except Exception as fwd_err:
                    log.warning(
                        "Forward to %s failed: %s", other_user_id, fwd_err
                    )
                    other_name = (
                        await get_user_first_name(pool, other_user_id) or "user"
                    )
                    await status.edit_text(
                        s["forward_failed_blocked"].format(name=other_name)
                    )
                    return

                # Record for reply-routing
                await record_forward_message(
                    pool, pair["id"], other_user_id, sent.message_id
                )
                # Quota always counts against the channel-owner
                await increment_usage(pool, billing_user_id, total_cost)

                # Confirm to the actual voice-sender with source transcript + Undo
                other_name = (
                    await get_user_first_name(pool, other_user_id) or "user"
                )
                undo_kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        s["forward_undo_btn"],
                        callback_data=f"undo_{other_user_id}_{sent.message_id}",
                    )
                ]])
                await status.edit_text(
                    f"{s['forward_confirm_to_sender'].format(name=other_name)}"
                    f"\n\n{src_flag} {transcript}",
                    reply_markup=undo_kb,
                )
            else:
                # Solo translation — original behavior
                target_chat = FORWARD_TO if FORWARD_TO else msg.chat_id
                with open(out_path, "rb") as f:
                    await context.bot.send_voice(chat_id=target_chat, voice=f)
                await context.bot.send_message(
                    chat_id=target_chat, text=f"{src_flag} {transcript}"
                )
                await context.bot.send_message(
                    chat_id=target_chat, text=f"{tgt_flag} {translation}"
                )
                await status.delete()
                await increment_usage(pool, billing_user_id, total_cost)
                if FORWARD_TO and str(target_chat) != str(msg.chat_id):
                    await msg.reply_text(f"✅ Sent to {FORWARD_TO}")

        except Exception as e:
            log.exception("Pipeline failed")
            await status.edit_text(s["error"].format(error=type(e).__name__))


# ---------- Admin ----------
ADMIN_ID = 981622851

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMyStarBalance")
    stars = resp.json()["result"]["amount"]
    await update.message.reply_text(f"⭐ Bot balance: {stars} Stars")


# ---------- Error handler ----------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Conflict):
        log.warning("Conflict during redeploy — ignoring")
        return
    log.exception("Unhandled error: %s", context.error)


# ---------- Lifecycle ----------
async def post_init(application: Application) -> None:
    import asyncio
    import uvicorn
    from dashboard import app as dash_app

    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
    await init_db(pool)
    application.bot_data["pool"] = pool
    log.info("DB pool ready")

    dash_app.state.pool = pool
    port = int(os.getenv("PORT", "8080"))
    config = uvicorn.Config(dash_app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # let PTB own the signals
    asyncio.create_task(server.serve())
    log.info("Dashboard listening on port %d", port)


async def post_shutdown(application: Application) -> None:
    await application.bot_data["pool"].close()
    log.info("DB pool closed")


# ---------- Main ----------
def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("support", support_cmd))
    app.add_handler(CommandHandler("terms", terms_cmd))
    app.add_handler(CommandHandler("paysupport", paysupport_cmd))
    app.add_handler(CommandHandler("lang", lang_cmd))
    app.add_handler(CommandHandler("setlang", setlang))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("forward", forward_cmd))
    app.add_handler(CommandHandler("unforward", unforward_cmd))
    app.add_handler(CommandHandler("voice", voice_cmd))
    app.add_handler(CallbackQueryHandler(src_callback,        pattern="^src_"))
    app.add_handler(CallbackQueryHandler(tgt_callback,        pattern="^tgt_"))
    app.add_handler(CallbackQueryHandler(change_src_callback, pattern="^change_src$"))
    app.add_handler(CallbackQueryHandler(change_tgt_callback, pattern="^change_tgt$"))
    app.add_handler(CallbackQueryHandler(buy_callback,         pattern="^buy_"))
    app.add_handler(CallbackQueryHandler(pair_lang_callback,    pattern="^plang_"))
    app.add_handler(CallbackQueryHandler(pair_accept_callback,  pattern="^paccept_"))
    app.add_handler(CallbackQueryHandler(pair_decline_callback, pattern="^pdecline_"))
    app.add_handler(CallbackQueryHandler(forward_undo_callback, pattern="^undo_"))
    app.add_handler(CallbackQueryHandler(voice_callback,        pattern="^voice_"))
    app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, payment_handler))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))

    log.info("Bot starting. Allowed users: %s",
             ALLOWED_USERS if ALLOWED_USERS else "ANYONE (open)")
    log.info("Forward target: %s", FORWARD_TO if FORWARD_TO else "reply to sender")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
