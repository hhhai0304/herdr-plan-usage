"""Normalized usage model shared by every provider.

A provider answers with a *plan* name, an optional account label, and a list of
*windows*. A window is one meter:

  session / daily / weekly / monthly  a timed quota, `used` is a percentage
  credits                             a pool metered in money, percentage + amount
  spend                               money spent, no percentage
  balance                             prepaid money left, no percentage

Every window carries `resets_at` when the provider knows when it starts over.
"""
from __future__ import annotations

import re
from datetime import datetime

# Python 3.9 takes at most 6 fractional digits; agy writes 9 of them.
_FRACTION = re.compile(r"(\.\d{6})\d+")

KIND_LABELS = {
    "session": "5h",
    "daily": "Daily",
    "weekly": "Weekly",
    "monthly": "Monthly",
    "credits": "Credits",
    "spend": "Spent",
    "balance": "Balance",
}

PERCENT_KINDS = ("session", "daily", "weekly", "monthly", "credits")

SYMBOL = {"USD": "$", "EUR": "\u20ac", "GBP": "\u00a3", "JPY": "\u00a5", "CNY": "\u00a5"}

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class FetchError(Exception):
    """A provider could not read a meter. `code` is the word the popup explains;
    `retry_after` keeps the scheduler from hammering an endpoint that said 429."""

    def __init__(self, code, retry_after=0, **params):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after
        self.params = params


def window(kind, used=None, resets_at=None, model=None, label=None, amount=None):
    """One meter. `used` is a percentage, `amount` a ready-to-print money string."""
    return {
        "kind": kind,
        "used": used,
        "resets_at": resets_at,
        "model": model,
        "label": label,
        "amount": amount,
    }


def to_epoch(value):
    """Epoch seconds from seconds, milliseconds, an ISO 8601 string, or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        return value / 1000 if value > 1e11 else float(value)
    if isinstance(value, str) and value:
        text = _FRACTION.sub(r"\1", value.replace("Z", "+00:00"))
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return None
    return None


def money(amount, currency=None):
    """A money string: `$6.43`, `12.50 CNY`, or None when the amount is unknown."""
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return None
    cur = (currency or "").upper()
    symbol = SYMBOL.get(cur)
    if symbol:
        return f"{symbol}{value:,.2f}"
    return f"{value:,.2f} {cur}".strip()


def windows_now(state, now):
    """Windows with a reset time in the past have started over: they read as 0.

    A malformed entry is dropped rather than crashing the tab bar.
    """
    wins = state.get("windows") if isinstance(state.get("windows"), list) else []
    out = []
    for w in wins:
        if not isinstance(w, dict):
            continue
        if w.get("kind") not in KIND_LABELS:
            continue
        if w.get("used") is None and not w.get("amount"):
            continue
        if w.get("resets_at") and w["resets_at"] <= now and w.get("used") is not None:
            w = {**w, "used": 0, "resets_at": None, "amount": None}
        out.append(w)
    return out


def headline(wins):
    """The window closest to its limit — what the tab bar leads with."""
    pct = [w for w in wins if isinstance(w.get("used"), (int, float))]
    if not pct:
        return wins[0] if wins else None
    return max(pct, key=lambda w: (w["used"], w.get("resets_at") or 0))


def label_of(w):
    if w.get("label"):
        return w["label"]
    base = KIND_LABELS.get(w.get("kind"), str(w.get("kind")))
    return f"{base} \u00b7 {w['model']}" if w.get("model") else base


def matches(w, token):
    """Does this window answer to a config token (kind, label or model name)?"""
    token = token.strip().lower()
    if not token:
        return False
    for candidate in (w.get("kind"), label_of(w), w.get("model")):
        if candidate and str(candidate).lower() == token:
            return True
    return False


def select(wins, wanted):
    """Windows the user asked for, in provider order. Empty `wanted` means all."""
    if not wanted:
        return list(wins)
    picked = [w for w in wins if any(matches(w, t) for t in wanted)]
    return picked or list(wins)


def pct(w):
    """Two digits while there is room: 03, 39, 100."""
    value = round(w.get("used") or 0)
    return f"{value:02d}" if value < 100 else "100"


def split_left(seconds):
    """Compact countdown: 42m, 4h28m, 6d18h."""
    s = max(0, int(seconds))
    days, hours, minutes = s // 86400, s % 86400 // 3600, s % 3600 // 60
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"


def fmt_left(ts, now):
    return split_left(ts - now) if ts else ""


def fmt_clock(ts, now):
    """The reset on the user's own clock: `Today 19:40`, `Mon 05:09`."""
    at = datetime.fromtimestamp(ts)
    day = "Today" if at.date() == datetime.fromtimestamp(now).date() else WEEKDAYS[at.weekday()]
    return f"{day} {at:%H:%M}"


def fmt_ago(ts, now):
    s = int(now - ts)
    if s < 10:
        return "just now"
    if s < 60:
        return f"{s}s ago"
    return f"{s // 60}m ago" if s < 3600 else f"{s // 3600}h ago"


def next_reset(wins):
    stamps = [w["resets_at"] for w in wins if w.get("resets_at")]
    return min(stamps) if stamps else None
