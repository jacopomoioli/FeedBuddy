# FeedBuddy

Telegram-based RSS reader that just makes sense.

- polls RSS feeds
- sends new posts to Telegram, with the full article as a readable PDF attachment (or audio for YouTube feeds)
- tag posts with LLM-based auto-tagging
- save posts for later with a button in Telegram (pins the message in the chat)
- manage feeds & tags from Telegram

## Getting Started

Copy `.env.example`:

```bash
cp .env.example .env
```

Edit it and set the right values. `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are required.

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | | Bot token from `@BotFather` |
| `TELEGRAM_CHAT_ID` | yes | | Only chat allowed to talk to the bot, and destination for notifications. Get it from `@GetMyIDo_Bot` |
| `GEMINI_API_KEY` | no | | Enables LLM-based auto-tagging |
| `GEMINI_MODEL` | no | `gemini-2.5-flash` | Model used for tagging |

Then run it:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 feedbuddy.py
```

## Telegram Commands

| Command | Description |
|---|---|
| `/help` | List available commands |
| `/listfeeds` | List all registered feeds |
| `/addfeed label \| <url>` | Add a feed with an optional label. YouTube channel URLs (`@handle`, `/channel/...`) are resolved automatically |
| `/delfeed <url>` | Remove a feed |
| `/exportfeeds` | Download the current feed list as `feeds.txt` |
| `/listsaved` | List all posts saved for later |
| `/addtag <tag>` | Add a tag for auto-tagging |
| `/deltag <tag>` | Remove a tag |
| `/listtags` | List all tags |
| `/summary` | List every post seen today |
| `/testfeed <url>` | Fetch and preview the latest post of a feed |
| `/testall` | Fetch and preview the latest post of every feed |
| `/testsend` | Send a test post |

Each post sent by the bot has a "Save for later" button. Pressing it pins the message in the chat. Pressing "Remove from later" unpins it.

## CLI

```bash
# import feeds from a file (additive, skips existing URLs)
python3 feedbuddy.py import <file>
```

The file format is one feed per line:

```
Label | https://example.com/feed.rss
https://example.com/no-label.rss
```

Lines starting with `#` are ignored.


## Why

There are a lot of RSS solutions out there. I tried some but none felt right. I also wanted a way to save interesting articles somewhere without losing them, inspired by some [Cal Newport videos](https://www.youtube.com/watch?v=FiLYCq0SfN4).

Most of the code was written with Claude Code. I used this project to test what coding agents can actually do.
