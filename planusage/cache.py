"""The usage cache: one JSON file, one lock, one deadline per refresh round.

The tab bar runs every 15 s but must not hit six endpoints that often, so a fetch
is at most one per provider per `BAR_MAX_AGE`. The attempt is recorded *before* the
network call, so a fetch that hangs or gets killed still counts and nothing retries
early. Only the file access is locked — the network calls happen outside the lock,
so the tab bar never waits on another process's fetch.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

from . import config
from .model import FetchError, window
from .providers import visible

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

BAR_MAX_AGE = 300     # the tab bar redraws every 15 s; this is the floor between fetches
PANEL_MAX_AGE = 120   # the popup is the user watching: fresher, but still gentle
EVENT_MIN_AGE = 120   # an agent turn just ended, so usage moved
MANUAL_MIN_AGE = 30   # `r` in the popup and the refresh action
FETCH_DEADLINE = 15   # herdr kills a tab bar command at 25 s


def cache_file():
    return config.cache_dir() / "usage.json"


def lock_file():
    return config.cache_dir() / "fetch.lock"


def load():
    try:
        data = json.loads(cache_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {pid: st for pid, st in data.items() if isinstance(st, dict)}


def save(data):
    path = cache_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)  # the tab bar may read at any moment; never half a file


class _Lock:
    def __enter__(self):
        config.cache_dir().mkdir(parents=True, exist_ok=True)
        self.handle = open(lock_file(), "a+b")
        if sys.platform == "win32":
            self.handle.seek(0)
            while True:  # LK_LOCK gives up after ~10 s; a slow fetch must not break it
                try:
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        else:
            fcntl.flock(self.handle, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if sys.platform == "win32":
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()
        return False


def attempt(provider):
    """Fetch one provider. A failure keeps the last good reading and records the reason."""
    now = time.time()
    try:
        result = provider.fetch()
        return {**result, "fetched_at": now, "attempt_at": now, "error": None, "retry_at": 0}
    except FetchError as e:
        return {"attempt_at": now, "error": {"code": e.code, **e.params},
                "retry_at": now + e.retry_after}
    except Exception as e:  # a provider bug must not blank the whole tab bar
        return {"attempt_at": now, "error": {"code": "unexpected", "name": type(e).__name__},
                "retry_at": 0}


def fetch_all(providers):
    """Fetch every provider in a daemon thread with one overall deadline. Daemon
    threads never hold up exit, so closing the popup mid-fetch is instant."""
    results = {}
    threads = [threading.Thread(target=lambda p=p: results.__setitem__(p.id, attempt(p)), daemon=True)
               for p in providers]
    for thread in threads:
        thread.start()
    end = time.time() + FETCH_DEADLINE
    for thread in threads:
        thread.join(max(0, end - time.time()))
    now = time.time()
    return {p.id: results.get(p.id) or {"attempt_at": now, "error": {"code": "network"}, "retry_at": 0}
            for p in providers}


def refresh(providers, max_age, settings, include_disabled=False):
    active = [p for p in providers if include_disabled or visible(p, settings)]
    if not active:
        return load()
    now = time.time()
    with _Lock():
        data = load()
        due = [p for p in active
               if now - data.get(p.id, {}).get("attempt_at", 0) >= max_age
               and now >= data.get(p.id, {}).get("retry_at", 0)]
        if not due:
            return data
        for p in due:
            data[p.id] = {**data.get(p.id, {}), "attempt_at": now}
        save(data)
    results = fetch_all(due)
    with _Lock():
        data = load()
        for pid, result in results.items():
            data[pid] = {**data.get(pid, {}), **result}
        save(data)
        return data


def demo(providers, now=None):
    """Plausible numbers for every provider without reading a credential or a network."""
    now = now or time.time()
    hour, day = 3600, 86400
    sample = {
        "codex": {"plan": "Plus", "account": "you@example.com", "windows": [
            window("session", used=12, resets_at=now + 4 * hour + 28 * 60),
            window("weekly", used=2, resets_at=now + 6 * day + 18 * hour)]},
        "opencode": {"plan": "Go", "windows": [
            window("session", used=0, resets_at=now + 4 * hour + 58 * 60),
            window("weekly", used=3, resets_at=now + 6 * day + 16 * hour),
            window("monthly", used=15, resets_at=now + 23 * day + 4 * hour)]},
        "openrouter": {"plan": "", "windows": [
            window("credits", used=0, resets_at=now + 6 * day,
                   amount="$0.01 / $1.00")]},
        "deepseek": {"plan": "", "windows": [window("balance", amount="$6.43")]},
        "antigravity": {"plan": "Free", "windows": [
            window("weekly", used=38, model="Claude and GPT models",
                   resets_at=now + 6 * day + 20 * hour)]},
    }
    out = {}
    for p in providers:
        base = sample.get(p.id) or sample.get(p.id.split(".")[0]) or {
            "plan": "", "windows": [window("weekly", used=41, resets_at=now + 2 * day)]}
        out[p.id] = {**base, "fetched_at": now - 20, "attempt_at": now - 20, "error": None}
    return out
