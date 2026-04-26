#!/usr/bin/env python3

import json
import os
import sys
import urllib.parse
import urllib.request


USER_AGENT = "FeedBuddy/0.1"


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


def env(name, required=False):
    value = os.getenv(name)
    if required and not value:
        print("missing env:", name, file=sys.stderr)
        sys.exit(1)
    return value


def http_get_json(url, params):
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{url}?{query}",
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def main():
    load_dotenv()

    key = env("TRELLO_KEY", required=True)
    token = env("TRELLO_TOKEN", required=True)
    params = {"key": key, "token": token}

    boards = http_get_json(
        "https://api.trello.com/1/members/me/boards",
        params,
    )

    for board in boards:
        print(f"BOARD {board['id']} | {board['name']}")
        lists = http_get_json(
            f"https://api.trello.com/1/boards/{board['id']}/lists",
            params,
        )
        for lst in lists:
            print(f"  LIST  {lst['id']} | {lst['name']}")
        print()


if __name__ == "__main__":
    main()
