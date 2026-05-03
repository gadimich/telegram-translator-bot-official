"""UI string translations with PostgreSQL-backed cache.

The cache key includes a hash of UI_STRINGS content, so when the developer
edits UI_STRINGS (adds/removes/changes a key), every language's cache entry
becomes invalid automatically — the next user request triggers a fresh
translation. Old rows are upserted in place, no cleanup needed.
"""
from __future__ import annotations

import hashlib
import json
import logging

import asyncpg

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
    "limit_hit": "You've used all {limit} free messages this month. Upgrade to keep translating.",
    "upgrade_basic_btn": "⭐ Basic — {limit} msgs/month ($5.99)",
    "upgrade_pro_btn": "⭐ Pro — Unlimited ($19.99)",
    "plan_activated": "✅ {plan} activated! You're good until {date}. Send a voice message to keep translating! 🎙",
    "plan_status_pro": "⭐ Pro — unlimited (until {date})",
    "plan_status_basic": "⭐ Basic — {count}/{limit} messages (until {date})",
    "plan_status_free": "Free — {count}/{limit} messages this month",
    "help_text": (
        "Send me a voice message — I'll translate it and reply with a voice note.\n\n"
        "Commands:\n"
        "/start – greeting + setup\n"
        "/lang – show or change your language settings\n"
        "/balance – view your plan and remaining messages\n"
        "/forward – set up auto-forwarding to a paired person (Basic / Pro)\n"
        "/unforward – remove the forwarding pair\n"
        "/terms – read our Terms of Service\n"
        "/support – contact us for help\n"
        "/paysupport – payment and subscription support\n"
        "/help – this message"
    ),
    "support_text": (
        "💬 Need help?\n\n"
        "Email: info@tryrespeak.com\n"
        "Website: https://tryrespeak.com\n\n"
        "We respond within one business day. For payment or subscription issues, use /paysupport."
    ),
    "terms_text": (
        "📄 Terms of Service\n\n"
        "Read the full terms at https://tryrespeak.com/terms\n\n"
        "Privacy Policy: https://tryrespeak.com/privacy\n\n"
        "By using Respeak you agree to these terms."
    ),
    "paysupport_text": (
        "💳 Payment support\n\n"
        "For any payment or subscription issue, contact us at info@tryrespeak.com — we handle all payment questions and refund requests directly.\n\n"
        "⚠️ Telegram support cannot help with bot payments. All payment issues must go through us.\n\n"
        "Subscriptions are paid via Telegram Stars and don't auto-renew — they expire at the end of the billing period.\n\n"
        "Refund policy: refunds may be issued at our discretion within 7 days of purchase in cases of service unavailability or billing error. See https://tryrespeak.com/terms for details."
    ),
    # Forward feature (auto-forward voice messages to a paired recipient)
    "forward_paid_only": "Auto-forwarding is a Basic and Pro feature. Upgrade to send your voice messages directly to a specific person.",
    "forward_no_pair": "You don't have a forwarding pair set up yet. Use /forward setup to create one.",
    "forward_setup_link": "Share this link with the person who should receive your translated voice messages:\n\n{link}\n\nThe link expires in 72 hours. Once they accept, your voice messages will be auto-forwarded to them in their language.",
    "forward_pending": "You have a pending invite. Share this link with them:\n\n{link}\n\nWaiting for them to accept.",
    "forward_active_status": "✅ Forwarding active to {name}.\n\nVoice messages you send are auto-translated to {lang} and delivered to them. They can reply by tapping & holding your message → Reply.\n\n/forward pause — temporarily stop\n/unforward — remove the pair",
    "forward_paused_status": "⏸ Forwarding paused.\n\n/forward resume — re-enable\n/unforward — remove the pair",
    "forward_pause_done": "Forwarding paused. Voice messages will translate normally for now. Use /forward resume to re-enable.",
    "forward_pause_pending": "Your pair invite hasn't been accepted yet — nothing to pause. Use /unforward to cancel the invite, or wait for them to accept.",
    "forward_resume_done": "Forwarding resumed. Voice messages will be sent to {name}.",
    "forward_resume_no_pair": "Nothing to resume. Use /forward setup to create a pair.",
    "unforward_done": "Unpaired from {name}. They've been notified.",
    "unforward_no_pair": "You don't have an active forwarding pair.",
    "pair_invite_received": "{sender_name} wants to send you translated voice messages via Respeak.\n\nYou'll receive their messages in {lang}. To reply, tap & hold their message → Reply → record your voice. Your reply will auto-translate back to them.\n\nAccept?",
    "pair_invite_lang_prompt": "{sender_name} wants to send you translated voice messages. First — what language do you speak?",
    "pair_invite_expired": "This invite has expired or is no longer valid. Ask {sender_name} for a fresh pairing link.",
    "pair_invite_already_active": "You're already paired with this person.",
    "pair_accept_btn": "✅ Accept",
    "pair_decline_btn": "❌ Decline",
    "pair_accepted_to_sender": "✅ {recipient_name} accepted! Your voice messages will now auto-forward to them in {lang}.",
    "pair_accepted_to_recipient": "✅ Paired with {sender_name}. Their voice messages will arrive in {lang}.\n\nTo reply: tap & hold their message → Reply → send your voice. Your reply will translate back to {sender_lang} for them.",
    "pair_declined_to_sender": "{recipient_name} declined the pairing.",
    "pair_decline_done": "Declined.",
    "unforward_recipient_notice": "❌ {sender_name} has unpaired from you. Their voice messages will no longer arrive here.",
    "forward_confirm_to_sender": "✓ Forwarded to {name}",
    "forward_undo_btn": "↩️ Undo",
    "forward_undo_done": "Forward undone.",
    "forward_undo_too_late": "Too late to undo this one.",
    "forward_failed_blocked": "⚠️ Couldn't forward to {name} — they may have blocked the bot. Use /unforward to remove the pairing.",
    "forward_failed_quota": "⚠️ Couldn't forward — your monthly limit reached. Upgrade or wait for next month.",
}

# Content hash of UI_STRINGS — when developer changes any value or key, this changes,
# and any DB rows with the old hash are bypassed (treated as cache misses).
_UI_HASH = hashlib.sha256(
    json.dumps(UI_STRINGS, sort_keys=True, ensure_ascii=False).encode("utf-8")
).hexdigest()[:16]

# Process-local cache. Avoids DB roundtrip for repeat requests within one process.
# Wiped on every restart, then warmed from DB or fresh translation.
_cache: dict[str, dict[str, str]] = {"en": UI_STRINGS}


async def get_strings(
    lang: str,
    lang_name: str,
    client,
    pool: asyncpg.Pool | None = None,
) -> dict[str, str]:
    if lang in _cache:
        return _cache[lang]

    # Try persistent cache (skipped if no pool, e.g. during early startup).
    if pool is not None:
        try:
            row = await pool.fetchrow(
                "SELECT strings FROM ui_strings_cache WHERE lang = $1 AND hash = $2",
                lang,
                _UI_HASH,
            )
            if row:
                cached = json.loads(row["strings"])
                strings = {k: cached.get(k, UI_STRINGS[k]) for k in UI_STRINGS}
                _cache[lang] = strings
                log.info("UI strings loaded from DB cache for %s (hash=%s)", lang_name, _UI_HASH)
                return strings
        except Exception as e:
            log.warning("DB cache lookup failed for %s, will translate fresh: %s", lang_name, e)

    log.info("Translating UI strings to %s (hash=%s)...", lang_name, _UI_HASH)
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
    strings = {k: translated.get(k, UI_STRINGS[k]) for k in UI_STRINGS}
    _cache[lang] = strings

    if pool is not None:
        try:
            await pool.execute(
                """
                INSERT INTO ui_strings_cache (lang, hash, strings, updated_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (lang) DO UPDATE SET hash = $2, strings = $3, updated_at = NOW()
                """,
                lang,
                _UI_HASH,
                json.dumps(strings, ensure_ascii=False),
            )
            log.info("UI strings persisted to DB for %s", lang_name)
        except Exception as e:
            log.warning("DB cache write failed for %s: %s", lang_name, e)

    return strings
