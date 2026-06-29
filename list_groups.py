#!/usr/bin/env python3
"""
List the WhatsApp groups your linked number can see, with their IDs.

Run this once after connecting your number in the Whapi dashboard to find the
GROUP_ID (the ...@g.us JID) to paste into .env. Reads PROVIDER_TOKEN from the
environment / .env, same as the main script.

    python list_groups.py
"""

import os
import sys

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

PROVIDER = os.environ.get("PROVIDER", "whapi").lower()
TOKEN = os.environ.get("PROVIDER_TOKEN", "").strip()
BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://127.0.0.1:3000").rstrip("/")


def main():
    if PROVIDER == "baileys":
        # The self-hosted bridge must be running and linked (scan the QR once).
        url = f"{BRIDGE_URL}/groups"
        headers = {}
    elif not TOKEN:
        sys.exit("PROVIDER_TOKEN is not set. Put your Whapi token in .env first.")
    elif PROVIDER == "whapi":
        url = "https://gate.whapi.cloud/groups"
        headers = {"Authorization": f"Bearer {TOKEN}"}
    elif PROVIDER == "wassenger":
        # Wassenger needs the device id in the path; check your dashboard.
        sys.exit("Wassenger: list groups via "
                 "GET https://api.wassenger.com/v1/chat/<DEVICE_ID>/groups")
    else:
        sys.exit(f"unknown PROVIDER: {PROVIDER}")

    try:
        r = requests.get(url, headers=headers, timeout=30)
    except requests.exceptions.ConnectionError:
        if PROVIDER == "baileys":
            sys.exit(f"can't reach the bridge at {BRIDGE_URL}. "
                     "Start it first: cd bridge && node index.js")
        raise
    if r.status_code != 200:
        sys.exit(f"request failed ({r.status_code}): {r.text[:300]}")

    groups = r.json().get("groups", [])
    if not groups:
        print("No groups found. Is the linked number a member of any group?")
        return

    print(f"{len(groups)} group(s) — copy the id of the one you want into GROUP_ID:\n")
    for g in groups:
        name = g.get("name") or g.get("subject") or "(unnamed)"
        print(f"  {g.get('id', '?'):<32}  {name}")


if __name__ == "__main__":
    main()
