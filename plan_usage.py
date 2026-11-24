#!/usr/bin/env python3
"""Plan Usage — every agent signed in on this machine, in the herdr tab bar.

  plan_usage.py bar        one line for herdr's ui.tab_bar_right (reads the cache)
  plan_usage.py panel      the popup: s settings · r refresh · q close
  plan_usage.py setup      toggle agents, edit labels, check what was detected
  plan_usage.py detect     what is on this machine right now (text or --json)
  plan_usage.py refresh    refresh now and print the bar line
  plan_usage.py snippet    the config.toml snippet for a fresh install
  plan_usage.py shim       (re)write the tab bar shim — runs on every herdr start
  plan_usage.py event      plugin event hook: refresh when an agent turn ends
  plan_usage.py open [panel|setup]   open a pane (what the manifest actions call)

Add `--demo` (or PLAN_USAGE_DEMO=1) to bar/panel/setup to render sample data
without reading a credential or calling a network endpoint.

Credentials are read from each agent's own login and sent only to that vendor's
usage endpoint. Nothing is refreshed or written here: the cache holds plan names,
percentages and reset times, never a token.
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# herdr renders UTF-8 (box drawing, `·`, `—`). Windows console text streams still
# default to the active code page, which turns every one of those into `?` or a
# replacement character, so pin the output encoding instead of inheriting it.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from planusage import cache, config, render, ui          # noqa: E402
from planusage.providers import registry                 # noqa: E402

COMMANDS = ("bar", "panel", "setup", "detect", "refresh", "snippet", "shim", "event", "open")


def detected(settings, providers, data, as_json):
    rows = ui.rows_of(providers, settings, data)
    if as_json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    if not rows:
        print("No provider configured.")
        return
    for row in rows:
        mark = "x" if row["enabled"] else " "
        state = row["state"]
        tail = (f'label "{row["label"]}" · bar {"on" if row["bar"] else "off"}'
                if state == "ready" else row["problem"] or render.STATE_TEXT.get(state, state))
        print(f'[{mark}] {row["name"]:<14} {row["plan"] or "–":<10} {tail}')
        print(f'      source: {row["source"] or row["id"]}')


def main():
    argv = sys.argv[1:]
    demo = "--demo" in argv or os.environ.get("PLAN_USAGE_DEMO") == "1"
    argv = [a for a in argv if a != "--demo"]
    command = argv[0] if argv else "bar"
    if command not in COMMANDS:
        sys.exit(__doc__)

    settings = config.load()
    providers = registry(settings)

    if command == "bar":
        data = cache.demo(providers) if demo else cache.refresh(providers, cache.BAR_MAX_AGE, settings)
        print(render.bar_text(providers, data, time.time(), settings, demo))
    elif command == "refresh":
        data = cache.demo(providers) if demo else cache.refresh(providers, cache.MANUAL_MIN_AGE, settings)
        print(render.bar_text(providers, data, time.time(), settings, demo))
    elif command == "panel":
        ui.panel(settings, providers, demo)
    elif command == "setup":
        ui.setup(settings, providers, demo)
    elif command == "detect":
        detected(settings, providers, cache.demo(providers) if demo else cache.load(), "--json" in argv)
    elif command == "snippet":
        print(ui.snippet())
    elif command == "shim":
        ui.write_shim()
    elif command == "event":
        # a status that leaves "working" means a turn finished (done, idle, blocked on a prompt)
        try:
            event = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON") or "{}")
        except ValueError:
            event = {}
        if event.get("agent_status") not in (None, "working"):
            cache.refresh(providers, cache.EVENT_MIN_AGE, settings)
    elif command == "open":
        entry = argv[1] if len(argv) > 1 else "panel"
        herdr = os.environ.get("HERDR_BIN_PATH") or "herdr"
        sys.exit(subprocess.run([herdr, "plugin", "pane", "open", "--plugin", config.PLUGIN_ID,
                                 "--entrypoint", entry]).returncode)


if __name__ == "__main__":
    main()
