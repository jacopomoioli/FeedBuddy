#!/usr/bin/env python3

import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import escape as html_escape
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import feedparser


DB_PATH = "feedbuddy.db"
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
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *args, flush=True)


def env(name, default=None, required=False):
    value = os.getenv(name, default)
    if required and not value:
        print("missing env:", name, file=sys.stderr)
        sys.exit(1)
    return value


load_dotenv()

BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", required=True)
TARGET_CHAT_ID = env("TELEGRAM_CHAT_ID", required=True)
TRELLO_KEY = env("TRELLO_KEY")
TRELLO_TOKEN = env("TRELLO_TOKEN")
TRELLO_LIST_ID = env("TRELLO_LIST_ID")
WEB_HOST = env("WEB_HOST", "127.0.0.1")
WEB_PORT = int(env("WEB_PORT", "8080"))
STALE_DAYS = int(env("STALE_DAYS", "60"))
GEMINI_API_KEY = env("GEMINI_API_KEY")
GEMINI_MODEL = env("GEMINI_MODEL", "gemini-2.5-flash")

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def trello_enabled():
    return bool(TRELLO_KEY and TRELLO_TOKEN and TRELLO_LIST_ID)


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
    if "published_ts" not in item_cols:
        db.execute("alter table items add column published_ts text")
    db.execute(
        """
        create table if not exists items (
            id integer primary key autoincrement,
            feed_url text not null,
            item_key text not null,
            title text,
            url text,
            published text,
            sent_chat_id text,
            sent_message_id integer,
            trello_saved integer not null default 0,
            trello_card_url text,
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


def http_post_multipart(url, fields, filename, file_content, timeout=30):
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
        + f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'.encode()
        + b"Content-Type: text/plain\r\n\r\n"
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


def ask_gemini(model, prompt):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set")
    url = GEMINI_URL.format(model=model) + "?key=" + GEMINI_API_KEY
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0},
    }
    resp = http_post_json(url, payload, timeout=30)
    return resp["candidates"][0]["content"]["parts"][0]["text"].strip()


def auto_tag_item(db, feed_url, entry):
    available_tags = [row["tag"] for row in db.execute("select tag from tags order by tag").fetchall()]
    if not available_tags:
        return []
    feed = db.execute("select label, url from feeds where url = ?", (feed_url,)).fetchone()
    feed_name = feed_display_name(feed["label"], feed["url"]) if feed else (feed_url or "")
    post_info = "\n".join([
        f"title: {entry.get('title') or ''}",
        f"url: {entry.get('link') or entry.get('url') or ''}",
        f"source: {feed_name}",
    ])
    prompt = (
        f"Post:\n{post_info}\n\n"
        f"Available tags: {' '.join(available_tags)}\n\n"
        "Based on the information provided about the post, and the list of available tags, "
        "return a space separated string containing only the appropriate tags. "
        "Use only tags from the list above. Return only the tags, nothing else. "
        "If no tag fits, return an empty string."
    )
    raw = ask_gemini(GEMINI_MODEL, prompt)
    matched = []
    for word in raw.lower().split():
        word = word.strip().strip("#")
        if word in available_tags and f"#{word}" not in matched:
            matched.append(f"#{word}")
    return matched


def save_item_tags(db, item_id, tags):
    for tag in tags:
        db.execute(
            "insert or ignore into item_tags(item_id, tag) values(?, ?)",
            (item_id, tag.lstrip("#")),
        )
    db.commit()


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
    return {
        "key": key,
        "title": title,
        "link": link,
        "published": published,
        "published_ts": published_ts,
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
    title = url
    try:
        title, entries = fetch_feed(url)
    except Exception as e:
        log("cannot fetch feed during import:", url, e)
        entries = []
    db.execute(
        "insert into feeds(url, label, title, added_at) values(?, ?, ?, ?)",
        (url, label, title, now()),
    )
    if catch_up:
        for entry in entries:
            db.execute(
                """
                insert or ignore into items(feed_url, item_key, title, url, published, published_ts, seen_at)
                values(?, ?, ?, ?, ?, ?, ?)
                """,
                (url, entry["key"], entry["title"], entry["link"], entry["published"], entry.get("published_ts"), now()),
            )
    db.commit()
    return True


def delete_feed(db, url):
    db.execute("delete from feeds where url = ?", (url,))
    db.commit()


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


def format_item(feed_name, entry):
    if feed_name:
        return f"""{feed_name}: {entry["title"]}

{entry["link"]}"""
    return f"""{entry["title"]}

{entry["link"]}"""


def send_feed_item(db, feed_url, feed_name, entry):
    db.execute(
        """
        insert or ignore into items(feed_url, item_key, title, url, published, published_ts, seen_at)
        values(?, ?, ?, ?, ?, ?, ?)
        """,
        (
            feed_url,
            entry["key"],
            entry["title"],
            entry["link"],
            entry["published"],
            entry.get("published_ts"),
            now(),
        ),
    )
    row = find_item_by_key(db, feed_url, entry["key"])
    item_id = row["id"]
    tags = []
    if GEMINI_API_KEY:
        try:
            tags = auto_tag_item(db, feed_url, entry)
        except Exception as e:
            log("auto-tag failed:", entry["title"], e)
    text = format_item(feed_name, entry)
    if tags:
        text += "\n\n" + " ".join(tags)
    markup = None
    if trello_enabled():
        markup = {
            "inline_keyboard": [
                [{"text": "Save for later", "callback_data": f"save:{item_id}"}]
            ]
        }
    msg = send_message(TARGET_CHAT_ID, text, markup)
    db.execute(
        """
        update items
        set sent_chat_id = ?, sent_message_id = ?, title = ?, url = ?, published = ?
        where feed_url = ? and item_key = ?
        """,
        (
            str(msg["chat"]["id"]),
            msg["message_id"],
            entry["title"],
            entry["link"],
            entry["published"],
            feed_url, entry["key"],
        ),
    )
    db.commit()
    if tags:
        save_item_tags(db, item_id, tags)
        log("tagged:", entry["title"], " ".join(tags))


def poll_feeds(db):
    feeds = db.execute("select url, label, title from feeds order by url").fetchall()
    for feed in feeds:
        try:
            title, entries = fetch_feed(feed["url"])
        except Exception as e:
            log("feed fetch failed:", feed["url"], e)
            continue
        db.execute("update feeds set title = ? where url = ?", (title, feed["url"]))
        db.commit()
        new_items = unsent_new_items(db, feed["url"], entries)
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


def send_help(chat_id):
    text = "\n".join(
        [
            "/help",
            "/listfeeds",
            "/addfeed label | <url>",
            "/delfeed <url>",
            "/exportfeeds",
            "/listsaved",
            "/addtag <tag>",
            "/deltag <tag>",
            "/listtags",
            "/summary",
            "/testfeed <url>",
            "/testall",
            "/testsend",
        ]
    )
    send_message(chat_id, text)


def handle_addfeed(db, chat_id, url):
    row = parse_source_line(url)
    label = row["label"]
    url = row["url"]
    if not url.startswith(("http://", "https://")):
        send_message(chat_id, "bad url")
        return
    try:
        created = ensure_feed(db, url, label=label, catch_up=True)
        if not created:
            send_message(chat_id, "feed already exists")
            return
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


def handle_addtag(db, chat_id, tag):
    if not tag:
        send_message(chat_id, "usage: /addtag <tag>")
        return
    tag = tag.strip().lower()
    existing = db.execute("select id from tags where tag = ?", (tag,)).fetchone()
    if existing:
        send_message(chat_id, "tag already exists")
        return
    db.execute("insert into tags(tag) values(?)", (tag,))
    db.commit()
    send_message(chat_id, f"tag added: {tag}")


def handle_deltag(db, chat_id, tag):
    if not tag:
        send_message(chat_id, "usage: /deltag <tag>")
        return
    tag = tag.strip().lower()
    row = db.execute("select id from tags where tag = ?", (tag,)).fetchone()
    if not row:
        send_message(chat_id, "tag not found")
        return
    db.execute("delete from tags where tag = ?", (tag,))
    db.commit()
    send_message(chat_id, f"tag removed: {tag}")


def handle_listsaved(db, chat_id):
    rows = db.execute(
        """
        select i.title, i.url, f.label, f.url as feed_url
        from items i
        left join feeds f on f.url = i.feed_url
        where i.trello_saved = 1
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


def handle_listtags(db, chat_id):
    rows = db.execute("select tag from tags order by tag asc").fetchall()
    if not rows:
        send_message(chat_id, "no tags")
        return
    send_message(chat_id, "\n".join("#"+row["tag"] for row in rows))


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
    send_message(chat_id, format_item(feed_name, entry))


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
        if GEMINI_API_KEY:
            try:
                tags = auto_tag_item(db, feed["url"], entry)
                if tags:
                    text += f"\n\ntags: {' '.join(tags)}"
            except Exception as e:
                text += f"\n\ntags: error ({e})"
        send_message(chat_id, text)
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


def trello_create_card(name, url, feed_url):
    if not trello_enabled():
        raise RuntimeError("trello is not configured")
    desc = url
    if feed_url:
        desc += f"\n\nfeed: {feed_url}"
    reply = http_post_form(
        "https://api.trello.com/1/cards",
        {
            "key": TRELLO_KEY,
            "token": TRELLO_TOKEN,
            "idList": TRELLO_LIST_ID,
            "name": name[:200],
            "desc": desc[:16000],
        },
    )
    return reply["url"]


def save_item_to_trello(db, item_id):
    row = db.execute(
        "select * from items where id = ?",
        (item_id,),
    ).fetchone()
    if not row:
        return "item not found"
    if row["trello_saved"]:
        return row["trello_card_url"] or "already saved"
    card_url = trello_create_card(row["title"] or "(no title)", row["url"] or "", row["feed_url"])
    db.execute(
        "update items set trello_saved = 1, trello_card_url = ? where id = ?",
        (card_url, item_id),
    )
    db.commit()
    return card_url


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



def safe_href(url):
    """Return url only if it uses http(s); otherwise a safe fallback."""
    if url and url.startswith(("http://", "https://")):
        return url
    return "#"



def feed_status_rows(db):
    return db.execute(
        """
        select f.url, f.label, f.title,
               (select i.published_ts from items i
                where i.feed_url = f.url and i.published_ts is not null
                order by i.published_ts desc limit 1) as last_published_ts
        from feeds f
        order by last_published_ts desc
        """
    ).fetchall()


def render_index():
    db = open_db()
    items = db.execute(
        """
        select i.title, i.url, i.published, i.seen_at, i.feed_url, i.trello_saved, i.trello_card_url,
               group_concat(it.tag, ' ') as tags
        from items i
        left join item_tags it on it.item_id = i.id
        where i.sent_message_id is not null
        group by i.id
        order by i.seen_at desc
        limit 10
        """
    ).fetchall()
    saved_items = db.execute(
        """
        select i.title, i.url, i.seen_at, i.feed_url,
               group_concat(it.tag, ' ') as tags
        from items i
        left join item_tags it on it.item_id = i.id
        where i.trello_saved = 1
        group by i.id
        order by i.seen_at desc
        limit 30
        """
    ).fetchall()
    feeds = feed_status_rows(db)
    feed_names = {row["url"]: feed_display_name(row["label"], row["url"]) for row in feeds}
    db.close()
    body = []
    body.append("<!doctype html>")
    body.append("<html>")
    body.append("<head>")
    body.append('<meta charset="utf-8">')
    body.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    body.append("<title>FeedBuddy</title>")
    body.append("<style>")
    body.append(
        """
        :root {
            --bg: #f4efe7;
            --fg: #1e1b18;
            --muted: #6f655c;
            --card-bg: #fffdf9;
            --card-border: #d8cfc4;
            --row-border: #e7ddd1;
            --link: #0f5c4d;
            --tag-bg: #e3f1eb;
            --tag-fg: #0f5c4d;
            --stale-bg: #fde8cc;
            --stale-fg: #8a4e00;
            --saved-bg: #e8f0fe;
            --saved-fg: #1a56c4;
        }
        body.dark {
            --bg: #1a1a1a;
            --fg: #e8e2d9;
            --muted: #9a9088;
            --card-bg: #242424;
            --card-border: #3a3530;
            --row-border: #333;
            --link: #4db89a;
            --tag-bg: #1e3329;
            --tag-fg: #4db89a;
            --stale-bg: #3d2a10;
            --stale-fg: #f0a84a;
            --saved-bg: #1a2a4a;
            --saved-fg: #7aaaff;
        }
        body {
            margin: 0;
            background: var(--bg);
            color: var(--fg);
            font: 16px/1.5 Georgia, serif;
        }
        main {
            max-width: 1140px;
            margin: 0 auto;
            padding: 32px 18px 60px;
        }
        h1 {
            margin: 0 0 8px;
            font-size: 36px;
        }
        .header {
            display: flex;
            align-items: baseline;
            gap: 16px;
            margin-bottom: 4px;
        }
        .toggle {
            font: 13px/1 Georgia, serif;
            color: var(--muted);
            background: none;
            border: none;
            cursor: pointer;
            padding: 0;
        }
        .toggle:hover { color: var(--fg); }
        .sub {
            color: var(--muted);
            margin-bottom: 28px;
        }
        .columns {
            display: grid;
            grid-template-columns: 1fr 320px;
            gap: 28px;
            align-items: start;
        }
        @media (max-width: 720px) {
            .columns { grid-template-columns: 1fr; }
        }
        .section {
            margin: 0 0 14px;
            font-size: 24px;
        }
        .col-side .section {
            margin-top: 0;
        }
        article {
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            padding: 18px 18px 16px;
            margin-bottom: 14px;
            box-shadow: 0 1px 0 rgba(0,0,0,0.03);
        }
        .col-side article {
            padding: 12px 14px 10px;
        }
        h2 {
            margin: 0 0 8px;
            font-size: 22px;
            line-height: 1.25;
        }
        .col-side h2 {
            font-size: 16px;
            margin-bottom: 4px;
        }
        a {
            color: var(--link);
        }
        .meta {
            color: var(--muted);
            font-size: 14px;
            margin-top: 10px;
        }
        .col-side .meta {
            font-size: 12px;
            margin-top: 4px;
        }
        .tag {
            display: inline-block;
            margin-left: 8px;
            padding: 1px 7px;
            border-radius: 999px;
            background: var(--tag-bg);
            color: var(--tag-fg);
            font-size: 12px;
            vertical-align: middle;
        }
        .badge-saved {
            display: inline-block;
            margin-left: 8px;
            padding: 1px 7px;
            border-radius: 999px;
            background: var(--saved-bg);
            color: var(--saved-fg);
            font-size: 12px;
            vertical-align: middle;
        }
        .feeds-section {
            margin-top: 40px;
        }
        .feeds-section .section {
            margin-bottom: 14px;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            background: var(--card-bg);
            border: 1px solid var(--card-border);
        }
        th, td {
            padding: 10px 12px;
            text-align: left;
            vertical-align: top;
            border-bottom: 1px solid var(--row-border);
        }
        th {
            font-size: 13px;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            color: var(--muted);
        }
        .stale {
            display: inline-block;
            margin-left: 8px;
            padding: 1px 7px;
            border-radius: 999px;
            background: var(--stale-bg);
            color: var(--stale-fg);
            font-size: 12px;
            vertical-align: middle;
        }
        """
    )
    body.append("</style>")
    body.append("</head>")
    body.append("<body>")
    body.append("<main>")
    body.append('<div class="header"><h1>FeedBuddy</h1><button class="toggle" id="theme-toggle"></button></div>')
    body.append('<div class="sub">Last 10 sent articles, newest first.</div>')
    body.append('<div class="columns">')

    # left column — recent posts
    body.append('<div class="col-main">')
    body.append('<div class="section">Recent posts</div>')
    if not items:
        body.append("<article><h2>No articles yet.</h2></article>")
    for row in items:
        title = escape(row["title"] or "(no title)")
        url = escape(safe_href(row["url"]))
        feed_url = escape(feed_names.get(row["feed_url"], row["feed_url"] or ""))
        seen = escape(fmt_time(row["seen_at"]))
        saved_badge = ""
        if row["trello_saved"]:
            saved_badge = ' <span class="badge-saved">saved</span>'
        tag_badges = ""
        if row["tags"]:
            tag_badges = "".join(
                f'<span class="tag">#{escape(t)}</span>'
                for t in row["tags"].split(" ")
            )
        body.append("<article>")
        body.append(f'<h2><a href="{url}">{title}</a>{saved_badge}</h2>')
        body.append(f'<div class="meta">seen: {seen}</div>')
        body.append(f'<div class="meta">{feed_url}</div>')
        if tag_badges:
            body.append(f'<div class="meta">{tag_badges}</div>')
        body.append("</article>")
    body.append("</div>")  # col-main

    # right column — saved posts
    body.append('<div class="col-side">')
    body.append('<div class="section">Saved</div>')
    if not saved_items:
        body.append("<article><h2>Nothing saved yet.</h2></article>")
    for row in saved_items:
        title = escape(row["title"] or "(no title)")
        url = escape(safe_href(row["url"]))
        feed_name = escape(feed_names.get(row["feed_url"], row["feed_url"] or ""))
        seen = escape(fmt_time(row["seen_at"]))
        tag_badges = ""
        if row["tags"]:
            tag_badges = "".join(
                f'<span class="tag">#{escape(t)}</span>'
                for t in row["tags"].split(" ")
            )
        body.append("<article>")
        body.append(f'<h2><a href="{url}">{title}</a></h2>')
        body.append(f'<div class="meta">{feed_name}</div>')
        body.append(f'<div class="meta">{seen}</div>')
        if tag_badges:
            body.append(f'<div class="meta">{tag_badges}</div>')
        body.append("</article>")
    body.append("</div>")  # col-side

    body.append("</div>")  # columns

    body.append('<div class="feeds-section">')
    body.append('<div class="section">Feeds</div>')
    body.append("<table>")
    body.append("<tr><th>Feed</th><th>Last post</th></tr>")
    for row in feeds:
        title = escape(feed_display_name(row["label"], row["url"]))
        url = escape(safe_href(row["url"]))
        last_published = escape(fmt_time(row["last_published_ts"]) if row["last_published_ts"] else "never")
        stale_badge = ""
        if row["last_published_ts"]:
            dt = parse_date(row["last_published_ts"])
            if dt:
                days = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).days
                if days > STALE_DAYS:
                    stale_badge = f' <span class="stale">{days}d</span>'
        parsed = urllib.parse.urlparse(row["url"])
        favicon_url = escape(f"{parsed.scheme}://{parsed.netloc}/favicon.ico")
        favicon = f'<img src="{favicon_url}" width="16" height="16" style="vertical-align:middle;margin-right:6px;" onerror="this.style.visibility=\'hidden\'">'
        body.append(f'<tr><td>{favicon}<a href="{url}">{title}</a></td><td>{last_published}{stale_badge}</td></tr>')
    body.append("</table>")
    body.append("</div>")  # feeds-section
    body.append("</main>")
    body.append("""<script>
const b = document.body, btn = document.getElementById('theme-toggle');
if (localStorage.getItem('dark') === '1') b.classList.add('dark');
btn.textContent = b.classList.contains('dark') ? 'light mode' : 'dark mode';
btn.onclick = () => {
    b.classList.toggle('dark');
    const d = b.classList.contains('dark') ? '1' : '0';
    localStorage.setItem('dark', d);
    btn.textContent = d === '1' ? 'light mode' : 'dark mode';
};
</script>""")
    body.append("</body>")
    body.append("</html>")
    return "\n".join(body).encode()


class WebHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/":
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"not found\n")
            return
        body = render_index()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def start_web():
    server = ThreadingHTTPServer((WEB_HOST, WEB_PORT), WebHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log(f"web ui on http://{WEB_HOST}:{WEB_PORT}/")
    return server


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
        row = db.execute("select id from items where id = ?", (item_id,)).fetchone()
        if not row:
            answer_callback_query(cb["id"], "item not found")
            return
        try:
            save_item_to_trello(db, row["id"])
            answer_callback_query(cb["id"], "saved")
            edit_reply_markup(chat_id, message_id, {
                "inline_keyboard": [[{"text": "Remove from later", "callback_data": f"unsave:{row['id']}"}]]
            })
        except Exception as e:
            log("save failed:", e)
            answer_callback_query(cb["id"], "save failed")

    elif data.startswith("unsave:"):
        item_id = data.split(":", 1)[1]
        row = db.execute("select id from items where id = ?", (item_id,)).fetchone()
        if not row:
            answer_callback_query(cb["id"], "item not found")
            return
        db.execute("update items set trello_saved = 0, trello_card_url = null where id = ?", (row["id"],))
        db.commit()
        answer_callback_query(cb["id"], "removed")
        edit_reply_markup(chat_id, message_id, {
            "inline_keyboard": [[{"text": "Save for later", "callback_data": f"save:{row['id']}"}]]
        })

    else:
        answer_callback_query(cb["id"], "unknown action")


def handle_message(db, update):
    msg = update["message"]
    chat_id = msg["chat"]["id"]
    text = msg.get("text") or ""
    if not text.startswith("/"):
        return
    if not chat_allowed(chat_id):
        return
    cmd, arg = parse_command(text)
    cmd = cmd.split("@", 1)[0]
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
    elif cmd == "/addtag":
        handle_addtag(db, chat_id, arg)
    elif cmd == "/deltag":
        handle_deltag(db, chat_id, arg)
    elif cmd == "/listtags":
        handle_listtags(db, chat_id)
    elif cmd == "/summary":
        handle_summary(db, chat_id)
    elif cmd == "/testfeed":
        handle_testfeed(db, chat_id, arg)
    elif cmd == "/testall":
        handle_testall(db, chat_id)
    elif cmd == "/testsend":
        handle_testsend(db, chat_id)


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


def main():
    db = open_db()
    backfill_published_ts(db)
    start_web()
    next_feed_check = 0
    log("started")
    while True:
        if time.time() >= next_feed_check:
            poll_feeds(db)
            next_feed_check = time.time() + CHECK_EVERY
        poll_telegram(db)


def cmd_import(path):
    sources = read_sources_file(path)
    if not sources:
        print("no sources found in", path)
        sys.exit(1)
    db = open_db()
    added = 0
    for row in sources:
        created = ensure_feed(db, row["url"], label=row["label"], catch_up=True)
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
