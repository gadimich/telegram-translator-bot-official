"""UI string translations with lazy GPT caching."""
from __future__ import annotations

import json
import logging

log = logging.getLogger("voice-translator")

UI_STRINGS: dict[str, str] = {
    "welcome_new": "Hello {name}! 👋\n\nI translate voice messages between any languages instantly.\n\n🎙 Send me a voice message — I'll translate it and reply with a voice note.",
    "detected_lang_prompt": "I detected your language as {lang}. Not right? Change it below.\n\nWhat language do you want to translate to?",
    "unknown_lang_prompt": "What language do you want to translate to?",
    "dont_speak": "I don't speak {lang}",
    "set_source": "🌐 Set my language",
    "what_do_you_speak": "What language do you speak?",
    "what_translate_to": "What language do you want to translate to?",
    "all_set": "✅ All set!\n\n{src} → {tgt}\n\nSend me a voice message to get started! 🎙",
    "welcome_back": "Hello {name}! 👋\n\nYou speak {src} and I translate to {tgt}.\n\nSend a voice message to translate, or update your settings:",
    "current_settings": "Current settings:\nYou speak: {src}\nTranslate to: {tgt}",
    "change_source": "Change source language",
    "change_target": "Change target language",
    "setup_first": "Please finish setup first — send /start to pick your languages.",
    "translating": "🎧 Translating...",
    "translating_langs": "🎧 Translating {src} → {tgt}...",
    "no_speech": "Couldn't hear any speech in that. Try again?",
    "too_long": "That's {duration}s — please keep messages under 5 minutes.",
    "error": "⚠️ Something went wrong: {error}",
    "private_bot": "Sorry, this bot is private. Ask the owner to add your user ID.",
    "help_text": (
        "Send me a voice message — I'll translate it and reply with a voice note.\n\n"
        "Commands:\n"
        "/start – greeting + setup\n"
        "/lang – show or change your language settings\n"
        "/help – this message"
    ),
}

_cache: dict[str, dict[str, str]] = {"en": UI_STRINGS}


async def get_strings(lang: str, lang_name: str, client) -> dict[str, str]:
    if lang in _cache:
        return _cache[lang]

    log.info("Translating UI strings to %s...", lang_name)
    resp = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    f"Translate the following JSON object from English to {lang_name}. "
                    "Preserve all {placeholder} markers exactly as-is. "
                    "Return valid JSON with the same keys, no extra keys."
                ),
            },
            {"role": "user", "content": json.dumps(UI_STRINGS, ensure_ascii=False)},
        ],
        temperature=0.1,
        response_format={"type": "json_object"},
    )
    translated = json.loads(resp.choices[0].message.content)
    # Fall back to English for any missing keys
    strings = {k: translated.get(k, UI_STRINGS[k]) for k in UI_STRINGS}
    _cache[lang] = strings
    log.info("UI strings cached for %s", lang_name)
    return strings
