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


# ---------- UI string helpers ----------
def _tg_lang(user) -> str | None:
    code = (user.language_code or "").split("-")[0].lower()
    return code if code in SUPPORTED else None


async def _strings(lang: str) -> dict[str, str]:
    lang_name = SUPPORTED.get(lang, SUPPORTED["en"])["name"]
    return await get_strings(lang, lang_name, openai_client)


async def _strings_for(pool: asyncpg.Pool, user_id: int) -> dict[str, str]:
    src, _ = await get_user_prefs(pool, user_id)
    return await _strings(src or "en")


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


async def synthesize(text: str, out_path: Path) -> Path:
    log.info("Synthesizing speech...")
    resp = await openai_client.audio.speech.create(
        model="tts-1", voice=TTS_VOICE, input=text, response_format="opus",
    )
    out_path.write_bytes(resp.content)
    log.info("Wrote %s (%d bytes)", out_path.name, out_path.stat().st_size)
    return out_path


# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    pool: asyncpg.Pool = context.bot_data["pool"]
    await update_user_info(pool, user.id, user.first_name, user.username)
    src, tgt = await get_user_prefs(pool, user.id)

    detected = _tg_lang(user)
    ui_lang = src or detected or "en"
    s = await _strings(ui_lang)

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


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pool: asyncpg.Pool = context.bot_data["pool"]
    s = await _strings_for(pool, update.effective_user.id)
    await update.message.reply_text(s["help_text"])


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
    s = await _strings(lang)

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
    s = await _strings(ui_lang)

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
    description = "100 messages per month" if plan == "basic" else "Unlimited messages per month"
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


# ---------- Voice handler ----------
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

    if not tgt:
        await msg.reply_text(s["setup_first"])
        return

    # --- Plan / usage gate ---
    plan = await get_plan(pool, user.id)
    limit = PLAN_LIMITS[plan]
    if limit is not None:
        count = await get_message_count(pool, user.id)
        if count >= limit:
            await msg.reply_text(
                s["limit_hit"].format(limit=limit),
                reply_markup=upgrade_keyboard(s),
            )
            return

    audio_obj = msg.voice or msg.audio
    duration = getattr(audio_obj, "duration", 0)
    log.info("Voice from %s (%s): %ds", user.username or user.first_name, user.id, duration)

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
            log.info("%s -> %s", src_lang, tgt)

            src_flag = SUPPORTED.get(src_lang, {}).get("flag", "🌐")
            tgt_flag = SUPPORTED.get(tgt, {}).get("flag", "🌐")
            await status.edit_text(s["translating_langs"].format(src=src_flag, tgt=tgt_flag))

            if not transcript:
                await status.edit_text(s["no_speech"])
                return

            translation, in_tok, out_tok = await translate(transcript, src_lang, tgt)
            await synthesize(translation, out_path)

            whisper_cost = max(duration, 1) * 0.006 / 60
            gpt_cost     = (in_tok * 0.150 + out_tok * 0.600) / 1_000_000
            tts_cost     = len(translation) * 15.0 / 1_000_000
            total_cost   = whisper_cost + gpt_cost + tts_cost

            target_chat = FORWARD_TO if FORWARD_TO else msg.chat_id

            with open(out_path, "rb") as f:
                await context.bot.send_voice(chat_id=target_chat, voice=f)

            await context.bot.send_message(
                chat_id=target_chat,
                text=f"{src_flag} {transcript}",
            )
            await context.bot.send_message(
                chat_id=target_chat,
                text=f"{tgt_flag} {translation}",
            )

            await status.delete()
            await increment_usage(pool, user.id, total_cost)

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
    app.add_handler(CommandHandler("lang", lang_cmd))
    app.add_handler(CommandHandler("setlang", setlang))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CallbackQueryHandler(src_callback,        pattern="^src_"))
    app.add_handler(CallbackQueryHandler(tgt_callback,        pattern="^tgt_"))
    app.add_handler(CallbackQueryHandler(change_src_callback, pattern="^change_src$"))
    app.add_handler(CallbackQueryHandler(change_tgt_callback, pattern="^change_tgt$"))
    app.add_handler(CallbackQueryHandler(buy_callback,         pattern="^buy_"))
    app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, payment_handler))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))

    log.info("Bot starting. Allowed users: %s",
             ALLOWED_USERS if ALLOWED_USERS else "ANYONE (open)")
    log.info("Forward target: %s", FORWARD_TO if FORWARD_TO else "reply to sender")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
