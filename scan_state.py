"""Disk-backed persistence for in-progress (resumable) scans.

Streamlit ties a running script to the live browser websocket. When the screen
locks, the tab is suspended, or the network blips, that connection drops and the
script (and its ``st.session_state``) is torn down — losing a long scan midway.

To survive that, the scanner persists its progress here after every batch of
tickers. On reconnect (even in a brand-new session) the app can find the
interrupted scan on disk and resume from where it left off instead of starting
over.

Only lightweight data is stored: the per-ticker signal dicts have their heavy
``_df`` candle DataFrame stripped before pickling, so the progress files stay
small and fast to write each batch.
"""

from __future__ import annotations

import os
import time
import pickle
import hashlib
from typing import Any, Optional

_DIR = os.path.join(os.path.dirname(__file__), ".scancache")

# A running scan older than this (no batch saved within the window) is treated as
# abandoned and will not be auto-resumed.
RESUME_MAX_AGE = 60 * 60  # 1 hour


def _ensure_dir() -> None:
    os.makedirs(_DIR, exist_ok=True)


def _path(ck: str) -> str:
    h = hashlib.md5(ck.encode("utf-8")).hexdigest()[:16]
    return os.path.join(_DIR, f"scan_{h}.pkl")


def _strip_results(results: list[dict]) -> list[dict]:
    """Return copies of result dicts without the heavy ``_df`` candle frame."""
    out = []
    for r in results:
        if "_df" in r:
            r = {k: v for k, v in r.items() if k != "_df"}
        out.append(r)
    return out


def save(state: dict) -> None:
    """Persist a scan-progress dict. ``_df`` frames are dropped before writing."""
    _ensure_dir()
    payload = dict(state)
    payload["updated"] = time.time()
    payload["results"] = _strip_results(payload.get("results", []))
    tmp = _path(state["ck"]) + ".tmp"
    try:
        with open(tmp, "wb") as fh:
            pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, _path(state["ck"]))
    except Exception:
        # Persistence is best-effort — a failed write must never break a scan.
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def load(ck: str) -> Optional[dict]:
    p = _path(ck)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "rb") as fh:
            return pickle.load(fh)
    except Exception:
        return None


def clear(ck: str) -> None:
    try:
        p = _path(ck)
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


def _iter_states():
    if not os.path.isdir(_DIR):
        return
    for name in os.listdir(_DIR):
        if not (name.startswith("scan_") and name.endswith(".pkl")):
            continue
        try:
            with open(os.path.join(_DIR, name), "rb") as fh:
                yield pickle.load(fh)
        except Exception:
            continue


def load_active(max_age: float = RESUME_MAX_AGE) -> Optional[dict]:
    """Return the most recently-updated *running* scan still within ``max_age``.

    Stale running files (older than ``max_age``) are cleaned up so they don't
    keep prompting a resume forever.
    """
    now = time.time()
    best: Optional[dict] = None
    for st_ in _iter_states():
        if st_.get("status") != "running":
            continue
        age = now - st_.get("updated", 0)
        if age > max_age:
            clear(st_.get("ck", ""))
            continue
        if best is None or st_.get("updated", 0) > best.get("updated", 0):
            best = st_
    return best


def info() -> dict:
    """Summary of persisted scan files (for an optional UI/debug readout)."""
    files = 0
    running = 0
    for st_ in _iter_states():
        files += 1
        if st_.get("status") == "running":
            running += 1
    return {"files": files, "running": running, "dir": _DIR}
