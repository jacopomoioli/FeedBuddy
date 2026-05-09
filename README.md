# FeedBuddy

Feed manager bot for Telegram.

- polls RSS feeds
- sends new posts to Telegram
- can save posts to Trello to read later
- lets you add/remove feeds from Telegram (via polling, no need for webhooks)
- shows the last 10 sent posts on a minimal web page

## Getting Started 
In order to make this work, you need to copy `.env.example`:

```bash
cp .env.example .env
```

Edit it and set the right env vars as described below (`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are required)

- `TELEGRAM_BOT_TOKEN`: the token of the telegram bot. Get it from `@BotFather`
- `TELEGRAM_CHAT_ID`: the only Telegram chat allowed to talk to the bot, and also the destination chat for notifications. Get it from `@GetMyIDo_Bot`
- `WEB_HOST` & `WEB_PORT`: bind address & port for the web page, default is `127.0.0.1`, `8080`
- `TRELLO_KEY` & `TRELLO_TOKEN`: generate them by creating a new app [here](https://trello.com/power-ups/admin/)
- `TRELLO_LIST_ID`: id of the list in which the bot will add articles. You can set up `TRELLO_KEY` and `TRELLO_TOKEN`, then run the utility script `trello_list_boards.py` to find the right id 

After this, activate the virtual environment, install the dependencies (only feedparser for now) and run it:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 feedbuddy.py
```

## Telegram Commands

The following commands are available via Telegram:

- `/help`: list the available commands
- `/listfeeds`: list the loaded RSS feeds
- `/addfeed label | <url>`: add a new RSS feed with an optional label
- `/delfeed <url>`: delete an existing RSS feed
- `/exportfeeds`: download the current feed list as a `feeds.txt` attachment
- `/summary`: list every post published today
- `/testfeed <url>`: fetch and send the latest post of one feed
- `/testall`: fetch and send the latest post of every configured feed
- `/testsend`: send a test post

## CLI

```bash
python3 feedbuddy.py import <file>
```

Additive, skips existing URLs.

## Web Interface
The web interface runs on http://127.0.0.1:8080 (default configuration).

Here's a screenshot:

![Web Page screenshot](https://github.com/user-attachments/assets/6361c7fd-4563-4f45-8cee-22db17ba7fde)



## Why
There are a lot of different RSS solutions out there, i tried some of them but none felt right for me.

I also wanted to move important stuff on another channel (like trello) to avoid losing them. I was inspired by some [Cal Newport's videos](https://www.youtube.com/watch?v=FiLYCq0SfN4).

Also, most of the code was written using Claude Code & Codex. I wanted to build a small project to test the capabilities of coding agents. 
