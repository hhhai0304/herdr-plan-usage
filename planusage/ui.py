"""The two interactive screens: the usage popup and the setup pane.

Keys work on POSIX (termios) and on Windows (msvcrt), because herdr runs the popup
in whatever console it has.
"""
from __future__ import annotations

import json
import os
import select
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import cache, config, render
from .config import PLUGIN_ID
from .providers import visible

if sys.platform == "win32":
    import msvcrt
else:
    import termios
    import tty

QUIT = (b"q", b"Q", b"\x1b", b"\x03")
UP = (b"k", b"\x1b[A")
DOWN = (b"j", b"\x1b[B")

# Windows consoles deliver arrows as two characters; map them to the POSIX bytes.
WINDOWS_KEYS = {"\xe0M": b"\x1b[C", "\xe0K": b"\x1b[D", "\xe0H": b"\x1b[A", "\xe0P": b"\x1b[B",
                "\x00\x0f": b"\x1b[Z"}


def decode_windows_key(ch, nxt=None):
    if ch in ("\x00", "\xe0"):
        return WINDOWS_KEYS.get(ch + (nxt or ""), b"")
    return ch.encode("utf-8", "replace")


def windows_key(timeout):
    deadline = time.time() + timeout
    while not msvcrt.kbhit():
        if time.time() >= deadline:
            return b""
        time.sleep(0.03)
    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):
        return decode_windows_key(ch, msvcrt.getwch())
    return decode_windows_key(ch)


class Screen:
    """Alternate screen in cbreak mode, restored on exit."""

    win = sys.platform == "win32"

    def __enter__(self):
        self.fd = sys.stdin.fileno()
        if not self.win:  # Windows reads keys through msvcrt; there is no cbreak to set
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[?7l")  # alt screen · hide cursor · no autowrap
        sys.stdout.flush()
        return self

    def __exit__(self, *exc):
        sys.stdout.write("\x1b[?7h\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        if not self.win:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        return exc[0] is KeyboardInterrupt

    def draw(self, lines):
        _, height = shutil.get_terminal_size()
        footer = lines[-1]
        lines = (lines[:-1] + [""] * height)[:height - 1] + [footer]  # key hints stick to bottom
        sys.stdout.write("\x1b[H" + "\r\n".join(line + "\x1b[K" for line in lines))
        sys.stdout.flush()

    def key(self, timeout):
        if self.win:
            return windows_key(timeout)
        return os.read(self.fd, 32) if select.select([self.fd], [], [], timeout)[0] else b""


class Refresher:
    """Fetch in the background so the screen keeps drawing while the network works."""

    def __init__(self, providers, settings):
        self.providers, self.settings, self.busy = providers, settings, False

    def kick(self, max_age):
        if self.busy:
            return
        self.busy = True

        def run():
            try:
                cache.refresh(self.providers, max_age, self.settings)
            finally:
                self.busy = False
        threading.Thread(target=run, daemon=True).start()


def size():
    return shutil.get_terminal_size().columns


# ── popup ────────────────────────────────────────────────────────────

def panel(settings, providers, demo=False):
    ui = {"screen": "usage", "cursor": 0, "note": ""}
    refresher = Refresher(providers, settings)
    with Screen() as screen:
        if not demo:
            refresher.kick(cache.PANEL_MAX_AGE)
        last = time.time()
        while True:
            now = time.time()
            if not demo and now - last >= cache.PANEL_MAX_AGE:
                refresher.kick(cache.PANEL_MAX_AGE)
                last = now
            data = cache.demo(providers) if demo else cache.load()
            if ui["screen"] == "setup":
                rows = rows_of(providers, settings, data, demo)
                ui["cursor"] = max(0, min(ui["cursor"], len(rows) - 1))
                screen.draw(render.setup_lines(rows, ui["cursor"], ui["note"], size()))
            else:
                screen.draw(render.panel_lines(providers, data, now, refresher.busy,
                                               size(), settings, demo))
            ui["note"] = ""
            key = screen.key(0.5)
            if key in QUIT:
                break
            if ui["screen"] == "setup":
                rows = rows_of(providers, settings, cache.demo(providers) if demo else cache.load(), demo)
                ui["cursor"], ui["note"] = setup_key(key, rows, ui["cursor"], settings, refresher, screen)
                if key in (b"s", b"S"):
                    ui["screen"], ui["cursor"] = "usage", 0
            elif key in (b"s", b"S"):
                ui["screen"], ui["cursor"], ui["note"] = "setup", 0, ""
            elif key in (b"r", b"R"):
                refresher.kick(cache.MANUAL_MIN_AGE)
                last = time.time()


# ── setup ────────────────────────────────────────────────────────────

def setup_key(key, rows, cursor, settings, refresher, screen):
    """One key on the settings screen. Returns (cursor, note); the caller decides
    whether `s` leaves for the usage screen."""
    if key in UP:
        return cursor - 1, ""
    if key in DOWN:
        return cursor + 1, ""
    if not rows:
        return cursor, ""
    row = rows[cursor]
    if key == b" ":
        settings.set_provider(row["id"], enabled=not row["enabled"])
        settings.save()
        if not row["enabled"]:  # just switched on: it may never have been fetched
            refresher.kick(cache.MANUAL_MIN_AGE)
    elif key in (b"b", b"B"):
        settings.set_provider(row["id"], bar=not row["bar"])
        settings.save()
    elif key in (b"l", b"L"):
        typed = edit_label(screen, row["label"])
        if typed is not None:
            settings.set_provider(row["id"], label=typed or None)
            settings.save()
    elif key in (b"r", b"R"):
        refresher.kick(cache.MANUAL_MIN_AGE)
    elif key in (b"c", b"C"):
        return cursor, "Copied." if copy(snippet()) else "No clipboard tool found."
    return cursor, ""


def rows_of(providers, settings, data, demo=False):
    rows = []
    for provider in providers:
        detected = provider.detect()
        state = detected["state"]
        problem = None
        stored = data.get(provider.id) or {}
        err = render.error_of(stored)
        if err:
            state = err.get("code") or "http"
            problem = render.error_text(err)
        elif state == "ready" and not stored.get("windows"):
            problem = detected.get("detail")
        rows.append({
            "id": provider.id,
            "name": provider.name,
            "color": provider.color,
            "enabled": True if demo else visible(provider, settings),
            "bar": settings.in_bar(provider.id),
            "label": settings.label(provider.id, provider.short),
            "plan": stored.get("plan") or "",
            "state": state,
            "problem": problem,
            "source": detected.get("detail") or "",
        })
    return rows


def snippet():
    path = shim_path()
    command = json_quote(shlex.quote(str(path)) if sys.platform != "win32" else f'"{path}"')
    return (
        "[ui]\n"
        "tab_bar_right = [\n"
        f'  {{ type = "command", command = {command}, interval_seconds = 15, timeout_seconds = 25 }},\n'
        "]\n\n"
        "[[keys.command]]\n"
        'key = "prefix+u"\n'
        'type = "plugin_action"\n'
        f'command = "{PLUGIN_ID}.open"\n'
        'description = "plan usage"\n'
    )


def json_quote(text):
    return json.dumps(text)


def copy(text):
    for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"],
                ["xsel", "-b", "-i"], ["clip"]):
        if shutil.which(cmd[0]):
            try:
                return subprocess.run(cmd, input=text, text=True).returncode == 0
            except OSError:
                continue
    return False


def edit_label(screen, current):
    """Inline editor for one label. Enter keeps it, Esc drops it, empty resets to default."""
    text = list(current)
    while True:
        prompt = ("  " + render.fg("dim", "label> ") + "".join(text)
                  + "\x1b[7m \x1b[27m" + render.fg("dim", "   Enter keep · Esc cancel"))
        screen.draw(["", prompt, ""])
        key = screen.key(0.5)
        if key in (b"\r", b"\n"):
            return "".join(text).strip()
        if key == b"\x1b" or key in (b"\x03",):
            return None
        if key in (b"\x7f", b"\x08"):
            if text:
                text.pop()
            continue
        if key.startswith(b"\x1b"):
            continue
        try:
            text.append(key.decode("utf-8"))
        except UnicodeDecodeError:
            pass


def setup(settings, providers, demo=False):
    refresher = Refresher(providers, settings)
    cursor, note = 0, ""
    with Screen() as screen:
        if not demo:
            refresher.kick(cache.PANEL_MAX_AGE)
        last = time.time()
        while True:
            now = time.time()
            if not demo and now - last >= cache.PANEL_MAX_AGE:
                refresher.kick(cache.PANEL_MAX_AGE)
                last = now
            rows = rows_of(providers, settings, cache.demo(providers) if demo else cache.load(), demo)
            cursor = max(0, min(cursor, len(rows) - 1))
            screen.draw(render.setup_lines(rows, cursor, note, size()))
            note = ""
            key = screen.key(0.5)
            if key in QUIT:
                break
            cursor, note = setup_key(key, rows, cursor, settings, refresher, screen)


# ── tab bar shim ─────────────────────────────────────────────────────

def shim_path():
    name = "tab-bar.cmd" if sys.platform == "win32" else "tab-bar.sh"
    return config.plugin_config_dir() / name


def write_shim():
    """config.toml points the tab bar at this file, and this file points at wherever
    the plugin is installed now. herdr's managed checkout path is internal and has
    changed before; the plugin config dir is the documented stable place."""
    path = shim_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    python = str(sys.executable)
    script = str(main_script())
    if sys.platform == "win32":
        body = ("@echo off\r\nrem Written by Plan Usage on setup and on every herdr start. Do not edit.\r\n"
                f'"{python}" "{script}" bar\r\n')
        tmp.write_bytes(body.encode("utf-8"))
    else:
        tmp.write_text("#!/bin/sh\n# Written by Plan Usage on setup and on every herdr start. Do not edit.\n"
                       f"exec {shlex.quote(python)} {shlex.quote(script)} bar\n", encoding="utf-8")
        tmp.chmod(0o755)
    os.replace(tmp, path)  # the tab bar may run it at any moment; never let it see half a file
    return path


def main_script():
    """The absolute path of plan_usage.py — argv[0] is relative when herdr runs it."""
    return Path(__file__).resolve().parent.parent / "plan_usage.py"
