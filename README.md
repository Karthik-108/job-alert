# Fresher Job Alert Bot

Checks many sources every 10 min, keeps only fresher roles, sends new ones to your Telegram.

## Setup (5 min)
1. `pip install -r requirements.txt`
2. Telegram: message **@BotFather** -> `/newbot` -> copy the token.
   Send any message to your new bot, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `"chat":{"id": ...}`.
3. Create a file named `.env` next to job_alert.py:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_CHAT_ID=123456789
   ```
4. `python job_alert.py --test`  -> you should get a message
5. `python job_alert.py --dry-run` -> prints what matches right now
6. `python job_alert.py` -> runs forever (first run saves old jobs silently, then only NEW ones alert)

## Run without keeping your laptop on
Push this folder to a GitHub repo, add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
under Settings -> Secrets -> Actions. `.github/workflows/alerts.yml` runs it every ~15 min for free.

## Adding more sources
Edit `config.yaml`. Add Telegram channels, RSS feeds, or use `json_api` for any company career page
(F12 -> Network -> Fetch/XHR -> find the request that returns the jobs).
