#!/usr/bin/env python3
"""Post a pipeline digest to Telegram.

Uses a SEPARATE dev bot from the trading bot. Two reasons: only one process may
poll a given bot token (a second causes 409 Conflict and both go deaf), and a
pipeline that can post as the trading bot can also be mistaken for it.

    notify.py "<title>" <markdown-file>
"""

import json
import os
import pathlib
import sys
import urllib.parse
import urllib.request

LIMIT = 3900

token = os.environ.get("TELEGRAM_BOT_TOKEN")
chat = os.environ.get("TELEGRAM_CHAT_ID")
if not token or not chat:
    print("no dev telegram credentials; skipping digest")
    sys.exit(0)

title = sys.argv[1] if len(sys.argv) > 1 else "Pipeline"
path = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else None
body = (
    path.read_text(encoding="utf-8")
    if path and path.exists()
    else "(no digest produced)"
)

text = f"*{title}*\n\n{body}"
if len(text) > LIMIT:
    text = text[:LIMIT] + "\n\n(truncated — full text in the workflow run)"

data = urllib.parse.urlencode(
    {"chat_id": chat, "text": text, "parse_mode": "Markdown"}
).encode()
try:
    with urllib.request.urlopen(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=30
    ) as r:
        json.loads(r.read())
    print("digest sent")
except Exception as exc:  # noqa: BLE001 — a failed digest must not fail the run
    print(f"digest not sent: {type(exc).__name__}")
