#!/usr/bin/env python3
"""
Find your Telegram chat id (for TELEGRAM_CHAT_ID in .env).

Steps:
  1. Create a bot with @BotFather and put its token in TELEGRAM_BOT_TOKEN.
  2. Add the bot to your group (or make it an admin of your channel).
  3. Post any message in that group/channel so Telegram has an update to show.
  4. Run:  python telegram_chat_id.py

It prints every chat the bot has seen recently, with the id to copy.
Group ids are negative numbers, e.g. -1001234567890.
"""

import os
import sys

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def main():
    if not TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN is not set. Get one from @BotFather, "
                 "put it in .env, then re-run.")

    r = requests.get(
        f"https://api.telegram.org/bot{TOKEN}/getUpdates", timeout=30
    )
    if r.status_code != 200:
        sys.exit(f"Telegram API error ({r.status_code}): {r.text[:300]}")

    data = r.json()
    if not data.get("ok"):
        sys.exit(f"Telegram said: {data}")

    chats = {}
    for upd in data.get("result", []):
        msg = (upd.get("message") or upd.get("channel_post")
               or upd.get("my_chat_member") or {})
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat

    if not chats:
        print("No chats seen yet. Add the bot to the group and POST a message "
              "there (any text), then run this again.")
        return

    print("Chats the bot can see — copy the id of your target into "
          "TELEGRAM_CHAT_ID:\n")
    for cid, chat in chats.items():
        name = chat.get("title") or chat.get("username") or chat.get("first_name", "")
        print(f"  {str(cid):<16}  [{chat.get('type','?')}]  {name}")


if __name__ == "__main__":
    main()
