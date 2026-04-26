# FeedBuddy

Local RSS bot for Telegram.

It:

- polls feeds
- sends new posts to Telegram
- lets you add/remove feeds from Telegram
- can save posts to Trello
- shows the last 10 sent posts on a local web page

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill `.env`.

Required:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Recommended:

- `TELEGRAM_ADMIN_USER_ID`

Optional:

- `WEB_HOST` default `127.0.0.1`
- `WEB_PORT` default `8080`
- `TRELLO_KEY`
- `TRELLO_TOKEN`
- `TRELLO_LIST_ID`
- `TRELLO_BOARD_ID`

## Run

```bash
python3 feedbuddy.py
```

Web UI:

- [http://127.0.0.1:8080/](http://127.0.0.1:8080/)

## Telegram

- `/help`
- `/listfeeds`
- `/addfeed <url>`
- `/delfeed <url>`
- `/summary`
- `/testsend`

## Scripts

- `python3 print_feed_status.py`
- `python3 trello_check.py`
- `python3 trello_list_boards.py`
