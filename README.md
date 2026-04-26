# FeedBuddy

Small local bot that:

- polls RSS and Atom feeds
- sends new entries to Telegram
- lets you add feeds from Telegram with `/addfeed <url>`
- saves items to Trello with a button
- can also save by Telegram reaction if the bot sees reaction updates
- shows the last 10 sent articles in a tiny local web page

No web server. No exposed ports. It works with Telegram long polling.

Config is loaded from a local `.env` file if present, then from normal environment variables.

## Files

- `feedbuddy.py`: the whole app
- `sources.txt`: initial feed list, one URL per line
- `feedbuddy.db`: SQLite state, created on first run

## Setup

Create a virtualenv if you want, then install deps:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create `.env`:

```bash
cp .env.example .env
```

Notes:

- `TELEGRAM_CHAT_ID` is where feed items are sent.
- `TELEGRAM_ADMIN_USER_ID` is the user allowed to run bot commands.
- `WEB_HOST` and `WEB_PORT` control the local web UI. Default is `127.0.0.1:8080`.
- Trello vars are optional. Without them, Trello save is disabled.
- Shell environment variables still work and override nothing already set before startup.

## Run

```bash
python3 feedbuddy.py
```

Then open:

```text
http://127.0.0.1:8080/
```

## Telegram commands

- `/help`
- `/listfeeds`
- `/addfeed <url>`
- `/delfeed <url>`
- `/summary`
- `/testsend`

## Trello save

Preferred MVP flow:

- the bot sends an item to Telegram
- press the `Save to Trello` button

Reaction flow:

- if Telegram delivers `message_reaction` updates to your bot in that chat
- and you react with `⭐`
- the bot will also save the item to Trello

For reaction-based saving, Telegram chat permissions and bot privileges matter. The inline button is the reliable path.

## Behavior

- feeds from `sources.txt` are imported on startup
- existing entries are marked as seen on first import, so the bot does not dump old posts on you
- new feeds added with `/addfeed` are also caught up silently
- state lives in SQLite
- the web UI shows the last 10 sent articles in chronological order

## Systemd example

```ini
[Unit]
Description=FeedBuddy
After=network-online.target

[Service]
WorkingDirectory=/Users/jacopomoioli/Documents/FeedBuddy
Environment=TELEGRAM_BOT_TOKEN=123456:abc
Environment=TELEGRAM_CHAT_ID=123456789
Environment=TELEGRAM_ADMIN_USER_ID=123456789
Environment=TRELLO_KEY=...
Environment=TRELLO_TOKEN=...
Environment=TRELLO_LIST_ID=...
ExecStart=/usr/bin/python3 /Users/jacopomoioli/Documents/FeedBuddy/feedbuddy.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
