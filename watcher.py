"""Watcher Bot: checks web pages you choose and sends a Telegram alert when they change.

Setup steps are in README.md. Secrets (Telegram token, optional Gemini key) are read
from environment variables and must never be written into this file.
"""
import difflib
import json
import os
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
STATE = ROOT / "state"
STATE.mkdir(exist_ok=True)
UA = "Mozilla/5.0 (compatible; WatcherBot/1.0; personal study project)"
DRY_RUN = os.environ.get("DRY_RUN") == "1"
FAIL_ALERT_AFTER = 3  # tell me once if a page fails this many runs in a row


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "page"


def fetch_lines(url, selector=None, ignore=()):
    """Download a page and return its visible text as a clean list of lines."""
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    if r.encoding in (None, "ISO-8859-1"):
        r.encoding = r.apparent_encoding
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    root = soup.select_one(selector) if selector else soup
    if root is None:
        raise ValueError(f"selector not found: {selector}")
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in root.get_text("\n").splitlines()]
    patterns = [re.compile(p, re.I) for p in ignore]
    return [ln for ln in lines if ln and not any(p.search(ln) for p in patterns)]


def changed_lines(old, new, limit=40):
    diff = difflib.unified_diff(old, new, lineterm="", n=0)
    out = [d for d in diff if d[:1] in "+-" and not d.startswith(("+++", "---"))]
    return out[:limit]


def ai_summary(name, url, changes):
    """Optional: ask Gemini to summarize the change. Returns None if not configured."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key or not changes:
        return None
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")  # check AI Studio for current names
    prompt = (
        "These lines changed on a web page I monitor (+ added, - removed).\n"
        f"Page: {name}\n\n" + "\n".join(changes) +
        "\n\nIn at most 3 short bullet points, say what changed. Highlight any deadline, "
        "eligibility rule, fee or date. If it looks like trivial noise, say so. "
        "Do not guess anything that is not in the lines."
    )
    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:  # the bot must keep working without AI
        print(f"[warn] AI summary failed: {type(e).__name__}")
        return None


def send_telegram(text):
    text = text[:3900]
    if DRY_RUN:
        print("---- TELEGRAM (dry run) ----\n" + text + "\n----------------------------")
        return
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"Telegram error {r.status_code}")


def check(item):
    name, url = item["name"], item["url"]
    base = STATE / slug(name)
    snap, fail = base.with_suffix(".txt"), base.with_suffix(".fail")
    try:
        new = fetch_lines(url, item.get("selector"), item.get("ignore", []))
        if not new:
            raise ValueError("page returned no text")
    except Exception as e:
        n = int(fail.read_text() or 0) + 1 if fail.exists() else 1
        fail.write_text(str(n))
        print(f"[fail] {name}: {type(e).__name__} (#{n})")
        if n == FAIL_ALERT_AFTER:
            send_telegram(f"⚠️ Watcher Bot cannot read '{name}' ({FAIL_ALERT_AFTER} runs in a row).\n{url}")
        return
    if fail.exists():
        fail.unlink()
    if not snap.exists():
        snap.write_text("\n".join(new))
        print(f"[baseline] {name}: saved {len(new)} lines")
        return
    old = snap.read_text().splitlines()
    if old == new:
        print(f"[same] {name}")
        return
    changes = changed_lines(old, new)
    snap.write_text("\n".join(new))
    keywords = [k.lower() for k in item.get("keywords", [])]
    if keywords and not any(k in c.lower() for c in changes for k in keywords):
        print(f"[quiet] {name}: changed, but no keyword matched")
        return
    summary = ai_summary(name, url, changes)
    msg = f"🔔 Change detected: {name}\n{url}\n"
    if summary:
        msg += f"\nAI summary (verify on the page):\n{summary}\n"
    msg += "\nChanged lines:\n" + "\n".join(changes)
    send_telegram(msg)
    print(f"[alert] {name}: sent")


def main():
    if "--test" in sys.argv:
        send_telegram("✅ Watcher Bot test: your Telegram connection works.")
        return
    items = json.loads((ROOT / "urls.json").read_text(encoding="utf-8"))
    for item in items:
        if item.get("enabled", True) and item.get("url", "").startswith("http"):
            check(item)


if __name__ == "__main__":
    main()
