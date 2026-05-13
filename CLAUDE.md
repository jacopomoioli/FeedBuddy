# FeedBuddy — CLAUDE.md

Personal RSS-to-Telegram bot. Polls feeds, sends new articles to a Telegram chat, and optionally saves them to Trello. Includes a minimal read-only web UI.

## What it is

Single-file Python script (`feedbuddy.py`, ~1500 lines). No framework. One external dependency (`feedparser`). Everything else is stdlib. SQLite for persistence. Runs as a foreground process. Optional Gemini API integration for AI-powered auto-tagging.

## Architecture

One process, one thread for the main loop, one daemon thread for the web server.

### Main loop (`main()`)

```
while True:
    if feed check is due:
        poll_feeds(db)          # fetch all feeds, send new items
    poll_telegram(db)           # long-poll Telegram for commands (blocks ~50s)
```

Feed checks run every `CHECK_EVERY = 300` seconds. Because `poll_telegram` blocks up to `TELEGRAM_TIMEOUT = 50` seconds, the actual interval between feed checks is `CHECK_EVERY + ~50s + feed fetch time`. Acceptable for a personal bot.

### SQLite schema (`feedbuddy.db`)

Five tables:

- **`feeds`** — registered feeds: `url` (PK), `label`, `title`, `added_at`
- **`items`** — every article ever seen: `feed_url`, `item_key` (unique within feed), `title`, `url`, `published`, `published_ts`, `summary`, `sent_chat_id`, `sent_message_id`, `trello_saved`, `trello_card_url`, `seen_at`
- **`tags`** — user-defined tags: `id`, `tag` (unique)
- **`item_tags`** — many-to-many join: `item_id`, `tag`
- **`meta`** — key/value store, used for `telegram_offset` (long-poll cursor)

An item is considered "sent" when `sent_message_id` is not null. New items are detected by checking `items` before sending — if the `(feed_url, item_key)` pair is absent, it's new.

### Web server

`ThreadingHTTPServer` on `WEB_HOST:WEB_PORT` (default `127.0.0.1:8080`). Serves one route (`/`): the last 10 sent articles (oldest first) and the feed list with the last seen date. Runs in a daemon thread; opens its own DB connection per request (WAL mode handles concurrent access safely).

## Key design decisions

**No webhooks.** Telegram long polling only. No need for a public URL or SSL cert. The bot is for personal use on a single chat.

**Single source of truth: the DB.** `sources.txt` is not used at runtime. It exists only as an input file for the one-shot CLI import command. All add/remove operations go through the DB via Telegram commands.

**Catch-up on first import.** When a feed is added (via `/addfeed` or CLI import), existing entries are marked as seen immediately so the user is not flooded with historical articles.

**Minimal dependencies.** `feedparser`, `requests`, `readability-lxml`, `weasyprint`, and `yt-dlp` are external. HTTP calls to Telegram/Gemini use `urllib`. HTML generation uses f-strings and `html.escape`. No template engine.

**AI auto-tagging via Gemini.** When `GEMINI_API_KEY` is set and at least one tag exists in the DB, each new item is tagged automatically by asking Gemini to pick from the available tag list. Tags are stored in `item_tags` and shown in the Telegram message and web UI.

**Article PDF attachment.** For non-YouTube feeds, each new item is fetched, extracted with `readability-lxml` (Firefox reader-mode algorithm), rendered to PDF via `weasyprint`, and sent as a `sendDocument` Telegram message with the formatted text as caption and the inline keyboard attached. If PDF generation fails for any reason the bot falls back to a plain text message.

**YouTube audio attachment.** For YouTube feeds, `yt-dlp` downloads the audio stream (preferring M4A ≤96 kbps for size, ~0.7 MB/min) and sends it via `sendAudio`, which renders as a native audio player in Telegram. Falls back to text-only if the download fails or the file exceeds Telegram's 50 MB bot limit (~75 min of audio).

## Configuration

Copy `.env.example` to `.env` and fill in the values. The `.env` loader is hand-rolled (`load_dotenv()`), supports `export KEY=VALUE` syntax and quoted values.

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | — | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | yes | — | Only chat allowed to talk to the bot, and destination for notifications |
| `WEB_HOST` | no | `127.0.0.1` | Web UI bind address |
| `WEB_PORT` | no | `8080` | Web UI bind port |
| `STALE_DAYS` | no | `60` | Days after which unsent items are pruned |
| `GEMINI_API_KEY` | no | — | Gemini API key for AI auto-tagging |
| `GEMINI_MODEL` | no | `gemini-2.5-flash` | Gemini model to use for tagging |

Each Telegram message gets a "Save for later" inline button. Pressing it pins the message in the chat and marks it in the DB. Pressing "Remove from later" unpins it.

## Running

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 feedbuddy.py
```

### CLI commands

```bash
# Normal run
python3 feedbuddy.py

# One-shot import from sources.txt (additive, skips existing URLs)
python3 feedbuddy.py import

# Import from a custom file
python3 feedbuddy.py import /path/to/feeds.txt
```

The import command prints each URL as added or skipped, then exits. It does not start the bot.

## Telegram commands

| Command | Description |
|---|---|
| `/help` | List commands |
| `/listfeeds` | Show all registered feeds (label + URL, HTML formatted, paginated) |
| `/addfeed label \| <url>` | Add a feed with an optional label |
| `/delfeed <url>` | Remove a feed |
| `/exportfeeds` | Send the current feed list as a `feeds.txt` attachment (sources.txt format) |
| `/summary` | List all articles seen today |
| `/listsaved` | List items saved to Trello |
| `/addtag <tag>` | Add a tag to the available tag list |
| `/deltag <tag>` | Remove a tag from the available tag list |
| `/listtags` | Show all defined tags |
| `/testfeed <url>` | Fetch and preview the latest entry of a feed (does not mark as sent) |
| `/testall` | Preview the latest entry of every registered feed |
| `/testsend` | Send a fake test article (marks it as sent in the DB) |

Only `TARGET_CHAT_ID` can interact with the bot. All other chats are silently ignored.

## Feed format (`sources.txt`)

Used only by the CLI import. One feed per line:

```
Label | https://example.com/feed.rss
https://example.com/no-label.rss
```

Lines starting with `#` are ignored.

## Code structure

All code is in `feedbuddy.py`. Functions are grouped loosely:

- **Setup**: `load_dotenv`, `env`, `open_db`, `get_meta`, `set_meta`
- **HTTP helpers**: `http_get`, `http_post_json`, `http_post_form`, `http_post_multipart`
- **Gemini / AI tagging**: `ask_gemini`, `auto_tag_item`, `save_item_tags`
- **PDF**: `_PDF_CSS`, `_is_youtube_feed`, `article_to_pdf_bytes`, `send_document`
- **YouTube audio**: `download_youtube_audio`, `send_audio`
- **Telegram**: `tg_api`, `send_message`, `answer_callback_query`, `edit_reply_markup`
- **Feed file**: `parse_source_line`, `read_sources_file`
- **YouTube**: `is_youtube_channel_url`, `resolve_youtube_feed`
- **Feed logic**: `feed_title`, `fetch_feed`, `normalize_entry`, `item_key`, `feed_display_name`, `ensure_feed`, `delete_feed`, `list_feeds`, `unsent_new_items`, `format_item`, `send_feed_item`, `poll_feeds`
- **Telegram command handlers**: `handle_addfeed`, `handle_delfeed`, `handle_listfeeds`, `handle_exportfeeds`, `handle_addtag`, `handle_deltag`, `handle_listsaved`, `handle_listtags`, `handle_testsend`, `send_preview_item`, `handle_testfeed`, `handle_testall`, `handle_summary`, `handle_callback_query`, `handle_message`
- **Web UI**: `render_index`, `last_items`, `feed_status_rows`, `safe_href`, `WebHandler`, `start_web`
- **Polling**: `poll_feeds`, `poll_telegram`
- **Migrations**: `backfill_published_ts`
- **Entry points**: `main`, `cmd_import`

## Style notes

- Minimal. No abstractions beyond what is needed.
- No comments unless the reason is non-obvious.
- Functions do one thing. No classes except `WebHandler` (required by stdlib).
- Error handling at the boundary (network calls, Telegram API). Internal logic is allowed to raise.
- All timestamps stored as UTC ISO strings. Parsed back with `parse_date()` which handles both ISO and RFC 2822 (email) format.
