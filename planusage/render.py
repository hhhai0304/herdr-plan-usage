"""Rendering: one line for the tab bar, one screen for the popup, one for setup."""
from __future__ import annotations

import json
import os
import re
import subprocess

from .model import (fmt_ago, fmt_clock, fmt_left, headline, label_of, next_reset, pct,
                    select, windows_now)
from .providers import visible

TIMED = ("session", "daily", "weekly", "monthly")

RGB = {
    "fg": (192, 202, 245), "dim": (110, 118, 150), "track": (59, 66, 97),
    "green": (158, 206, 106), "yellow": (224, 175, 104), "red": (247, 118, 142),
    "claude": (217, 119, 87), "codex": (236, 236, 241), "opencode": (203, 166, 247),
    "openrouter": (125, 196, 255), "deepseek": (86, 156, 214), "antigravity": (150, 210, 130),
    "custom": (180, 180, 200),
}

ANSI = re.compile(r"\x1b\[[0-9;]*m")

ERROR_TEXT = {
    "expired": "login expired \u2014 open {cli} to renew it",
    "limited": "rate limited (429) \u2014 retrying later",
    "unentitled": "{detail}",
    "nologin": "not signed in",
    "unsupported": "{detail}",
    "http": "HTTP {status}",
    "network": "network error",
    "format": "unexpected response format",
    "unexpected": "unexpected error: {name}",
}

STATE_TEXT = {
    "ready": "ready",
    "nologin": "installed, not signed in",
    "absent": "not on this machine",
    "expired": "login expired",
    "unentitled": "no active subscription",
    "unsupported": "no usage API for this account",
}


def fg(name, text):
    colour = RGB.get(name) or RGB["fg"]
    return "\x1b[38;2;%d;%d;%dm%s\x1b[39m" % (*colour, text)


def bold(text):
    return f"\x1b[1m{text}\x1b[22m"


def sev(used):
    return "green" if used < 50 else "yellow" if used < 80 else "red"


def width(text):
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in ANSI.sub("", text))


def pad(text, size):
    return text + " " * max(0, size - width(text))


def cut(text, limit):
    """Truncate to `limit` columns (plain text only) so a long reason cannot wrap a row."""
    if limit <= 0:
        return ""
    if width(text) <= limit:
        return text
    return text[:max(1, limit - 1)] + "\u2026"


def row(left, right, screen_width):
    gap = max(1, screen_width - 4 - width(left) - width(right))
    return "  " + left + " " * gap + right


def error_of(state):
    err = state.get("error")
    return err if isinstance(err, dict) else None


def error_text(err):
    code = err.get("code")
    template = ERROR_TEXT.get(code)
    if not template:
        return f"unexpected error: {code}"
    try:
        return template.format(**err)
    except (KeyError, IndexError):
        return template.replace("{detail}", str(err.get("detail") or code))


def provider_label(provider, settings):
    label = settings.label(provider.id, provider.short)
    return f"{provider.icon} {label}" if settings.icons == "nerd" else label


# ── tab bar ──────────────────────────────────────────────────────────

AGENT_PROVIDER = {"claude": "claude", "codex": "codex", "opencode": "opencode",
                  "agy": "antigravity", "antigravity": "antigravity"}


def focused_provider():
    """The provider whose agent runs in the focused herdr pane, if any.

    Best effort by design: no focused pane, no herdr binary or no agent in the
    pane all mean "no opinion", never an error.
    """
    binary = os.environ.get("HERDR_BIN_PATH") or "herdr"
    try:
        panes = json.loads(subprocess.run([binary, "pane", "list"], capture_output=True,
                                          text=True, timeout=3).stdout)["result"]["panes"]
        pane = next((p for p in panes if p.get("focused")), None)
        if not pane:
            return None
        info = json.loads(subprocess.run([binary, "pane", "process-info", "--pane", pane["pane_id"]],
                                         capture_output=True, text=True, timeout=3).stdout)
        processes = info["result"]["process_info"]["foreground_processes"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None
    for process in processes:
        name = (process.get("name") or "").split(".")[0].lower()
        if name in AGENT_PROVIDER:
            return AGENT_PROVIDER[name]
    return None


def ordered(providers, settings, demo=False):
    """Provider order the tab bar and the popup use, after the `focus` rule."""
    shown = [p for p in providers if demo or visible(p, settings)]
    if settings.focus == "all":
        return shown
    focused = focused_provider()
    if not focused:
        return shown
    if settings.focus == "agent-only":
        only = [p for p in shown if p.id == focused or p.id.startswith(f"{focused}2")]
        return only or shown
    first = [p for p in shown if p.id == focused]
    return first + [p for p in shown if p.id != focused]


def provider_part(provider, state, now, settings):
    """One tab bar segment: `GPT 12/2 4h28m`, `DS $6.43`, or None when there is nothing.

    A timed quota reads as a percentage; a money meter reads as the amount, which is
    the actionable number (the percentage of a $1 budget rounds to 0).
    """
    wins = select(windows_now(state, now), settings.windows(provider.id))
    timed = [w for w in wins if w["kind"] in TIMED and isinstance(w.get("used"), (int, float))]
    paid = [w for w in wins if w["kind"] not in TIMED and w.get("amount")]
    if not timed and not paid:
        return None
    stale = "~" if state.get("fetched_at") and now - state["fetched_at"] > settings.stale_after else ""
    pieces = []
    if timed:
        numbers = "/".join(pct(w) for w in timed)
        pieces.append(numbers + ("%" if len(timed) == 1 else ""))
    pieces += [w["amount"] for w in paid]
    text = provider_label(provider, settings) + " " + stale + " ".join(pieces)
    reset = next_reset(wins)
    if reset:
        text += " " + fmt_left(reset, now)
    return text


def bar_text(providers, data, now, settings, demo=False):
    parts = []
    for provider in ordered(providers, settings, demo):
        if not settings.in_bar(provider.id):
            continue
        state = data.get(provider.id) or {}
        part = provider_part(provider, state, now, settings)
        if part is None and settings.bar_errors and error_of(state):
            parts.append(f"{provider_label(provider, settings)} \u26a0")
        elif part:
            parts.append(part)
    return settings.separator.join(parts)


# ── popup ────────────────────────────────────────────────────────────

def window_row(w, now, screen_width, label_width=16):
    used = w["used"] if isinstance(w.get("used"), (int, float)) else None
    size = max(6, screen_width - 34 - label_width)
    if used is None:
        return ("    " + fg("dim", pad(label_of(w), label_width)) + " "
                + fg("fg", w.get("amount") or ""))
    filled = max(1 if used > 0 else 0, round(size * min(100, used) / 100))
    track = fg(sev(used), "\u2501" * filled) + fg("track", "\u2501" * (size - filled))
    reset = (f"{fmt_left(w['resets_at'], now)} \u00b7 {fmt_clock(w['resets_at'], now)}"
             if w.get("resets_at") else w.get("amount") or "")
    return ("    " + fg("dim", pad(label_of(w), label_width)) + " " + track + " "
            + fg("fg", f"{round(used):>3}%") + "  " + fg("dim", reset))


def open_problems(providers, data, settings, demo=False):
    """Providers the user believes are on but that have no reading to show."""
    problems = []
    for provider in providers:
        if not (demo or visible(provider, settings)):
            continue
        state = data.get(provider.id) or {}
        if error_of(state) or not windows_now(state, 0):
            problems.append(provider.id)
    return problems


def label_width(providers, data, now):
    """Widen the window label column when a provider names its meters (Antigravity
    reports one weekly quota per model group). The bars give up the space."""
    longest = 0
    for provider in providers:
        for w in windows_now(data.get(provider.id) or {}, now):
            longest = max(longest, width(label_of(w)))
    return min(26, max(16, longest))


def panel_lines(providers, data, now, mode, busy, screen_width, settings, demo=False):
    shown = ordered(providers, settings, demo)
    problems = open_problems(providers, data, settings, demo)
    label_w = label_width(shown, data, now)
    segment = "".join(
        "\x1b[48;2;41;46;66m" + bold(fg("fg", f" {name} ")) + "\x1b[49m" if name == mode
        else fg("dim", f" {name} ") for name in ("Detail", "Compact"))
    lines = [row(fg("dim", "\u21bb r"), segment, screen_width), ""]
    if problems:
        noun = "agent" if len(problems) == 1 else "agents"
        lines += [fg("yellow", f"  \u26a0 {len(problems)} {noun} without a reading \u2014 see below"),
                  ""]
    for provider in shown:
        state = data.get(provider.id) or {}
        wins = windows_now(state, now)
        title = (bold(fg("fg", provider.name))
                 + ("  " + fg("dim", state["plan"]) if state.get("plan") else "")
                 + ("  " + fg("dim", state["account"]) if state.get("account") else ""))
        if wins:
            lead = headline(wins)
            reset = fg("dim", fmt_left(lead["resets_at"], now)) + "  " if lead and lead.get("resets_at") else ""
            if isinstance(lead.get("used"), (int, float)):
                value = bold(fg(sev(lead["used"]), f"{round(lead['used']):>3}%"))
            else:
                value = bold(fg("fg", lead.get("amount") or ""))
            lines.append(row(title, reset + value, screen_width))
            if mode == "detail":
                lines += [window_row(w, now, screen_width, label_w) for w in wins]
        else:
            lines.append(row(title, fg("dim", "loading\u2026" if busy else "\u2013"), screen_width))
        err = error_of(state)
        if err and (mode == "detail" or not wins):
            old = (fg("dim", " \u00b7 value from " + fmt_ago(state["fetched_at"], now))
                   if state.get("fetched_at") else "")
            lines.append("    " + fg("red", "\u26a0 " + error_text(err)) + old)
        if mode == "detail":
            lines.append("")
    fetched = [data[p.id]["fetched_at"] for p in providers if (data.get(p.id) or {}).get("fetched_at")]
    status = ("refreshing\u2026" if busy
              else f"updated {fmt_ago(min(fetched), now)}" if fetched else "")
    hint = "Tab view \u00b7 r refresh \u00b7 q close"
    lines.append("  " + fg("dim", " \u00b7 ".join(x for x in (status, hint) if x)))
    return lines


# ── setup ────────────────────────────────────────────────────────────

def setup_lines(rows, cursor, note, screen_width):
    lines = ["", "  " + bold(fg("fg", "Plan Usage")) + "  "
             + fg("dim", "agents signed in on this machine"), ""]
    for index, item in enumerate(rows):
        mark = "x" if item["enabled"] else " "
        pointer = "\u25b8" if index == cursor else " "
        name = pad(item["name"][:12], 12)
        plan = pad((item["plan"] or "\u2013")[:10], 10)
        state = item["state"]
        hidden = "hidden \u00b7 " if not item["enabled"] else ""
        if state != "ready":
            reason = item.get("problem") or STATE_TEXT.get(state, state)
            tail = hidden + reason
            colour = "red" if state in ("expired", "unentitled", "unsupported", "nologin") else "dim"
        elif not item["enabled"]:
            # ready but switched off: say so, otherwise the row looks like it is showing
            tail, colour = f'hidden \u00b7 label "{item["label"]}"', "dim"
        else:
            tail, colour = f'label "{item["label"]}" \u00b7 bar {"on" if item["bar"] else "off"}', "dim"
        plain = f" {pointer} [{mark}] {name} {plan}"
        head = (fg("fg" if index == cursor else "dim", f" {pointer} ")
                + fg("fg", f"[{mark}]") + " " + fg(item["color"], name) + " " + fg("dim", plan))
        available = max(0, screen_width - 4 - width(plain) - 2)  # two columns of right margin
        clipped = cut(tail, available)
        gap = max(1, screen_width - 2 - width(plain) - width(clipped) - 2)
        lines.append("  " + head + " " * gap + fg(colour, clipped))
    lines += ["", "  " + fg("green", note) if note else "",
              "  " + fg("dim", cut("\u2191\u2193 move \u00b7 space show/hide \u00b7 b tab bar \u00b7 l label "
                                  "\u00b7 r refresh \u00b7 c snippet \u00b7 q close", screen_width - 2))]
    return lines
