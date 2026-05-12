# FeedBuddy

Telegram-based RSS reader that just makes sense.

- polls RSS feeds
- sends new posts to Telegram, with the full article as a readable PDF attachment
- tag posts with LLM-based auto-tagging
- save posts for later (and on a trello board) with a button in Telegram
- manage feeds & tags from Telegram
- minimal web page to list latest posts, "saved for later" posts & sources status

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
| `WEB_HOST` | no | `127.0.0.1` | Bind address for the web UI |
| `WEB_PORT` | no | `8080` | Bind port for the web UI |
| `STALE_DAYS` | no | `60` | Days before a feed is marked as stale in the web UI |
| `TRELLO_KEY` | no | | Trello API key |
| `TRELLO_TOKEN` | no | | Trello API token |
| `TRELLO_LIST_ID` | no | | Trello target list for saved cards. Run `trello_list_boards.py` to find it |
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

Each post sent by the bot has a "Save for later" button. Pressing it again removes it from the saved list.

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

## Web Interface

The web UI runs on `http://127.0.0.1:8080` by default. 
Shows
- latest posts
- saved for later posts
- feed list & status

![latest & saved posts](https://github.com/user-attachments/assets/ea66d481-cf04-4846-aaff-1508dbf00f8e)

![feed list](https://github.com/user-attachments/assets/0d5307f3-196f-4eea-914f-b39da88f5c88)

## Why

There are a lot of RSS solutions out there. I tried some but none felt right. I also wanted a way to save interesting articles somewhere without losing them, inspired by some [Cal Newport videos](https://www.youtube.com/watch?v=FiLYCq0SfN4).

Most of the code was written with Claude Code. I used this project to test what coding agents can actually do.
