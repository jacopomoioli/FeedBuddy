#!/usr/bin/env python3

import html as html_module
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import escape as html_escape
from html import escape

import feedparser
import requests
import yt_dlp
from readability import Document
from weasyprint import HTML as WeasyprintHTML, CSS as WeasyprintCSS


DB_PATH = "feedbuddy.db"
LOG_PATH = "feedbuddy.log"
USER_AGENT = "FeedBuddy/0.1"
CHECK_EVERY = 300
TELEGRAM_TIMEOUT = 50


def load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            if value and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ.setdefault(key, value)


def now():
    return datetime.now(timezone.utc).isoformat()


def log(*args):
    line = datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " " + " ".join(str(a) for a in args)
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def strip_html(text):
    """Strip HTML tags and decode entities, returning plain text."""
    from html.parser import HTMLParser
    class _P(HTMLParser):
        def __init__(self):
            super().__init__()
            self._parts = []
        def handle_data(self, data):
            self._parts.append(data)
    p = _P()
    p.feed(text)
    return " ".join(p._parts).strip()


def env(name, default=None, required=False):
    value = os.getenv(name, default)
    if required and not value:
        print("missing env:", name, file=sys.stderr)
        sys.exit(1)
    return value


load_dotenv()

BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", required=True)
TARGET_CHAT_ID = env("TELEGRAM_CHAT_ID", required=True)
_raw_subscribers = env("SUBSCRIBER_CHAT_IDS", "")
SUBSCRIBER_CHAT_IDS = [s.strip() for s in _raw_subscribers.split(",") if s.strip()]
OPENROUTER_API_KEY = env("OPENROUTER_API_KEY")
OPENROUTER_MODEL = env("OPENROUTER_MODEL", "google/gemini-2.5-flash")


def open_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("pragma journal_mode=wal")
    db.execute(
        """
        create table if not exists feeds (
            url text primary key,
            label text,
            title text,
            added_at text not null
        )
        """
    )
    cols = db.execute("pragma table_info(feeds)").fetchall()
    colnames = {row[1] for row in cols}
    if "label" not in colnames:
        db.execute("alter table feeds add column label text")
    item_cols = {row[1] for row in db.execute("pragma table_info(items)").fetchall()}
    if item_cols and "published_ts" not in item_cols:
        db.execute("alter table items add column published_ts text")
    if item_cols and "summary" not in item_cols:
        db.execute("alter table items add column summary text")
    if item_cols and "saved" not in item_cols:
        db.execute("alter table items add column saved integer not null default 0")
        if "trello_saved" in item_cols:
            db.execute("update items set saved = trello_saved")
    db.execute(
        """
        create table if not exists items (
            id integer primary key autoincrement,
            feed_url text not null,
            item_key text not null,
            title text,
            url text,
            published text,
            published_ts text,
            summary text,
            sent_chat_id text,
            sent_message_id integer,
            saved integer not null default 0,
            seen_at text not null,
            unique(feed_url, item_key)
        )
        """
    )
    db.execute(
        """
        create table if not exists tags (
            id integer primary key autoincrement,
            tag text not null unique
        )
        """
    )
    db.execute(
        """
        create table if not exists item_tags (
            item_id integer not null references items(id),
            tag text not null,
            primary key (item_id, tag)
        )
        """
    )
    db.execute(
        """
        create table if not exists meta (
            key text primary key,
            value text not null
        )
        """
    )
    db.execute(
        "create unique index if not exists idx_items_message on items(sent_chat_id, sent_message_id)"
    )
    db.commit()
    return db


def get_meta(db, key, default=None):
    row = db.execute("select value from meta where key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(db, key, value):
    db.execute(
        "insert into meta(key, value) values(?, ?) on conflict(key) do update set value = excluded.value",
        (key, str(value)),
    )
    db.commit()


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_post_json(url, data, timeout=30):
    payload = json.dumps(data).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def http_post_multipart(url, fields, filename, file_content, content_type="text/plain", field_name="document", timeout=30):
    boundary = b"----feedbuddy"
    parts = []
    for name, value in fields.items():
        parts.append(
            b"--" + boundary + b"\r\n"
            + f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            + str(value).encode() + b"\r\n"
        )
    parts.append(
        b"--" + boundary + b"\r\n"
        + f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode()
        + f"Content-Type: {content_type}\r\n\r\n".encode()
        + file_content + b"\r\n"
    )
    parts.append(b"--" + boundary + b"--\r\n")
    body = b"".join(parts)
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": f"multipart/form-data; boundary={boundary.decode()}",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def http_post_form(url, data, timeout=30):
    payload = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        if not body:
            return {}
        return json.loads(body.decode())


def ask_llm(prompt):
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    for attempt in range(3):
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                json={"model": OPENROUTER_MODEL, "messages": [{"role": "user", "content": prompt}]},
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except requests.HTTPError as e:
            if e.response.status_code in (502, 503) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"OpenRouter {e.response.status_code}: {e.response.text}")


def tg_api(method, data):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    reply = http_post_json(url, data, timeout=TELEGRAM_TIMEOUT + 10)
    if not reply.get("ok"):
        raise RuntimeError(f"telegram {method} failed: {reply}")
    return reply["result"]


def send_message(chat_id, text, reply_markup=None, parse_mode=None):
    data = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": False,
    }
    if reply_markup:
        data["reply_markup"] = reply_markup
    if parse_mode:
        data["parse_mode"] = parse_mode
    return tg_api("sendMessage", data)


def answer_callback_query(callback_id, text):
    try:
        tg_api("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})
    except Exception as e:
        log("callback answer failed:", e)



def parse_source_line(line):
    if " | " in line:
        label, url = line.split(" | ", 1)
        return {"label": label.strip(), "url": url.strip()}
    return {"label": None, "url": line.strip()}


def read_sources_file(path):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            rows.append(parse_source_line(line))
    return rows


def feed_title(parsed, url):
    title = parsed.feed.get("title")
    return title or url


def item_key(entry):
    for key in ("id", "guid", "link", "title"):
        value = entry.get(key)
        if value:
            return value.strip()
    return None


def normalize_entry(entry):
    key = item_key(entry)
    if not key:
        return None
    title = (entry.get("title") or "(no title)").strip()
    link = (entry.get("link") or "").strip()
    published = (
        entry.get("published")
        or entry.get("updated")
        or entry.get("created")
        or ""
    ).strip()
    tp = entry.get("published_parsed") or entry.get("updated_parsed")
    published_ts = datetime(*tp[:6], tzinfo=timezone.utc).isoformat() if tp else None
    raw_summary = entry.get("summary") or ""
    summary = strip_html(raw_summary)[:300].strip() if raw_summary else ""
    return {
        "key": key,
        "title": title,
        "link": link,
        "published": published,
        "published_ts": published_ts,
        "summary": summary,
    }


def fetch_feed(url):
    raw = http_get(url, timeout=30)
    parsed = feedparser.parse(raw)
    if getattr(parsed, "bozo", 0) and not parsed.entries:
        raise RuntimeError(f"invalid feed: {url}")
    entries = []
    for entry in parsed.entries:
        item = normalize_entry(entry)
        if item:
            entries.append(item)
    return feed_title(parsed, url), entries



def ensure_feed(db, url, label=None, catch_up=False):
    row = db.execute("select url, label from feeds where url = ?", (url,)).fetchone()
    if row:
        if label and label != row["label"]:
            db.execute("update feeds set label = ? where url = ?", (label, url))
            db.commit()
        return False
    log("adding feed:", url)
    title, entries = fetch_feed(url)
    db.execute(
        "insert into feeds(url, label, title, added_at) values(?, ?, ?, ?)",
        (url, label, title, now()),
    )
    if catch_up:
        for entry in entries:
            db.execute(
                """
                insert or ignore into items(feed_url, item_key, title, url, published, published_ts, summary, seen_at)
                values(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (url, entry["key"], entry["title"], entry["link"], entry["published"], entry.get("published_ts"), entry.get("summary"), now()),
            )
        log(f"caught up {len(entries)} existing items for:", url)
    db.commit()
    return True


def delete_feed(db, url):
    db.execute("delete from feeds where url = ?", (url,))
    db.commit()
    log("removed feed:", url)


def list_feeds(db):
    return db.execute("select url, label, title from feeds order by url").fetchall()


def feed_display_name(label, url):
    return label or url


def unsent_new_items(db, feed_url, entries):
    out = []
    for entry in entries:
        row = db.execute(
            "select id, sent_message_id from items where feed_url = ? and item_key = ?",
            (feed_url, entry["key"]),
        ).fetchone()
        if not row:
            out.append(entry)
    return out


def format_item(feed_name, entry, summary=None):
    parts = []
    if feed_name:
        parts.append(f"<i>{html_escape(feed_name)}</i>")
    parts.append(f"<b>{html_escape(entry['title'])}</b>")
    if summary:
        parts.append(html_escape(summary))
    parts.append(entry["link"])
    return "\n\n".join(parts)


_PDF_CSS = WeasyprintCSS(string="""
    @page { margin: 2.5cm 3cm; size: A4; }
    body { font-family: Georgia, serif; font-size: 11pt; line-height: 1.7; color: #1a1a1a; }
    h1 { font-size: 22pt; line-height: 1.3; margin: 0 0 0.3em 0; }
    h2 { font-size: 15pt; margin: 1.4em 0 0.4em; }
    h3 { font-size: 13pt; margin: 1.2em 0 0.3em; }
    h4, h5, h6 { font-size: 11pt; margin: 1em 0 0.3em; }
    p { margin: 0 0 0.9em 0; orphans: 3; widows: 3; }
    a { color: #1a1a1a; text-decoration: none; }
    blockquote { border-left: 3px solid #ccc; margin: 1em 0; padding: 0.3em 1em; color: #555; font-style: italic; }
    pre, code { font-family: monospace; font-size: 9pt; background: #f5f5f5; }
    pre { padding: 0.8em 1em; white-space: pre-wrap; word-break: break-all; }
    code { padding: 0.1em 0.3em; }
    img { max-width: 100%; height: auto; }
    ul, ol { margin: 0 0 0.9em 0; padding-left: 1.5em; }
    li { margin-bottom: 0.3em; }
    table { border-collapse: collapse; width: 100%; margin: 1em 0; font-size: 10pt; }
    th, td { border: 1px solid #ddd; padding: 0.4em 0.6em; }
    th { background: #f0f0f0; font-weight: bold; }
    .source { font-size: 9pt; color: #888; margin: 0 0 1.8em 0; font-family: monospace; word-break: break-all; }
""")


def _is_youtube_feed(feed_url):
    return feed_url and feed_url.startswith(_YT_FEED_BASE)


def article_to_pdf_bytes(url, title):
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; FeedBuddy/1.0)"}, timeout=20)
    r.raise_for_status()
    try:
        html = r.content.decode("utf-8")
    except UnicodeDecodeError:
        html = r.content.decode("latin-1")
    doc = Document(html)
    body_html = doc.summary(html_partial=True)
    plain_text = strip_html(body_html)
    full_html = (
        f'<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>'
        f'<h1>{html_escape(title)}</h1>'
        f'<p class="source">{html_escape(url)}</p>'
        f'{body_html}</body></html>'
    )
    pdf_bytes = WeasyprintHTML(string=full_html, base_url=url).write_pdf(stylesheets=[_PDF_CSS])
    return pdf_bytes, plain_text


PROMPT_PREFIX = "Article: {title}\n\n{excerpt}\n\n"
DEFAULT_INSTRUCTION = "Write a 2-3 sentence summary of the key points. Be concise and direct. No preamble."


def summarize_article(db, title, text):
    excerpt = text[:3000].strip()
    instruction = get_meta(db, "llm_instruction", DEFAULT_INSTRUCTION)
    prompt = PROMPT_PREFIX.format(title=title, excerpt=excerpt) + instruction
    return ask_llm(prompt)


def send_document(chat_id, pdf_bytes, filename, caption=None, parse_mode=None, reply_markup=None):
    fields = {"chat_id": str(chat_id)}
    if caption:
        fields["caption"] = caption[:1024]
    if parse_mode:
        fields["parse_mode"] = parse_mode
    if reply_markup:
        fields["reply_markup"] = json.dumps(reply_markup)
    tg_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    return http_post_multipart(tg_url, fields, filename, pdf_bytes, content_type="application/pdf")


class _YtLogger:
    def debug(self, msg):
        if msg.startswith("[debug]"):
            return
        log("yt-dlp:", msg)
    def info(self, msg):
        log("yt-dlp:", msg)
    def warning(self, msg):
        log("yt-dlp warning:", msg)
    def error(self, msg):
        log("yt-dlp error:", msg)


def download_youtube_audio(url):
    log("downloading audio:", url)
    with tempfile.TemporaryDirectory() as tmpdir:
        opts = {
            "format": "bestaudio[ext=m4a][abr<=96]/bestaudio[abr<=96]/bestaudio[ext=m4a]/bestaudio",
            "outtmpl": os.path.join(tmpdir, "%(title)s.%(ext)s"),
            "logger": _YtLogger(),
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info)
            size_mb = os.path.getsize(path) / 1_000_000
            log(f"audio ready: {os.path.basename(path)} ({size_mb:.1f} MB)")
            with open(path, "rb") as f:
                return f.read(), os.path.basename(path)


def send_audio(chat_id, audio_bytes, filename, caption=None, parse_mode=None, reply_markup=None):
    fields = {"chat_id": str(chat_id)}
    if caption:
        fields["caption"] = caption[:1024]
    if parse_mode:
        fields["parse_mode"] = parse_mode
    if reply_markup:
        fields["reply_markup"] = json.dumps(reply_markup)
    tg_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendAudio"
    return http_post_multipart(tg_url, fields, filename, audio_bytes, content_type="audio/mp4", field_name="audio")


def send_feed_item(db, feed_url, feed_name, entry):
    db.execute(
        """
        insert or ignore into items(feed_url, item_key, title, url, published, published_ts, summary, seen_at)
        values(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            feed_url,
            entry["key"],
            entry["title"],
            entry["link"],
            entry["published"],
            entry.get("published_ts"),
            entry.get("summary"),
            now(),
        ),
    )
    row = find_item_by_key(db, feed_url, entry["key"])
    item_id = row["id"]
    markup = {
        "inline_keyboard": [
            [{"text": "Save for later", "callback_data": f"save:{item_id}"}]
        ]
    }
    all_chats = [TARGET_CHAT_ID] + SUBSCRIBER_CHAT_IDS
    attachment = None
    summary = entry.get("summary") or ""
    if entry.get("link"):
        if _is_youtube_feed(feed_url):
            try:
                audio_bytes, filename = download_youtube_audio(entry["link"])
                attachment = ("audio", audio_bytes, filename)
            except Exception as e:
                log("audio download failed, sending text only:", entry["title"], e)
        else:
            try:
                log("fetching article:", entry["link"])
                pdf_bytes, plain_text = article_to_pdf_bytes(entry["link"], entry["title"])
                if OPENROUTER_API_KEY and plain_text:
                    try:
                        log("summarizing:", entry["title"])
                        summary = summarize_article(db, entry["title"], plain_text)
                    except Exception as e:
                        log("summarize failed:", entry["title"], e)
                safe_title = re.sub(r"[^\w\s-]", "", entry["title"])[:60].strip()
                filename = re.sub(r"\s+", "-", safe_title).lower() + ".pdf"
                attachment = ("pdf", pdf_bytes, filename)
            except Exception as e:
                log("pdf failed, sending text only:", entry["title"], e)
    text = format_item(feed_name, entry, summary=summary)
    msg = None
    for chat_id in all_chats:
        sub_markup = markup if str(chat_id) == str(TARGET_CHAT_ID) else None
        try:
            if attachment:
                kind, data, fname = attachment
                if kind == "audio":
                    result = send_audio(chat_id, data, fname, caption=text, parse_mode="HTML", reply_markup=sub_markup)
                else:
                    result = send_document(chat_id, data, fname, caption=text, parse_mode="HTML", reply_markup=sub_markup)
                sent = result["result"]
            else:
                sent = send_message(chat_id, text, sub_markup, parse_mode="HTML")
        except Exception as e:
            log("send failed for chat", chat_id, ":", entry["title"], e)
            continue
        if str(chat_id) == str(TARGET_CHAT_ID):
            msg = sent
    if msg is None:
        msg = send_message(TARGET_CHAT_ID, text, markup, parse_mode="HTML")
    db.execute(
        """
        update items
        set sent_chat_id = ?, sent_message_id = ?, title = ?, url = ?, published = ?, summary = ?
        where feed_url = ? and item_key = ?
        """,
        (
            str(msg["chat"]["id"]),
            msg["message_id"],
            entry["title"],
            entry["link"],
            entry["published"],
            entry.get("summary"),
            feed_url, entry["key"],
        ),
    )
    db.commit()


def poll_feeds(db):
    feeds = db.execute("select url, label, title from feeds order by url").fetchall()
    log(f"checking {len(feeds)} feed(s)")
    for feed in feeds:
        try:
            title, entries = fetch_feed(feed["url"])
        except Exception as e:
            log("feed fetch failed:", feed["url"], e)
            continue
        db.execute("update feeds set title = ? where url = ?", (title, feed["url"]))
        db.commit()
        new_items = unsent_new_items(db, feed["url"], entries)
        if new_items:
            log(f"{len(new_items)} new item(s) in:", feed_display_name(feed["label"], feed["url"]))
        for entry in new_items:
            try:
                send_feed_item(
                    db,
                    feed["url"],
                    feed_display_name(feed["label"], feed["url"]),
                    entry,
                )
                log("sent:", entry["title"])
                time.sleep(1)
            except Exception as e:
                log("send failed:", entry["title"], e)


def chat_allowed(chat_id):
    return str(chat_id) == str(TARGET_CHAT_ID)


def parse_command(text):
    parts = (text or "").strip().split(None, 1)
    cmd = parts[0] if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""
    return cmd, arg


def handle_getlog(chat_id):
    if not os.path.exists(LOG_PATH):
        send_message(chat_id, "no log file yet")
        return
    with open(LOG_PATH, "rb") as f:
        data = f.read()
    tg_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    http_post_multipart(tg_url, {"chat_id": str(chat_id)}, "feedbuddy.log", data, content_type="text/plain")


def send_help(chat_id):
    text = "\n".join(
        [
            "/help",
            "/listfeeds",
            "/addfeed label | <url>",
            "/delfeed <url>",
            "/exportfeeds",
            "/listsaved",
            "/summary",
            "/getprompt",
            "/setprompt <prompt>",
            "/testfeed <url>",
            "/testall",
            "/testsend",
            "/getlog",
        ]
    )
    send_message(chat_id, text)


_YT_FEED_BASE = "https://www.youtube.com/feeds/videos.xml"
_YT_SCRAPE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def is_youtube_channel_url(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc not in ("www.youtube.com", "youtube.com"):
        return False
    if parsed.path.startswith("/feeds/"):
        return False
    return bool(re.match(r"^(/(@[\w.-]+)|/channel/[\w-]+|/user/[\w.-]+)$", parsed.path))


def resolve_youtube_feed(url):
    parsed = urllib.parse.urlparse(url)
    # /channel/UCxxxxx — channel ID already present
    m = re.match(r"^/channel/(UC[\w-]+)$", parsed.path)
    if m:
        return f"{_YT_FEED_BASE}?channel_id={m.group(1)}"
    # /@handle or /user/name — scrape the page for the RSS <link> tag
    req = urllib.request.Request(url, headers={"User-Agent": _YT_SCRAPE_UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        html = r.read().decode("utf-8", errors="replace")
    m = re.search(
        r'href="(https://www\.youtube\.com/feeds/videos\.xml\?channel_id=[^"]+)"',
        html,
    )
    if not m:
        raise RuntimeError("RSS feed link not found in YouTube page")
    return m.group(1)


def handle_addfeed(db, chat_id, url):
    row = parse_source_line(url)
    label = row["label"]
    url = row["url"]
    if not url.startswith(("http://", "https://")):
        send_message(chat_id, "bad url")
        return
    resolved_yt_url = None
    if is_youtube_channel_url(url):
        try:
            resolved_yt_url = resolve_youtube_feed(url)
            log("resolved youtube url:", url, "->", resolved_yt_url)
            url = resolved_yt_url
        except Exception as e:
            send_message(chat_id, f"could not resolve youtube feed: {e}")
            return
    try:
        created = ensure_feed(db, url, label=label, catch_up=True)
        if not created:
            send_message(chat_id, "feed already exists")
            return
        if resolved_yt_url:
            send_message(chat_id, f"YouTube feed added\n\n<code>{html_escape(resolved_yt_url)}</code>", parse_mode="HTML")
        else:
            send_message(chat_id, "feed added")
    except Exception as e:
        send_message(chat_id, f"cannot add feed: {e}")


def handle_delfeed(db, chat_id, url):
    if not url:
        send_message(chat_id, "missing url")
        return
    url = parse_source_line(url)["url"]
    row = db.execute("select url from feeds where url = ?", (url,)).fetchone()
    if not row:
        send_message(chat_id, "feed not found")
        return
    delete_feed(db, url)
    send_message(chat_id, "feed removed")


def handle_listfeeds(db, chat_id):
    feeds = list_feeds(db)
    if not feeds:
        send_message(chat_id, "no feeds")
        return
    lines = []
    for row in feeds:
        name = feed_display_name(row["label"], row["url"])
        title = html_escape(name)
        url = html_escape(row["url"])
        lines.append(f"<b>{title}</b>")
        lines.append(f"<code>{url}</code>")
        lines.append("")
    chunk = []
    size = 0
    for line in lines:
        if size + len(line) + 1 > 3500:
            send_message(chat_id, "\n".join(chunk), parse_mode="HTML")
            chunk = []
            size = 0
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        send_message(chat_id, "\n".join(chunk), parse_mode="HTML")


def handle_exportfeeds(db, chat_id):
    feeds = list_feeds(db)
    if not feeds:
        send_message(chat_id, "no feeds")
        return
    lines = []
    for row in feeds:
        if row["label"]:
            lines.append(f"{row['label']} | {row['url']}")
        else:
            lines.append(row["url"])
    content = "\n".join(lines).encode()
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    http_post_multipart(url, {"chat_id": chat_id}, "feeds.txt", content)



def handle_listsaved(db, chat_id):
    rows = db.execute(
        """
        select i.title, i.url, f.label, f.url as feed_url
        from items i
        left join feeds f on f.url = i.feed_url
        where i.saved = 1
        order by i.seen_at desc
        """
    ).fetchall()
    if not rows:
        send_message(chat_id, "no saved items")
        return
    lines = []
    for row in rows:
        title = html_escape(row["title"] or "(no title)")
        url = html_escape(row["url"] or "")
        feed_name = html_escape(feed_display_name(row["label"], row["feed_url"] or ""))
        lines.append(f"<b><a href=\"{url}\">{title}</a></b>")
        lines.append(f"<i>{feed_name}</i>")
        lines.append("")
    chunk = []
    size = 0
    for line in lines:
        if size + len(line) + 1 > 3500:
            send_message(chat_id, "\n".join(chunk), parse_mode="HTML")
            chunk = []
            size = 0
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        send_message(chat_id, "\n".join(chunk), parse_mode="HTML")


def handle_getprompt(db, chat_id):
    instruction = get_meta(db, "llm_instruction", DEFAULT_INSTRUCTION)
    send_message(chat_id, f"<pre>{html_escape(instruction)}</pre>", parse_mode="HTML")


def handle_setprompt(db, chat_id, text):
    if not text:
        send_message(chat_id, "usage: /setprompt <instruction>\n\nsets the instruction appended after the article excerpt")
        return
    set_meta(db, "llm_instruction", text)
    log("llm instruction updated")
    send_message(chat_id, "prompt instruction updated")


def handle_testsend(db, chat_id):
    entry = {
        "key": f"test:{int(time.time())}",
        "title": "FeedBuddy test post",
        "link": "https://example.com/feedbuddy-test",
        "published": now(),
    }
    try:
        send_feed_item(db, "feedbuddy:test", "FeedBuddy", entry)
        send_message(chat_id, "test sent")
    except Exception as e:
        send_message(chat_id, f"test failed: {e}")


def send_preview_item(chat_id, feed_name, entry):
    send_message(chat_id, format_item(feed_name, entry), parse_mode="HTML")


def handle_testfeed(db, chat_id, url):
    row = parse_source_line(url)
    url = row["url"]
    if not url.startswith(("http://", "https://")):
        send_message(chat_id, "bad url")
        return
    feed = db.execute(
        "select label, url from feeds where url = ?",
        (url,),
    ).fetchone()
    feed_name = feed_display_name(feed["label"], feed["url"]) if feed else url
    try:
        _, entries = fetch_feed(url)
    except Exception as e:
        send_message(chat_id, f"test failed: {e}")
        return
    if not entries:
        send_message(chat_id, "no entries")
        return
    send_preview_item(chat_id, feed_name, entries[0])


def handle_testall(db, chat_id):
    feeds = list_feeds(db)
    if not feeds:
        send_message(chat_id, "no feeds")
        return
    for feed in feeds:
        try:
            _, entries = fetch_feed(feed["url"])
        except Exception as e:
            send_message(chat_id, f"{feed_display_name(feed['label'], feed['url'])}\n\nerror: {e}")
            continue
        if not entries:
            send_message(chat_id, f"{feed_display_name(feed['label'], feed['url'])}\n\nno entries")
            continue
        entry = entries[0]
        feed_name = feed_display_name(feed["label"], feed["url"])
        text = format_item(feed_name, entry)
        send_message(chat_id, text, parse_mode="HTML")
        time.sleep(1)


def parse_date(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


def fmt_time(value):
    dt = parse_date(value)
    if not dt:
        return value or ""
    return dt.strftime("%Y-%m-%d %H:%M")


def summary_rows_for_today(db):
    today = datetime.now().astimezone().date()
    rows = db.execute(
        """
        select title, url, published, seen_at, feed_url
        from items
        where sent_message_id is not null
        order by seen_at asc
        """
    ).fetchall()
    out = []
    for row in rows:
        dt = parse_date(row["published"]) or parse_date(row["seen_at"])
        if dt and dt.date() == today:
            out.append(row)
    return out


def handle_summary(db, chat_id):
    rows = summary_rows_for_today(db)
    if not rows:
        send_message(chat_id, "no posts today")
        return
    lines = ["posts today:", ""]
    for row in rows:
        lines.append(row["title"] or "(no title)")
        if row["url"]:
            lines.append(row["url"])
        if row["feed_url"]:
            feed = db.execute(
                "select label, url from feeds where url = ?",
                (row["feed_url"],),
            ).fetchone()
            name = row["feed_url"]
            if feed:
                name = feed_display_name(feed["label"], feed["url"])
            lines.append(f"feed: {name}")
        lines.append("")
    text = "\n".join(lines).strip()
    if len(text) > 4000:
        text = text[:3990].rstrip() + "\n..."
    send_message(chat_id, text)


def find_item_by_message(db, chat_id, message_id):
    return db.execute(
        "select id from items where sent_chat_id = ? and sent_message_id = ?",
        (str(chat_id), message_id),
    ).fetchone()


def find_item_by_key(db, feed_url, key):
    return db.execute(
        "select id from items where feed_url = ? and item_key = ?",
        (feed_url, key),
    ).fetchone()



def pin_message(chat_id, message_id):
    try:
        tg_api("pinChatMessage", {"chat_id": chat_id, "message_id": message_id, "disable_notification": True})
    except Exception as e:
        log("pinChatMessage failed:", e)


def unpin_message(chat_id, message_id):
    try:
        tg_api("unpinChatMessage", {"chat_id": chat_id, "message_id": message_id})
    except Exception as e:
        log("unpinChatMessage failed:", e)


def edit_reply_markup(chat_id, message_id, markup):
    try:
        tg_api("editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": markup,
        })
    except Exception as e:
        log("editMessageReplyMarkup failed:", e)


def handle_callback_query(db, update):
    cb = update["callback_query"]
    chat_id = cb.get("message", {}).get("chat", {}).get("id")
    message_id = cb.get("message", {}).get("message_id")
    if not chat_allowed(chat_id):
        return
    data = cb.get("data") or ""

    if data.startswith("save:"):
        item_id = data.split(":", 1)[1]
        row = db.execute("select id, title from items where id = ?", (item_id,)).fetchone()
        if not row:
            answer_callback_query(cb["id"], "item not found")
            return
        try:
            db.execute("update items set saved = 1 where id = ?", (row["id"],))
            db.commit()
            pin_message(chat_id, message_id)
            answer_callback_query(cb["id"], "saved")
            edit_reply_markup(chat_id, message_id, {
                "inline_keyboard": [[{"text": "Remove from later", "callback_data": f"unsave:{row['id']}"}]]
            })
            log("saved for later:", row["title"])
        except Exception as e:
            log("save failed:", e)
            answer_callback_query(cb["id"], "save failed")

    elif data.startswith("unsave:"):
        item_id = data.split(":", 1)[1]
        row = db.execute("select id, title from items where id = ?", (item_id,)).fetchone()
        if not row:
            answer_callback_query(cb["id"], "item not found")
            return
        db.execute("update items set saved = 0 where id = ?", (row["id"],))
        db.commit()
        unpin_message(chat_id, message_id)
        answer_callback_query(cb["id"], "removed")
        log("removed from later:", row["title"])
        edit_reply_markup(chat_id, message_id, {
            "inline_keyboard": [[{"text": "Save for later", "callback_data": f"save:{row['id']}"}]]
        })

    else:
        answer_callback_query(cb["id"], "unknown action")


def handle_message(db, update):
    msg = update["message"]
    chat_id = msg["chat"]["id"]
    username = msg.get("from", {}).get("username") or msg.get("from", {}).get("first_name") or str(chat_id)
    text = msg.get("text") or ""
    if text:
        log(f"message from {username} ({chat_id}): {text}")
    if not text.startswith("/"):
        return
    if not chat_allowed(chat_id):
        return
    cmd, arg = parse_command(text)
    cmd = cmd.split("@", 1)[0]
    log(f"command: {cmd}" + (f" {arg}" if arg else ""))
    if cmd == "/help" or cmd == "/start":
        send_help(chat_id)
    elif cmd == "/listfeeds":
        handle_listfeeds(db, chat_id)
    elif cmd == "/addfeed":
        handle_addfeed(db, chat_id, arg)
    elif cmd == "/delfeed":
        handle_delfeed(db, chat_id, arg)
    elif cmd == "/exportfeeds":
        handle_exportfeeds(db, chat_id)
    elif cmd == "/listsaved":
        handle_listsaved(db, chat_id)
    elif cmd == "/summary":
        handle_summary(db, chat_id)
    elif cmd == "/testfeed":
        handle_testfeed(db, chat_id, arg)
    elif cmd == "/testall":
        handle_testall(db, chat_id)
    elif cmd == "/testsend":
        handle_testsend(db, chat_id)
    elif cmd == "/getprompt":
        handle_getprompt(db, chat_id)
    elif cmd == "/setprompt":
        handle_setprompt(db, chat_id, arg)
    elif cmd == "/getlog":
        handle_getlog(chat_id)


def poll_telegram(db):
    offset = int(get_meta(db, "telegram_offset", "0"))
    try:
        updates = tg_api(
            "getUpdates",
            {
                "offset": offset,
                "timeout": TELEGRAM_TIMEOUT,
                "allowed_updates": [
                    "message",
                    "callback_query",
                ],
            },
        )
    except Exception as e:
        log("telegram poll failed:", e)
        time.sleep(3)
        return
    for update in updates:
        set_meta(db, "telegram_offset", update["update_id"] + 1)
        try:
            if "message" in update:
                handle_message(db, update)
            elif "callback_query" in update:
                handle_callback_query(db, update)
        except Exception as e:
            log("update handling failed:", e)


def backfill_published_ts(db):
    rows = db.execute(
        "select id, published from items where published_ts is null and published != ''"
    ).fetchall()
    for row in rows:
        dt = parse_date(row["published"])
        if dt:
            db.execute(
                "update items set published_ts = ? where id = ?",
                (dt.astimezone(timezone.utc).isoformat(), row["id"]),
            )
    if rows:
        db.commit()
        log(f"backfilled published_ts for {len(rows)} items")


def register_commands():
    tg_api("setMyCommands", {"commands": [
        {"command": "help",        "description": "List available commands"},
        {"command": "listfeeds",   "description": "List all registered feeds"},
        {"command": "addfeed",     "description": "Add a feed: label | <url>"},
        {"command": "delfeed",     "description": "Remove a feed: <url>"},
        {"command": "exportfeeds", "description": "Download feed list as feeds.txt"},
        {"command": "listsaved",   "description": "List posts saved for later"},
        {"command": "summary",     "description": "List every post seen today"},
        {"command": "getprompt",   "description": "Show the current LLM instruction"},
        {"command": "setprompt",   "description": "Edit the LLM instruction"},
        {"command": "getlog",      "description": "Download the bot log file"},
        {"command": "testfeed",    "description": "Preview latest post of a feed: <url>"},
        {"command": "testall",     "description": "Preview latest post of every feed"},
        {"command": "testsend",    "description": "Send a test post"},
    ]})


def main():
    db = open_db()
    backfill_published_ts(db)
    register_commands()
    next_feed_check = 0
    first_run = True
    log("starting up")
    while True:
        if time.time() >= next_feed_check:
            poll_feeds(db)
            next_feed_check = time.time() + CHECK_EVERY
            if first_run:
                first_run = False
                log("ready")
        poll_telegram(db)


def cmd_import(path):
    sources = read_sources_file(path)
    if not sources:
        print("no sources found in", path)
        sys.exit(1)
    db = open_db()
    added = 0
    for row in sources:
        try:
            created = ensure_feed(db, row["url"], label=row["label"], catch_up=True)
        except Exception as e:
            log("skipped (cannot fetch):", row["url"], e)
            continue
        if created:
            added += 1
            log("added:", row["url"])
        else:
            log("skipped (exists):", row["url"])
    print(f"done: {added} added, {len(sources) - added} skipped")
    db.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "import":
        if len(sys.argv) < 3:
            print("usage: feedbuddy.py import <file>")
            sys.exit(1)
        cmd_import(sys.argv[2])
    else:
        main()
