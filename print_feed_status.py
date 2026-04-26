#!/usr/bin/env python3

import sys
import urllib.request

import feedparser


USER_AGENT = "FeedBuddy/0.1"
SOURCES_PATH = "sources.txt"


def read_sources(path):
    urls = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            urls.append(line)
    return urls


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        return feedparser.parse(r.read())


def feed_name(parsed, url):
    return parsed.feed.get("title") or url


def last_post_date(parsed):
    if not parsed.entries:
        return "no entries"
    entry = parsed.entries[0]
    for key in ("published", "updated", "created"):
        value = entry.get(key)
        if value:
            return value
    return "date missing"


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else SOURCES_PATH
    for url in read_sources(path):
        try:
            parsed = fetch(url)
            print(f"{feed_name(parsed, url)} | {last_post_date(parsed)}")
        except Exception as e:
            print(f"{url} | error: {e}")


if __name__ == "__main__":
    main()
