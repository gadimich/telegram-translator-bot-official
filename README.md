# tg-voice-translator

Bidirectional EN ↔ ES Telegram voice translation bot. Send a voice message, get a translated voice message back.

**Pipeline:** Telegram Bot API → Whisper (transcription) → GPT-4o-mini (translation) → OpenAI TTS (speech synthesis)

---

## Features

- Per-user language preference (`/setlang en` or `/setlang es`) persisted across restarts
- Optional allowlist (`ALLOWED_USERS`) to keep your OpenAI bill in check
- Optional `FORWARD_TO` to send translations to a different chat or channel

---

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env with your real tokens

python bot.py
```

---

## Railway deployment

### Prerequisites

- [Railway CLI](https://docs.railway.app/develop/cli) or the Railway dashboard
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- An [OpenAI API key](https://platform.openai.com/api-keys)

### Steps

1. **Create a new Railway project**

   Via dashboard: New Project → Deploy from GitHub repo (or "Empty project" if pushing manually).

   Via CLI:
   ```bash
   railway login
   railway init        # creates a new project linked to this directory
   ```

2. **Set environment variables**

   In the Railway dashboard → your service → Variables, add each variable from `.env.example`.
   The only required ones are `BOT_TOKEN` and `OPENAI_API_KEY`.

   Via CLI:
   ```bash
   railway variables set BOT_TOKEN=<your-token>
   railway variables set OPENAI_API_KEY=<your-key>
   railway variables set ALLOWED_USERS=<your-telegram-id>
   ```

3. **Deploy**

   ```bash
   railway up
   ```

   Railway detects `requirements.txt` automatically (Python nixpack). The `Procfile` tells it to run the bot as a `worker` (long-polling, no HTTP port needed).

4. **Verify**

   In Railway dashboard → your service → Logs, you should see:
   ```
   Bot starting. Allowed users: {your_id}
   ```
   Send `/start` to your bot in Telegram to confirm.

### Persistent user preferences

Railway's filesystem is **ephemeral** — `user_prefs.json` resets on every redeploy. To persist it across deploys, add a [Railway Volume](https://docs.railway.app/reference/volumes) and set:

```
PREFS_FILE=/data/user_prefs.json
```

where `/data` is the volume's mount path. Without this, users need to re-run `/setlang` after each redeploy.

### Environment variable reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `BOT_TOKEN` | Yes | — | Telegram bot token from @BotFather |
| `OPENAI_API_KEY` | Yes | — | OpenAI API key |
| `ALLOWED_USERS` | Recommended | (open) | Comma-separated Telegram user IDs. Leave empty to allow everyone — costs you money. |
| `FORWARD_TO` | No | (reply to sender) | Chat ID or `@username` to forward translations to |
| `TTS_VOICE` | No | `alloy` | OpenAI TTS voice: `alloy`, `echo`, `fable`, `onyx`, `nova`, `shimmer` |
| `DEFAULT_SOURCE_LANG` | No | `en` | Language assumed for users who haven't run `/setlang` |
| `PREFS_FILE` | No | `user_prefs.json` | Path to the user-preferences file |

---

## Recovery: accidentally committed `.env`

If you committed `.env` (or any file containing real tokens), treat **both secrets as compromised** immediately regardless of whether anyone saw them.

### 1 — Revoke the secrets first (do this now, before touching git)

- **Telegram bot token:** Open [@BotFather](https://t.me/BotFather) → select your bot → "Revoke token". This invalidates the old token instantly. Copy the new one.
- **OpenAI key:** Go to [platform.openai.com/api-keys](https://platform.openai.com/api-keys) → find the key → Delete. Create a new key.

### 2 — Remove the file from git history

```bash
# Remove from the index and all past commits
git filter-repo --path .env --invert-paths
```

If you don't have `git filter-repo`: `pip install git-filter-repo`.

> Alternatively, BFG Repo Cleaner is faster for large repos:
> `bfg --delete-files .env && git reflog expire --expire=now --all && git gc --prune=now --aggressive`

### 3 — Force-push to overwrite remote history

```bash
git push origin --force --all
git push origin --force --tags
```

### 4 — Confirm `.gitignore` is in place

```bash
git status   # .env must NOT appear as a tracked file
```

If `.env` still shows up: `git rm --cached .env`, commit, then push.

### 5 — Update secrets everywhere

Update your Railway environment variables (and any other places you've used these keys) with the newly generated tokens.

---

## Bot commands

| Command | Description |
|---|---|
| `/start` | Greeting + shows current language setting |
| `/setlang en` | You speak English → bot returns Spanish |
| `/setlang es` | You speak Spanish → bot returns English |
| `/lang` | Show your current language setting |
| `/help` | Command reference |
