"""
Watchlist persistence
=====================
A small JSON-backed store for the stocks the user wants to monitor. Each entry
records the snapshot at the moment it was added (date, price, signal, score) so
the UI can later show how the stock has moved and whether its signal flipped.

Storage is a plain JSON file next to this module. On a normal machine it
persists indefinitely. On Streamlit Community Cloud the filesystem is ephemeral
(it resets on reboot/redeploy), so the UI also offers export/import to keep a
permanent copy.

An entry is uniquely identified by (ticker, interval) so the same stock can be
tracked on more than one timeframe.
"""

import json
import os
from datetime import datetime

WATCHLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.json")


def load_watchlist() -> list[dict]:
    """Return the saved watchlist, or an empty list if none/unreadable."""
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def save_watchlist(items: list[dict]) -> None:
    """Persist the full watchlist to disk."""
    try:
        with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2)
    except Exception:
        pass


def _key(ticker: str, interval: str) -> tuple[str, str]:
    return (ticker.upper().strip(), interval)


def is_watched(ticker: str, interval: str) -> bool:
    k = _key(ticker, interval)
    return any((it.get("ticker"), it.get("interval")) == k for it in load_watchlist())


def add_to_watchlist(ticker: str, price: float, signal: str,
                     score: float, interval: str) -> tuple[bool, str]:
    """
    Add a stock snapshot to the watchlist.
    Returns (added, message). `added` is False if it was already present.
    """
    items = load_watchlist()
    t = ticker.upper().strip()
    if any((it.get("ticker"), it.get("interval")) == (t, interval) for it in items):
        return False, f"{t} ({interval}) is already in your watchlist."

    items.append({
        "ticker":       t,
        "interval":     interval,
        "added_at":     datetime.now().strftime("%Y-%m-%d %H:%M"),
        "added_price":  round(float(price), 2),
        "added_signal": signal,
        "added_score":  round(float(score), 1),
    })
    save_watchlist(items)
    return True, f"Added {t} ({interval}) to your watchlist."


def remove_from_watchlist(ticker: str, interval: str) -> None:
    k = _key(ticker, interval)
    items = [it for it in load_watchlist()
             if (it.get("ticker"), it.get("interval")) != k]
    save_watchlist(items)


def clear_watchlist() -> None:
    save_watchlist([])
