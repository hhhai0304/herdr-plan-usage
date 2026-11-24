"""Config file, defaults and the herdr paths this plugin lives in.

The file is a small, hand-editable TOML subset so a user can change everything
without a TUI. The setup pane writes the same file; unknown lines are preserved.

    lang = "en"                 en
    icons = "text"              text | nerd
    separator = " \u00b7 "
    stale_after = 900           seconds before a reading is marked stale
    focus = "all"               all | agent-first | agent-only
    bar_errors = false          show a provider with no reading as `\u26a0`
    order = ["codex", "claude"] optional explicit provider order

    [codex]
    label = "GPT"
    bar = true
    enabled = true
    windows = ["session", "weekly"]
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

PLUGIN_ID = "hhhai0304.plan-usage"

DEFAULTS = {
    "lang": "en",
    "icons": "text",
    "separator": " \u00b7 ",
    "stale_after": "900",
    "focus": "all",
    "bar_errors": "false",
    "order": "[]",
}

PROVIDER_KEYS = ("enabled", "bar", "label", "windows", "command", "name", "plan")


# ── paths ────────────────────────────────────────────────────────────

def herdr_dir():
    path = os.environ.get("HERDR_CONFIG_PATH")
    return Path(path).expanduser().parent if path else Path.home() / ".config" / "herdr"


def plugin_config_dir():
    """herdr passes this to plugin commands but not to tab bar commands, so the
    fallback is the documented per-platform location."""
    base = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if base:
        return Path(base)
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "herdr" / "plugins" / "config" / PLUGIN_ID
    return herdr_dir() / "plugins" / "config" / PLUGIN_ID


def cache_dir():
    override = os.environ.get("XDG_CACHE_HOME")
    if override:
        return Path(override) / "herdr-plan-usage"
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "herdr-plan-usage" / "cache"
    return Path.home() / ".cache" / "herdr-plan-usage"


def config_path():
    return plugin_config_dir() / "config.toml"


# ── parsing ──────────────────────────────────────────────────────────

def _scalar(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        items = []
        for part in inner.split(","):
            part = part.strip()
            if not part:
                continue
            items.append(part[1:-1] if len(part) >= 2 and part[0] == part[-1] and part[0] in "\"'" else part)
        return items
    return text


def _strip_comment(line):
    out, quote = [], None
    for ch in line:
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            break
        out.append(ch)
    return "".join(out).rstrip()


def parse(text):
    """(settings, warnings) from the config subset. Never raises."""
    data, warnings, extra, section = {}, [], [], None
    for raw in text.splitlines():
        line = _strip_comment(raw).strip()
        if not line:
            extra.append(raw.rstrip())
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().strip("\"'")
            data.setdefault(section, {})
            continue
        if "=" not in line:
            warnings.append(f"ignored: {raw.strip()}")
            extra.append(raw.rstrip())
            continue
        key, _, value = line.partition("=")
        key = key.strip().strip("\"'")
        target = data.setdefault(section, {}) if section else data
        target[key] = _scalar(value)
    return {"values": data, "warnings": warnings, "extra": extra}


def _toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(json.dumps(str(v)) for v in value) + "]"
    return json.dumps(str(value))


def dump(values):
    """Serialize the subset back: scalar keys first, then one section per provider."""
    head = [k for k in values if not k.startswith("_") and not isinstance(values[k], dict)]
    parts = []
    for key in head:
        parts.append(f"{key} = {_toml_value(values[key])}")
    if parts:
        parts.append("")
    for name, section in values.items():
        if name.startswith("_") or not isinstance(section, dict):
            continue
        if not section:
            continue
        parts.append(f"[{name}]")
        for key, value in section.items():
            parts.append(f"{key} = {_toml_value(value)}")
        parts.append("")
    for line in values.get("_extra", []):
        if line.strip():
            parts.append(line)
    return "\n".join(parts).rstrip() + "\n"


# ── settings ─────────────────────────────────────────────────────────

class Settings:
    """Effective settings: defaults, then the file, then what the setup pane changed."""

    def __init__(self, raw=None, warnings=None):
        self.values = dict(DEFAULTS)
        self.sections = {}
        self.warnings = list(warnings or [])
        self.extra = []
        if raw:
            self.values.update({k: v for k, v in raw.get("values", {}).items() if not isinstance(v, dict)})
            self.sections = {k: v for k, v in raw.get("values", {}).items() if isinstance(v, dict)}
            self.warnings += raw.get("warnings", [])
            self.extra = raw.get("extra", [])

    # scalars
    @property
    def lang(self):
        return self.values.get("lang") or "en"

    @property
    def icons(self):
        return "nerd" if self.values.get("icons") == "nerd" else "text"

    @property
    def separator(self):
        return self.values.get("separator") or DEFAULTS["separator"]

    @property
    def stale_after(self):
        try:
            return int(float(self.values.get("stale_after") or 900))
        except (TypeError, ValueError):
            return 900
    @property
    def focus(self):
        value = str(self.values.get("focus") or "all")
        return value if value in ("all", "agent-first", "agent-only") else "all"

    @property
    def bar_errors(self):
        return str(self.values.get("bar_errors")).lower() == "true"

    @property
    def order(self):
        raw = self.values.get("order")
        if isinstance(raw, list):
            return [str(x) for x in raw]
        try:
            order = json.loads(raw or "[]")
        except ValueError:
            return []
        return [str(x) for x in order] if isinstance(order, list) else []

    # one provider
    def section(self, pid):
        return dict(self.sections.get(pid) or {})

    def enabled_setting(self, pid):
        """True, False, or None for "auto" (show it when a login was detected)."""
        raw = self.section(pid).get("enabled")
        return None if raw is None else str(raw).lower() != "false"

    def in_bar(self, pid):
        return str(self.section(pid).get("bar", "true")).lower() != "false"

    def label(self, pid, fallback):
        return self.section(pid).get("label") or fallback

    def windows(self, pid):
        raw = self.section(pid).get("windows")
        if raw is None:
            return []
        if isinstance(raw, list):
            return [str(v) for v in raw]
        try:
            value = json.loads(raw) if isinstance(raw, str) and raw.startswith("[") else raw
        except ValueError:
            return []
        return [str(v) for v in value] if isinstance(value, list) else []

    def custom_providers(self):
        """[custom.<name>] sections: id, command and a display label."""
        out = []
        for name, section in self.sections.items():
            if name.startswith("custom.") and section.get("command"):
                out.append({"id": name.split(".", 1)[1], "command": section["command"],
                            "name": section.get("name") or name.split(".", 1)[1],
                            "label": section.get("label") or section.get("name") or name.split(".", 1)[1],
                            "windows": section.get("windows")})
        return out

    # persistence
    def set_provider(self, pid, **changes):
        section = self.sections.setdefault(pid, {})
        for key, value in changes.items():
            if value is None:
                section.pop(key, None)
            else:
                section[key] = value

    def save(self):
        # Only what differs from the defaults, so a future default change still reaches
        # a config nobody touched by hand.
        values = {k: v for k, v in self.values.items() if str(v) != DEFAULTS.get(k)}
        for name, section in self.sections.items():
            values[name] = section
        values["_extra"] = self.extra
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(dump(values), encoding="utf-8")
        os.replace(tmp, path)
        return path


def load():
    try:
        text = config_path().read_text(encoding="utf-8")
    except OSError:
        return Settings()
    return Settings(parse(text))
