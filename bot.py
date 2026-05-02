"""
Telegram Voice Translator Bot (bidirectional EN <-> ES)
-------------------------------------------------------
Each user sets their source language once with /setlang en or /setlang es.
The bot then translates voice messages to the opposite language and
replies with a voice message in that language.

Pipeline: Telegram Bot API -> Whisper -> GPT-4o-mini -> OpenAI TTS
"""

from __future__ import annotations

import os
import logging
import tempfile
from pathlib import Path

import asyncpg
from telegram import Update
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from openai import AsyncOpenAI
from dotenv import load_dotenv

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
DEFAULT_SOURCE = os.getenv("DEFAULT_SOURCE_LANG", "en")

# ---------- Language config ----------
SUPPORTED = {
    "en": {"name": "English", "flag": "🇬🇧"},
    "es": {"name": "Spanish", "flag": "🇪🇸"},
}


def opposite(lang: str) -> str:
    return "es" if lang == "en" else "en"


# ---------- DB helpers ----------
async def init_db(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_prefs (
                user_id     BIGINT PRIMARY KEY,
                source_lang TEXT NOT NULL,
                updated_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)


async def get_user_lang(pool: asyncpg.Pool, user_id: int) -> str | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT source_lang FROM user_prefs WHERE user_id = $1", user_id
        )
    return row["source_lang"] if row else None


async def set_user_lang(pool: asyncpg.Pool, user_id: int, lang: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO user_prefs (user_id, source_lang, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (user_id) DO UPDATE SET source_lang = $2, updated_at = NOW()
        """, user_id, lang)


async def clear_user_lang(pool: asyncpg.Pool, user_id: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM user_prefs WHERE user_id = $1", user_id)


# ---------- Pipeline ----------
WHISPER_TO_CODE = {"spanish": "es", "español": "es", "es": "es",
                   "english": "en", "en": "en"}


async def transcribe(audio_path: Path, hint: str | None = None) -> tuple[str, str]:
    if hint:
        log.info("Transcribing %s (lang=%s, user-set)...", audio_path.name, hint)
        with open(audio_path, "rb") as f:
            resp = await openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language=hint,
            )
        text = resp.text.strip()
        log.info("Transcript: %s", text)
        return text, hint

    log.info("Transcribing %s (auto-detect)...", audio_path.name)
    with open(audio_path, "rb") as f:
        resp = await openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            response_format="verbose_json",
        )
    text = resp.text.strip()
    detected = (resp.language or "").lower()
    lang = WHISPER_TO_CODE.get(detected, "en")
    log.info("Whisper detected %r -> %s | Transcript: %s", detected, lang, text)
    return text, lang


async def translate(text: str, source_lang: str, target_lang: str) -> str:
    src_name = SUPPORTED[source_lang]["name"]
    tgt_name = SUPPORTED[target_lang]["name"]
    log.info("Translating %s -> %s ...", src_name, tgt_name)
    resp = await openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    f"You are a professional translator. Translate the user's "
                    f"{src_name} text to natural, conversational {tgt_name}. "
                    "Preserve tone and register (casual stays casual, formal "
                    "stays formal). Output ONLY the translation — no quotes, "
                    "no commentary, no explanations."
                ),
            },
            {"role": "user", "content": text},
        ],
        temperature=0.3,
    )
    translated = resp.choices[0].message.content.strip()
    log.info("Translation: %s", translated)
    return translated


async def synthesize(text: str, out_path: Path) -> Path:
    log.info("Synthesizing speech ...")
    resp = await openai_client.audio.speech.create(
        model="tts-1",
        voice=TTS_VOICE,
        input=text,
        response_format="opus",
    )
    out_path.write_bytes(resp.content)
    log.info("Wrote %s (%d bytes)", out_path.name, out_path.stat().st_size)
    return out_path


# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    pool: asyncpg.Pool = context.bot_data["pool"]
    lang = await get_user_lang(pool, user.id)
    if lang:
        tgt = opposite(lang)
        mode = (f"Fixed: {SUPPORTED[lang]['flag']} {SUPPORTED[lang]['name']} "
                f"→ {SUPPORTED[tgt]['flag']} {SUPPORTED[tgt]['name']}\n"
                "Run /setlang to change or /setlang clear to auto-detect.")
    else:
        mode = "Auto-detect (I'll figure out the language from your voice).\nRun /setlang en or /setlang es to fix it."
    await update.message.reply_text(
        f"¡Hola / Hello {user.first_name}! 👋\n\n"
        "Send me a voice message and I'll translate it.\n\n"
        f"<b>Mode:</b> {mode}\n\n"
        f"Your Telegram user ID: <code>{user.id}</code>",
        parse_mode="HTML",
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Send me a voice message — I'll translate it to the other language "
        "and reply with a voice message.\n\n"
        "Commands:\n"
        "/start – greeting + current mode\n"
        "/setlang en | es – fix the language you speak (skips auto-detect)\n"
        "/setlang clear – go back to auto-detect\n"
        "/lang – show your current setting\n"
        "/help – this message"
    )


async def setlang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    pool: asyncpg.Pool = context.bot_data["pool"]
    args = context.args or []
    arg = args[0].lower() if args else ""

    if arg in ("clear", "auto", ""):
        await clear_user_lang(pool, user.id)
        await update.message.reply_text("✅ Cleared — I'll auto-detect your language from now on.")
        return

    if arg not in SUPPORTED:
        await update.message.reply_text(
            "Usage:\n"
            "/setlang en — you speak English\n"
            "/setlang es — you speak Spanish\n"
            "/setlang clear — back to auto-detect"
        )
        return

    await set_user_lang(pool, user.id, arg)
    tgt = opposite(arg)
    await update.message.reply_text(
        f"✅ Fixed to:\n"
        f"<b>{SUPPORTED[arg]['flag']} {SUPPORTED[arg]['name']} "
        f"→ {SUPPORTED[tgt]['flag']} {SUPPORTED[tgt]['name']}</b>\n\n"
        "Run /setlang clear to go back to auto-detect.",
        parse_mode="HTML",
    )


async def lang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    pool: asyncpg.Pool = context.bot_data["pool"]
    lang = await get_user_lang(pool, user.id)
    if lang:
        tgt = opposite(lang)
        text = (f"<b>{SUPPORTED[lang]['flag']} {SUPPORTED[lang]['name']} "
                f"→ {SUPPORTED[tgt]['flag']} {SUPPORTED[tgt]['name']}</b> (fixed)\n\n"
                "Run /setlang clear to switch to auto-detect.")
    else:
        text = "<b>Auto-detect</b> — I detect your language from each voice message.\n\nRun /setlang en or /setlang es to fix it."
    await update.message.reply_text(text, parse_mode="HTML")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    user = update.effective_user
    pool: asyncpg.Pool = context.bot_data["pool"]

    if ALLOWED_USERS and user.id not in ALLOWED_USERS:
        log.warning("Unauthorized user %s (%s)", user.id, user.username)
        await msg.reply_text(
            "Sorry, this bot is private. Ask the owner to add your user ID."
        )
        return

    if not (msg.voice or msg.audio):
        await msg.reply_text("Please send a voice message (hold the mic button).")
        return

    audio_obj = msg.voice or msg.audio
    duration = getattr(audio_obj, "duration", 0)

    log.info("Voice from %s (%s): %ds", user.username or user.first_name, user.id, duration)

    MAX_SECONDS = 300
    if duration > MAX_SECONDS:
        await msg.reply_text(
            f"That's {duration}s — please keep messages under {MAX_SECONDS}s."
        )
        return

    status = await msg.reply_text("🎧 Translating...")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        in_path = td / "input.ogg"
        out_path = td / "translated.ogg"

        try:
            tg_file = await audio_obj.get_file()
            await tg_file.download_to_drive(custom_path=str(in_path))

            hint = await get_user_lang(pool, user.id)
            transcript, src_lang = await transcribe(in_path, hint=hint)
            tgt_lang = opposite(src_lang)
            log.info("%s -> %s", src_lang, tgt_lang)
            await status.edit_text(
                f"🎧 Translating {SUPPORTED[src_lang]['flag']} → {SUPPORTED[tgt_lang]['flag']}..."
            )

            if not transcript:
                await status.edit_text("Couldn't hear any speech in that. Try again?")
                return

            translation = await translate(transcript, src_lang, tgt_lang)
            await synthesize(translation, out_path)

            target_chat = FORWARD_TO if FORWARD_TO else msg.chat_id

            with open(out_path, "rb") as f:
                await context.bot.send_voice(chat_id=target_chat, voice=f)

            await context.bot.send_message(
                chat_id=target_chat,
                text=f"{SUPPORTED[src_lang]['flag']} {transcript}",
            )

            await context.bot.send_message(
                chat_id=target_chat,
                text=f"{SUPPORTED[tgt_lang]['flag']} {translation}",
            )

            await status.delete()

            if FORWARD_TO and str(target_chat) != str(msg.chat_id):
                await msg.reply_text(f"✅ Sent to {FORWARD_TO}")

        except Exception as e:
            log.exception("Pipeline failed")
            await status.edit_text(f"⚠️ Something broke: {type(e).__name__}")


# ---------- Error handler ----------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Conflict):
        log.warning("Conflict during redeploy — another instance was shutting down, ignoring")
        return
    log.exception("Unhandled error: %s", context.error)


# ---------- Lifecycle ----------
async def post_init(application: Application) -> None:
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
    await init_db(pool)
    application.bot_data["pool"] = pool
    log.info("DB pool ready")


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
    app.add_handler(CommandHandler("setlang", setlang))
    app.add_handler(CommandHandler("lang", lang_cmd))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))

    log.info("Bot starting. Allowed users: %s",
             ALLOWED_USERS if ALLOWED_USERS else "ANYONE (open)")
    log.info("Forward target: %s", FORWARD_TO if FORWARD_TO else "reply to sender")
    log.info("Default source language: %s", DEFAULT_SOURCE)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
