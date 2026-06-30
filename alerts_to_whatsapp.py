#!/usr/bin/env python3
"""
News -> chat group.

Fetches recent news on your topic, drops anything already seen, optionally
scores each item for relevance with Groq, and posts the survivors into a
chat group. Default destination is Telegram (official bot API — free, no ban
risk, no persistent connection). WhatsApp is also supported via a free
self-hosted bridge (PROVIDER=baileys) or paid providers (whapi/wassenger).

News source is selectable with SOURCE:
    SOURCE=gdelt   -> GDELT DOC 2.0 API (free, no key, keyword-controlled)   [default]
    SOURCE=rss     -> Google Alert RSS feed URL(s)

Run modes:
    python alerts_to_whatsapp.py            # one shot (use with a cron job)
    python alerts_to_whatsapp.py --loop     # long-running worker
    python alerts_to_whatsapp.py --dry-run  # print, never send (test safely)

Config is via environment variables (see CONFIG block). On Render/Railway
set them in the dashboard; locally export them or drop a .env beside this
file.
"""

import argparse
import html
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import requests

# feedparser is only needed for SOURCE=rss; import lazily so a GDELT-only
# deploy doesn't require it.
try:
    import feedparser
except ImportError:
    feedparser = None

# Optional .env support for local dev (no hard dependency).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --------------------------------------------------------------------------- #
# CONFIG                                                                       #
# --------------------------------------------------------------------------- #

# Where the news comes from: "gdelt" (default) or "rss".
SOURCE = os.environ.get("SOURCE", "gdelt").lower()

# --- GDELT (SOURCE=gdelt) --------------------------------------------------- #
# The search query. Supports "phrases", OR inside (parentheses), and trailing
# operators like sourcelang:english, sourcecountry:IN, domain:thehindu.com.
# Keep at least one real keyword (operators-only queries are rejected).
GDELT_QUERY = os.environ.get(
    "GDELT_QUERY",
    '("data privacy" OR "data protection" OR "DPDP Act" OR '
    '"AI governance" OR cybersecurity) sourcelang:english',
)
# Look-back window each run. For a 15-min cron, 1h over-fetches and the
# de-dupe trims repeats, which is robust against gaps. e.g. 15min,1h,24h,7d.
GDELT_TIMESPAN = os.environ.get("GDELT_TIMESPAN", "1h")
GDELT_MAX = int(os.environ.get("GDELT_MAX", "75"))  # cap 250
# GDELT's public API rate-limits hard; retry with linear backoff on 429/5xx.
GDELT_RETRIES = int(os.environ.get("GDELT_RETRIES", "4"))
GDELT_BACKOFF = float(os.environ.get("GDELT_BACKOFF", "6"))  # seconds * attempt

# --- RSS (SOURCE=rss) ------------------------------------------------------- #
# Comma-separated Google Alert RSS feed URLs. Label one as "label|url".
ALERT_FEEDS = os.environ.get("ALERT_FEEDS", "").strip()

# --- Messaging provider ----------------------------------------------------- #
# Provider: "telegram" (official bot API — free, no ban risk; recommended),
# "baileys" (free, self-hosted WhatsApp bridge), "whapi", or "wassenger".
PROVIDER = os.environ.get("PROVIDER", "telegram").lower()
PROVIDER_TOKEN = os.environ.get("PROVIDER_TOKEN", "").strip()
# For PROVIDER=baileys: the local Node bridge's HTTP base URL (see bridge/).
BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://127.0.0.1:3000").rstrip("/")

# --- Telegram (PROVIDER=telegram) ------------------------------------------- #
# Bot token from @BotFather, and the target chat id (group/channel). Get the
# chat id by adding the bot to the group and running telegram_chat_id.py.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# The target group's id.
#   Whapi:     full JID, e.g. 120363040469423382@g.us
#   Wassenger: the group's WID/id from its /chat/groups endpoint
GROUP_ID = os.environ.get("GROUP_ID", "").strip()

# --- Optional Groq relevance gate ------------------------------------------- #
# Leave GROQ_API_KEY unset to disable scoring and forward everything unseen.
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
SCORE_THRESHOLD = int(os.environ.get("SCORE_THRESHOLD", "60"))
SCORE_TOPIC = os.environ.get(
    "SCORE_TOPIC",
    "AI governance, data privacy, and cybersecurity news relevant to CISOs, "
    "DPOs and privacy engineers in India and the GCC",
)

# --- Optional newsletter-fit pass (needs GROQ_API_KEY) ---------------------- #
# Judges whether each headline is a strong pick for your newsletter.
#   "off"    -> do nothing (default)
#   "tag"    -> post everything, but prefix newsletter-worthy items with a tag
#   "filter" -> only post newsletter-worthy items (drops confident "no"s)
NEWSLETTER_MODE = os.environ.get("NEWSLETTER_MODE", "off").lower()
NEWSLETTER_TAG = os.environ.get("NEWSLETTER_TAG", "⭐ NEWSLETTER PICK")
NEWSLETTER_CRITERIA = os.environ.get(
    "NEWSLETTER_CRITERIA",
    "substantive developments — new laws or regulations, major enforcement or "
    "fines, significant breaches, notable policy or industry shifts, important "
    "research, or product moves with broad impact; NOT press releases, product "
    "listings, vendor marketing, minor or local items, or clickbait",
)

# --- Misc ------------------------------------------------------------------- #
# Where to keep the "already posted" record so nothing double-fires.
DB_PATH = os.environ.get("SEEN_DB", "seen.db")
# Polling interval for --loop mode, in seconds.
LOOP_INTERVAL = int(os.environ.get("LOOP_INTERVAL", "900"))  # 15 min
# Be polite between sends so the linked number reads as human, not a firehose.
SEND_DELAY = float(os.environ.get("SEND_DELAY", "4"))
# Cap how many items to post per run (0 = no limit). Overflow is left unseen
# and goes out on the next run, so nothing is lost — bursts just get paced.
MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN", "0"))

USER_AGENT = "alerts-to-whatsapp/1.0 (+https://github.com/)"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("alerts")


# --------------------------------------------------------------------------- #
# DEDUPE STORE                                                                 #
# --------------------------------------------------------------------------- #

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS seen "
        "(key TEXT PRIMARY KEY, posted_at TEXT)"
    )
    return conn


def already_seen(conn, key):
    cur = conn.execute("SELECT 1 FROM seen WHERE key = ?", (key,))
    return cur.fetchone() is not None


def mark_seen(conn, key):
    conn.execute(
        "INSERT OR IGNORE INTO seen (key, posted_at) VALUES (?, ?)",
        (key, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# SHARED HELPERS                                                               #
# --------------------------------------------------------------------------- #

_TAG_RE = re.compile(r"<[^>]+>")


def clean_text(raw):
    """Strip any embedded tags and unescape HTML entities."""
    return html.unescape(_TAG_RE.sub("", raw or "")).strip()


def real_url(link):
    """Google Alert links wrap the source in a /url?...&url=<real> redirect."""
    try:
        qs = parse_qs(urlparse(link).query)
        if "url" in qs:
            return qs["url"][0]
    except Exception:
        pass
    return link


_NORM_RE = re.compile(r"[^a-z0-9]+")


def content_key(title, url):
    """A dedupe key for the *story*, not the URL.

    The same headline syndicated across outlets (different URLs) collapses to
    a single post when we key on the normalized title. Falls back to the URL
    when the title is missing or too generic to trust as an identifier.
    """
    norm = _NORM_RE.sub(" ", (title or "").lower()).strip()
    if len(norm) < 12:          # empty / "(no title)" / ultra-short headline
        return f"url:{url}"
    return f"title:{norm}"


# --------------------------------------------------------------------------- #
# SOURCE: GDELT                                                                #
# --------------------------------------------------------------------------- #

def gather_gdelt():
    """Yield (label, key, title, url, published) from the GDELT DOC 2.0 API."""
    params = {
        "query": GDELT_QUERY,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": str(min(GDELT_MAX, 250)),
        "timespan": GDELT_TIMESPAN,
        "sort": "DateDesc",
    }
    # GDELT's public API rate-limits aggressively (429) and occasionally
    # blips with 5xx. Retry with backoff before giving up on a run.
    r = None
    for attempt in range(GDELT_RETRIES):
        try:
            r = requests.get(
                "https://api.gdeltproject.org/api/v2/doc/doc",
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=30,
            )
            if r.status_code == 429 or r.status_code >= 500:
                wait = GDELT_BACKOFF * (attempt + 1)
                log.warning(
                    "GDELT %s, retrying in %.0fs (%d/%d)",
                    r.status_code, wait, attempt + 1, GDELT_RETRIES,
                )
                time.sleep(wait)
                continue
            r.raise_for_status()
            break
        except Exception as exc:
            wait = GDELT_BACKOFF * (attempt + 1)
            log.warning("GDELT request error: %s, retrying in %.0fs", exc, wait)
            time.sleep(wait)
    if r is None or r.status_code != 200:
        log.error("GDELT unavailable after %d attempts; skipping run", GDELT_RETRIES)
        return

    body = r.text.strip()
    if not body:
        return
    try:
        data = r.json()
    except ValueError:
        # GDELT returns a plain-text error (e.g. bad query) instead of JSON.
        log.warning("GDELT returned non-JSON (query issue?): %s", body[:200])
        return

    for a in data.get("articles", []) or []:
        url = a.get("url", "")
        if not url:
            continue
        published = ""
        sd = a.get("seendate", "")
        if sd:
            try:
                dt = datetime.strptime(sd, "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=timezone.utc
                )
                published = dt.strftime("%d %b %Y, %H:%M UTC")
            except ValueError:
                published = sd
        yield (
            a.get("domain", "news"),
            url,                               # url is the de-dupe key
            clean_text(a.get("title", "(no title)")),
            url,
            published,
        )


# --------------------------------------------------------------------------- #
# SOURCE: RSS (Google Alerts)                                                  #
# --------------------------------------------------------------------------- #

def gather_rss():
    """Yield (label, key, title, url, published) from Google Alert RSS feeds."""
    if feedparser is None:
        log.error("SOURCE=rss needs feedparser: pip install feedparser")
        return
    feeds = [f for f in ALERT_FEEDS.split(",") if f.strip()]
    if not feeds:
        log.error("ALERT_FEEDS is empty. Set it to your Google Alert RSS url(s).")
        return

    for spec in feeds:
        label, _, url = spec.partition("|")
        if not url:          # no label given; whole thing is the url
            label, url = "alert", label
        label = label.strip() or "alert"

        parsed = feedparser.parse(url.strip())
        if parsed.bozo:
            log.warning("feed parse issue (%s): %s", label, parsed.bozo_exception)

        for e in parsed.entries:
            key = getattr(e, "id", None) or real_url(getattr(e, "link", ""))
            if not key:
                continue
            published = ""
            if getattr(e, "published_parsed", None):
                published = time.strftime("%d %b %Y, %H:%M", e.published_parsed)
            yield (
                label,
                key,
                clean_text(getattr(e, "title", "(no title)")),
                real_url(getattr(e, "link", "")),
                published,
            )


def gather_items():
    """Dispatch to the configured news source."""
    if SOURCE == "gdelt":
        yield from gather_gdelt()
    elif SOURCE == "rss":
        yield from gather_rss()
    else:
        log.error("unknown SOURCE: %s (use 'gdelt' or 'rss')", SOURCE)


# --------------------------------------------------------------------------- #
# OPTIONAL GROQ RELEVANCE GATE                                                 #
# --------------------------------------------------------------------------- #

def passes_score(title):
    """Return True if the item clears SCORE_THRESHOLD (or scoring is off)."""
    if not GROQ_API_KEY:
        return True
    prompt = (
        f"Rate from 0 to 100 how relevant this headline is to: {SCORE_TOPIC}.\n"
        f"Reply with the number only.\n\nHeadline: {title}"
    )
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={
                "model": GROQ_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 4,
            },
            timeout=20,
        )
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"]
        score = int(re.search(r"\d+", raw).group())
        log.info("  score %d/%d  %s", score, SCORE_THRESHOLD, title[:60])
        return score >= SCORE_THRESHOLD
    except Exception as exc:
        # Fail open: don't silently lose a news item if scoring hiccups.
        log.warning("  scoring failed (%s); forwarding anyway", exc)
        return True


def newsletter_fit(title):
    """Editorial verdict: is this headline a strong newsletter pick?

    Returns True (good fit), False (not a fit), or None when undecidable
    (feature off, no API key, or the call errored). None never drops news.
    """
    if NEWSLETTER_MODE not in ("tag", "filter") or not GROQ_API_KEY:
        return None
    prompt = (
        f"You are the editor of a newsletter on: {SCORE_TOPIC}.\n"
        f"Decide if this news headline is a strong fit to feature. "
        f"Good fits are: {NEWSLETTER_CRITERIA}.\n"
        f"Answer with only YES or NO.\n\nHeadline: {title}"
    )
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={
                "model": GROQ_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 3,
            },
            timeout=20,
        )
        r.raise_for_status()
        ans = r.json()["choices"][0]["message"]["content"].strip().upper()
        fit = ans.startswith("Y")
        log.info("  newsletter:%-3s %s", "YES" if fit else "no", title[:55])
        return fit
    except Exception as exc:
        log.warning("  newsletter check failed (%s); not tagging", exc)
        return None


# --------------------------------------------------------------------------- #
# WHATSAPP SEND                                                                #
# --------------------------------------------------------------------------- #

def format_message(label, title, url, published):
    meta = f"{label}" + (f" · {published}" if published else "")
    if PROVIDER == "telegram":
        # HTML parse_mode: escape so titles/urls with &, <, > can't break it.
        t = html.escape(title)
        u = html.escape(url, quote=False)
        m = html.escape(meta)
        return f"<b>{t}</b>\n{u}\n<i>{m}</i>"
    # WhatsApp-style markdown for whapi/wassenger/baileys.
    return f"*{title}*\n{url}\n_{meta}_"


def send_to_group(text, dry_run=False):
    if dry_run:
        log.info("DRY RUN, would send:\n%s\n", text)
        return True

    if PROVIDER == "telegram":
        # Official Bot API: a plain HTTPS POST, no persistent connection.
        endpoint = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        headers = {}
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        }
    elif PROVIDER == "baileys":
        # Free, self-hosted: post to the local Node bridge (must be running).
        # Group id goes in `to` as the full JID, e.g. 120363xxxx@g.us.
        endpoint = f"{BRIDGE_URL}/send"
        headers = {}
        payload = {"to": GROUP_ID, "body": text}
    elif PROVIDER == "whapi":
        # Confirm the exact host/path in your Whapi dashboard; this is the
        # standard text endpoint. Group id goes in `to` as the full JID.
        endpoint = "https://gate.whapi.cloud/messages/text"
        headers = {"Authorization": f"Bearer {PROVIDER_TOKEN}"}
        payload = {"to": GROUP_ID, "body": text}
    elif PROVIDER == "wassenger":
        endpoint = "https://api.wassenger.com/v1/messages"
        headers = {"Token": PROVIDER_TOKEN}
        payload = {"group": GROUP_ID, "message": text}
    else:
        raise ValueError(f"unknown PROVIDER: {PROVIDER}")

    try:
        r = requests.post(endpoint, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        return True
    except Exception as exc:
        log.error("send failed: %s", exc)
        return False


# --------------------------------------------------------------------------- #
# MAIN                                                                         #
# --------------------------------------------------------------------------- #

def run_once(dry_run=False):
    if not dry_run:
        # Each provider needs different credentials; check the right ones.
        missing = []
        if PROVIDER == "telegram":
            if not TELEGRAM_BOT_TOKEN:
                missing.append("TELEGRAM_BOT_TOKEN")
            if not TELEGRAM_CHAT_ID:
                missing.append("TELEGRAM_CHAT_ID")
        elif PROVIDER == "baileys":
            if not GROUP_ID:
                missing.append("GROUP_ID")
        else:  # whapi / wassenger
            if not PROVIDER_TOKEN:
                missing.append("PROVIDER_TOKEN")
            if not GROUP_ID:
                missing.append("GROUP_ID")
        if missing:
            log.error("PROVIDER=%s is missing required config: %s",
                      PROVIDER, ", ".join(missing))
            return

    conn = db()
    seen_this_run = set()       # in-memory: collapse same-batch syndicated copies
    seen_cnt = posted = 0
    for label, key, title, url, published in gather_items():
        seen_cnt += 1
        # Two keys: the source key (exact URL/id) catches re-fetches, the
        # content key (normalized headline) catches the same story syndicated
        # across outlets. The DB dedupes across runs; seen_this_run dedupes
        # within this batch (so it works even in --dry-run, which never writes
        # the DB). Skip if either key has been seen anywhere.
        ckey = content_key(title, url)
        if (key in seen_this_run or ckey in seen_this_run
                or already_seen(conn, key) or already_seen(conn, ckey)):
            continue
        seen_this_run.update((key, ckey))
        if not passes_score(title):
            if not dry_run:
                mark_seen(conn, key)    # seen but filtered out; don't re-score
                mark_seen(conn, ckey)
            continue
        verdict = newsletter_fit(title)
        if NEWSLETTER_MODE == "filter" and verdict is False:
            if not dry_run:
                mark_seen(conn, key)    # not a newsletter fit; drop and remember
                mark_seen(conn, ckey)
            continue
        msg = format_message(label, title, url, published)
        if NEWSLETTER_MODE == "tag" and verdict is True:
            msg = f"{NEWSLETTER_TAG}\n{msg}"
        if send_to_group(msg, dry_run):
            if not dry_run:
                mark_seen(conn, key)
                mark_seen(conn, ckey)
            posted += 1
            if MAX_POSTS_PER_RUN and posted >= MAX_POSTS_PER_RUN:
                log.info("reached per-run cap (%d); the rest will post next run",
                         MAX_POSTS_PER_RUN)
                break
            if not dry_run:
                time.sleep(SEND_DELAY)
    log.info(
        "run complete: source=%s fetched=%d posted=%d", SOURCE, seen_cnt, posted
    )


def main():
    ap = argparse.ArgumentParser(description="News -> WhatsApp group")
    ap.add_argument("--loop", action="store_true", help="run forever on an interval")
    ap.add_argument("--dry-run", action="store_true", help="print instead of sending")
    args = ap.parse_args()

    if args.loop:
        log.info("worker started, polling every %ds", LOOP_INTERVAL)
        while True:
            try:
                run_once(args.dry_run)
            except Exception as exc:
                log.exception("run errored: %s", exc)
            time.sleep(LOOP_INTERVAL)
    else:
        run_once(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
