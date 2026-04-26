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
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import feedparser


DB_PATH = "feedbuddy.db"
SOURCES_PATH = "sources.txt"
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
ADMIN_USER_ID = env("TELEGRAM_ADMIN_USER_ID")
TRELLO_KEY = env("TRELLO_KEY")
TRELLO_TOKEN = env("TRELLO_TOKEN")
TRELLO_LIST_ID = env("TRELLO_LIST_ID")
WEB_HOST = env("WEB_HOST", "127.0.0.1")
WEB_PORT = int(env("WEB_PORT", "8080"))


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


def tg_api(method, data):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    reply = http_post_json(url, data, timeout=TELEGRAM_TIMEOUT + 10)
    if not reply.get("ok"):
        raise RuntimeError(f"telegram {method} failed: {reply}")
    return reply["result"]


def send_message(chat_id, text, reply_markup=None):
    data = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": False,
    }
    if reply_markup:
        data["reply_markup"] = reply_markup
    return tg_api("sendMessage", data)


def answer_callback_query(callback_id, text):
    try:
        tg_api("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})
    except Exception as e:
        log("callback answer failed:", e)


def append_source_file(url, label=None):
    existing = {row["url"] for row in read_sources_file()}
    if url in existing:
        return
    with open(SOURCES_PATH, "a", encoding="utf-8") as f:
        if os.path.getsize(SOURCES_PATH) > 0:
            f.write("\n")
        if label:
            f.write(f"{label} | {url}")
        else:
            f.write(url)


def rewrite_source_file(rows):
    with open(SOURCES_PATH, "w", encoding="utf-8") as f:
        for i, row in enumerate(rows):
            if i:
                f.write("\n")
            if row["label"]:
                f.write(f"{row['label']} | {row['url']}")
            else:
                f.write(row["url"])


def parse_source_line(line):
    if " | " in line:
        label, url = line.split(" | ", 1)
        return {"label": label.strip(), "url": url.strip()}
    return {"label": None, "url": line.strip()}


def read_sources_file():
    if not os.path.exists(SOURCES_PATH):
        return []
    rows = []
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
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
    return {
        "key": key,
        "title": title,
        "link": link,
        "published": published,
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


def import_sources(db):
    for row in read_sources_file():
        ensure_feed(db, row["url"], label=row["label"], catch_up=True)


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
                insert or ignore into items(feed_url, item_key, title, url, published, seen_at)
                values(?, ?, ?, ?, ?, ?)
                """,
                (url, entry["key"], entry["title"], entry["link"], entry["published"], now()),
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
    lines = [entry["title"], "", entry["link"]]
    if feed_name:
        lines.append("")
        lines.append(f"feed: {feed_name}")
    return "\n".join(lines)


def send_feed_item(db, feed_url, feed_name, entry):
    db.execute(
        """
        insert or ignore into items(feed_url, item_key, title, url, published, seen_at)
        values(?, ?, ?, ?, ?, ?)
        """,
        (
            feed_url,
            entry["key"],
            entry["title"],
            entry["link"],
            entry["published"],
            now(),
        ),
    )
    row = find_item_by_key(db, feed_url, entry["key"])
    item_id = row["id"]
    markup = None
    if trello_enabled():
        markup = {
            "inline_keyboard": [
                [{"text": "Save to Trello", "callback_data": f"save:{item_id}"}]
            ]
        }
    msg = send_message(TARGET_CHAT_ID, format_item(feed_name, entry), markup)
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


def user_allowed(user_id):
    if not ADMIN_USER_ID:
        return False
    return str(user_id) == str(ADMIN_USER_ID)


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
            "/addfeed <url>",
            "/delfeed <url>",
            "/summary",
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
        append_source_file(url, label=label)
        send_message(chat_id, "feed added")
    except Exception as e:
        send_message(chat_id, f"cannot add feed: {e}")


def handle_delfeed(db, chat_id, url):
    if not url:
        send_message(chat_id, "missing url")
        return
    url = parse_source_line(url)["url"]
    delete_feed(db, url)
    rewrite_source_file(list_feeds(db))
    send_message(chat_id, "feed removed")


def handle_listfeeds(db, chat_id):
    feeds = list_feeds(db)
    if not feeds:
        send_message(chat_id, "no feeds")
        return
    lines = []
    for row in feeds:
        name = feed_display_name(row["label"], row["url"])
        if row["label"]:
            lines.append(f"{name} | {row['url']}")
        else:
            lines.append(name)
    chunk = []
    size = 0
    for line in lines:
        if size + len(line) + 1 > 3500:
            send_message(chat_id, "\n".join(chunk))
            chunk = []
            size = 0
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        send_message(chat_id, "\n".join(chunk))


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


def last_items(limit=10):
    db = open_db()
    rows = db.execute(
        """
        select title, url, published, seen_at, feed_url, trello_saved, trello_card_url
        from (
            select title, url, published, seen_at, feed_url, trello_saved, trello_card_url
            from items
            where sent_message_id is not null
            order by seen_at desc
            limit ?
        )
        order by seen_at asc
        """,
        (limit,),
    ).fetchall()
    db.close()
    return rows


def safe_href(url):
    """Return url only if it uses http(s); otherwise a safe fallback."""
    if url and url.startswith(("http://", "https://")):
        return url
    return "#"


def article_time(row):
    return row["published"] or row["seen_at"] or ""


def feed_status_rows():
    rows = []
    for source in read_sources_file():
        url = source["url"]
        label = source["label"]
        try:
            title, entries = fetch_feed(url)
            published = "no entries"
            if entries:
                published = entries[0]["published"] or "date missing"
            rows.append(
                {
                    "title": feed_display_name(label, url),
                    "url": url,
                    "published": published,
                    "real_title": title,
                }
            )
        except Exception as e:
            rows.append(
                {
                    "title": feed_display_name(label, url),
                    "url": url,
                    "published": f"error: {e}",
                    "real_title": url,
                }
            )
    return rows


def render_index():
    items = last_items(10)
    feeds = feed_status_rows()
    feed_names = {}
    db = open_db()
    for row in db.execute("select url, label from feeds").fetchall():
        feed_names[row["url"]] = feed_display_name(row["label"], row["url"])
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
        :root { color-scheme: light; }
        body {
            margin: 0;
            background: #f4efe7;
            color: #1e1b18;
            font: 16px/1.5 Georgia, serif;
        }
        main {
            max-width: 760px;
            margin: 0 auto;
            padding: 32px 18px 60px;
        }
        h1 {
            margin: 0 0 8px;
            font-size: 36px;
        }
        .sub {
            color: #6f655c;
            margin-bottom: 28px;
        }
        .section {
            margin: 30px 0 14px;
            font-size: 24px;
        }
        article {
            background: #fffdf9;
            border: 1px solid #d8cfc4;
            padding: 18px 18px 16px;
            margin-bottom: 14px;
            box-shadow: 0 1px 0 rgba(0,0,0,0.03);
        }
        h2 {
            margin: 0 0 8px;
            font-size: 22px;
            line-height: 1.25;
        }
        a {
            color: #0f5c4d;
        }
        .meta {
            color: #6f655c;
            font-size: 14px;
            margin-top: 10px;
        }
        .tag {
            display: inline-block;
            margin-left: 8px;
            padding: 1px 7px;
            border-radius: 999px;
            background: #e3f1eb;
            color: #0f5c4d;
            font-size: 12px;
            vertical-align: middle;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            background: #fffdf9;
            border: 1px solid #d8cfc4;
        }
        th, td {
            padding: 10px 12px;
            text-align: left;
            vertical-align: top;
            border-bottom: 1px solid #e7ddd1;
        }
        th {
            font-size: 13px;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            color: #6f655c;
        }
        """
    )
    body.append("</style>")
    body.append("</head>")
    body.append("<body>")
    body.append("<main>")
    body.append("<h1>FeedBuddy</h1>")
    body.append("<div class=\"sub\">Last 10 sent articles, oldest first.</div>")
    body.append("<div class=\"section\">Recent posts</div>")
    if not items:
        body.append("<article><h2>No articles yet.</h2></article>")
    for row in items:
        title = escape(row["title"] or "(no title)")
        url = escape(safe_href(row["url"]))
        feed_url = escape(feed_names.get(row["feed_url"], row["feed_url"] or ""))
        when = escape(article_time(row))
        trello = ""
        if row["trello_saved"] and row["trello_card_url"]:
            card_url = escape(safe_href(row["trello_card_url"]))
            trello = f' <a class="tag" href="{card_url}">trello</a>'
        body.append("<article>")
        body.append(f'<h2><a href="{url}">{title}</a>{trello}</h2>')
        body.append(f'<div class="meta">{when}</div>')
        body.append(f'<div class="meta">{feed_url}</div>')
        body.append("</article>")
    body.append("<div class=\"section\">Feeds</div>")
    body.append("<table>")
    body.append("<tr><th>Feed</th><th>Last post</th></tr>")
    for row in feeds:
        title = escape(row["title"])
        url = escape(safe_href(row["url"]))
        published = escape(row["published"])
        body.append(f'<tr><td><a href="{url}">{title}</a></td><td>{published}</td></tr>')
    body.append("</table>")
    body.append("</main>")
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


def handle_callback_query(db, update):
    cb = update["callback_query"]
    user_id = cb.get("from", {}).get("id")
    if not user_allowed(user_id):
        answer_callback_query(cb["id"], "not allowed")
        return
    data = cb.get("data") or ""
    if not data.startswith("save:"):
        answer_callback_query(cb["id"], "unknown action")
        return
    item_id = data.split(":", 1)[1]
    row = db.execute("select id from items where id = ?", (item_id,)).fetchone()
    if not row:
        answer_callback_query(cb["id"], "item not found")
        return
    try:
        card_url = save_item_to_trello(db, row["id"])
        answer_callback_query(cb["id"], "saved")
        send_message(cb["message"]["chat"]["id"], card_url)
    except Exception as e:
        log("trello save failed:", e)
        answer_callback_query(cb["id"], "save failed")


def reaction_has_star(reactions):
    for r in reactions or []:
        emoji = r.get("emoji")
        if emoji == "⭐":
            return True
    return False


def handle_message_reaction(db, update):
    if not trello_enabled():
        return
    reaction = update.get("message_reaction") or {}
    user = reaction.get("user")
    actor_chat = reaction.get("actor_chat")
    user_id = user.get("id") if user else None
    if user_id is not None and not user_allowed(user_id):
        return
    if user_id is None and actor_chat:
        return
    if not reaction_has_star(reaction.get("new_reaction")):
        return
    chat = reaction.get("chat") or {}
    row = find_item_by_message(db, chat.get("id"), reaction.get("message_id"))
    if not row:
        return
    try:
        card_url = save_item_to_trello(db, row["id"])
        send_message(chat["id"], card_url)
    except Exception as e:
        log("reaction save failed:", e)


def handle_message(db, update):
    msg = update["message"]
    chat_id = msg["chat"]["id"]
    user_id = msg.get("from", {}).get("id")
    text = msg.get("text") or ""
    if not text.startswith("/"):
        return
    if not user_allowed(user_id):
        send_message(chat_id, "not allowed")
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
    elif cmd == "/summary":
        handle_summary(db, chat_id)
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
                    "message_reaction",
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
            elif "message_reaction" in update:
                handle_message_reaction(db, update)
        except Exception as e:
            log("update handling failed:", e)


def main():
    db = open_db()
    import_sources(db)
    start_web()
    next_feed_check = 0
    log("started")
    while True:
        if time.time() >= next_feed_check:
            poll_feeds(db)
            next_feed_check = time.time() + CHECK_EVERY
        poll_telegram(db)


if __name__ == "__main__":
    main()
