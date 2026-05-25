# FeedBuddy

Telegram-based RSS reader that just makes sense.

- polls RSS feeds
- sends new posts to Telegram, with the full article as a readable PDF attachment (or audio for YouTube feeds)
- summarizes each article with an LLM (through openrouter), and gives them a relevance score based on your interests
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
Then, run feedbuddy with Docker (recommended):

```bash
touch feedbuddy.db feedbuddy.log   # ensure these exist as files before mounting
docker compose up -d
docker compose logs -f             # tail logs
```

Or directly with Python:

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
| `/addgoated [title \| ] <url>` | Manually add a URL to the Goated list |
| `/listgoated` | List goated posts |
| `/stats` | Reading stats: totals, read rate, top feeds |
| `/getprompt` | Show the current LLM summarization instruction |
| `/setprompt <text>` | Edit the LLM summarization instruction |
| `/setinterests <text>` | Set your interest profile for relevance scoring |
| `/getinterests` | Show your interest profile and silence threshold |
| `/setthreshold <1-10>` | Set the score below which posts are delivered silently |
| `/getlog` | Download the bot log file |
| `/testfeed <url>` | Fetch and preview the latest post of a feed |

Each post has three inline buttons: "Mark as Read", "Save for later" (which pins the message), and "Add to Goated".


## Why

There are a lot of RSS solutions out there. I tried some but none felt right. I also wanted a way to save interesting articles somewhere without losing them, inspired by some [Cal Newport videos](https://www.youtube.com/watch?v=FiLYCq0SfN4).

Most of the code was written with Claude Code. I used this project to test what coding agents can actually do.
