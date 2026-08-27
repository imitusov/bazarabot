#!/usr/bin/env python3
"""V8 - Telegram delivery, command receipt, and the message length limit.

Uses the HTTP API directly rather than the bot library: this runs before any
dependency beyond the SDK is installed, and it is the transport that matters
here, not the library wrapping it.
"""

import json
import pathlib
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _harness import Verifier, env  # noqa: E402

BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", secret=True)
CHAT_ID = env("TELEGRAM_CHAT_ID")
WAIT_SECONDS = 120

v = Verifier("V8", "Telegram transport")


def api(method, params=None):
    url = "https://api.telegram.org/bot{}/{}".format(BOT_TOKEN, method)
    data = urllib.parse.urlencode(params or {}).encode()
    with urllib.request.urlopen(url, data=data, timeout=30) as response:
        return json.loads(response.read().decode())


try:
    me = api("getMe")
    v.check("bot token authenticates", me.get("ok"),
            "@{}".format(me.get("result", {}).get("username", "?")))

    sent = api("sendMessage", {"chat_id": CHAT_ID,
                               "text": "zarabot V8: transport check"})
    v.check("message delivered to the configured chat", sent.get("ok"))

    long_text = "x" * 4096
    long_sent = api("sendMessage", {"chat_id": CHAT_ID, "text": long_text})
    v.check("4096-character message is accepted", long_sent.get("ok"),
            "the truncation contract assumes this is the ceiling")

    api("sendMessage", {"chat_id": CHAT_ID,
                        "text": "V8 is listening now — reply /status within {}s. "
                                "A command sent before this message may already "
                                "have been consumed by another poller.".format(
                                    WAIT_SECONDS)})
    v.note("waiting up to {}s for a command from chat {}".format(WAIT_SECONDS, CHAT_ID))

    deadline = time.time() + WAIT_SECONDS
    received = None
    offset = 0
    while time.time() < deadline and received is None:
        updates = api("getUpdates", {"timeout": 5, "offset": offset})
        for update in updates.get("result", []):
            offset = update["update_id"] + 1
            message = update.get("message") or {}
            if str((message.get("chat") or {}).get("id")) == str(CHAT_ID):
                received = message.get("text")
                break
        if received is None:
            time.sleep(1)

    v.check("an incoming command was received from the authorised chat",
            received is not None,
            "received: {}".format(received) if received
            else "nothing arrived within {}s".format(WAIT_SECONDS))
except Exception as exc:  # noqa: BLE001
    v.crashed(exc)

v.finish()
