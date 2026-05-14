# FeedBuddy

Telegram-based RSS reader that just makes sense.

- polls RSS feeds
- sends new posts to Telegram, with the full article as a readable PDF attachment (or audio for YouTube feeds)
- summarizes each article with an LLM
- save posts for later with a button in Telegram (pins the message in the chat)
- manage feeds from Telegram

## Demo

| Incoming Post | Post Saved for Later |
|-|-|
|![Incoming Post](https://github.com/user-attachments/assets/3374545c-5b34-46bd-a34c-d3205dedc7b9) | ![Post Saved for Later](https://github.com/user-attachments/assets/57d9650c-32e3-447b-8576-8834af22c7ef) | 

| Loaded Feed Listing | Help Command |
|-|-|
|![Loaded Feed Listing](https://github.com/user-attachments/assets/210b5a2f-1f09-4ce4-9b94-b391350ea148) | ![Help Command](https://github.com/user-attachments/assets/16815034-02e6-47e9-9fe4-b0b792c10176) |


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
| `SUBSCRIBER_CHAT_IDS` | no | | Comma-separated chat IDs that receive posts but cannot run commands |
| `OPENROUTER_API_KEY` | no | | OpenRouter API key |
| `OPENROUTER_MODEL` | no | `google/gemini-2.5-flash` | Model to use via OpenRouter |

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
| `/summary` | List every post seen today |
| `/getprompt` | Show the current LLM summarization instruction |
| `/setprompt <text>` | Edit the LLM summarization instruction |
| `/getlog` | Download the bot log file |
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
