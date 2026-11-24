# Plan Usage

Every agent's usage meters in the herdr tab bar, plus one popup with the detail.

The tab bar line is one segment per agent, showing only the numbers that matter:
timed quotas as percentages (`GPT 12/02` = 12% of the 5-hour window, 2% of the
weekly), money meters as amounts (`DS $6.43`), and the countdown to the next reset.

```
GPT 12/02 4h21m · OR $0.00/$1.00 6d15h · DS $6.43 · AGY 00% 6d23h
```

`prefix+u` opens the popup: every window of every agent, a bar per window, the
reset on your own clock, and a plain-language reason when something cannot be read.
`s` in the popup opens the settings screen; `s` again goes back.

```
  ⚠ 1 agent without a reading — see below

  Codex  Plus  you@example.com                                 4h21m   24%
    5h                      ━━━━━━━━━━━━━━━━━━━  24%  4h21m · Today 19:42
    Weekly                  ━━━━━━━━━━━━━━━━━━━   4%  6d18h · Tue 09:29

  OpenCode                                                               –
    ⚠ No active subscription — renew on opencode.ai

  OpenRouter                                                   6d15h    0%
    Credits                 ━━━━━━━━━━━━━━━━━━━   0%  6d15h · Tue 07:00

  DeepSeek                                                           $6.43
    Balance                 $6.43

  Antigravity  Free                                            6d23h    0%
    Weekly · Claude and GPT ━━━━━━━━━━━━━━━━━━━   0%  6d23h · Tue 15:20

  updated 20s ago · s setup · r refresh · q close
```

## Install

```bash
herdr plugin install hhhai0304/herdr-plan-usage
herdr plugin action invoke hhhai0304.plan-usage.setup   # prints the config snippet, `c` copies it
```

Paste the snippet into `~/.config/herdr/config.toml`. If that file already has a
`[ui]` table, move the keys into it — TOML does not allow a second `[ui]` — and if
`tab_bar_right` already lists something, add the entry to the array instead of
replacing it.

```toml
[ui]
tab_bar_right = [
  { type = "command", command = "$HOME/.config/herdr/plugins/config/hhhai0304.plan-usage/tab-bar.sh", interval_seconds = 15, timeout_seconds = 25 },
]

[[keys.command]]
key = "prefix+u"
type = "plugin_action"
command = "hhhai0304.plan-usage.open"
description = "plan usage"
```

Then `herdr server reload-config` (or restart herdr).

`tab-bar.sh` is a one-line shim the plugin rewrites on setup and on every herdr
start, so the config keeps working when herdr's internal install path changes.
On Windows the shim is `%APPDATA%\herdr\plugins\config\hhhai0304.plan-usage\tab-bar.cmd`.

## What it detects

No provider list to maintain: a provider appears when its login is on the machine,
and disappears when it is gone. A CLI installed without a login stays hidden
until you enable it in setup.

| Agent | Login read from | Usage read from |
|---|---|---|
| **Claude Code** | `~/.claude/.credentials.json` (or `$CLAUDE_CONFIG_DIR`), macOS Keychain | `api.anthropic.com/api/oauth/usage` |
| **Codex / ChatGPT** | `$CODEX_HOME/auth.json`, then pi's `openai-codex` login | `chatgpt.com/backend-api/wham/usage` |
| **OpenCode Go** | pi's `opencode-go` key | `opencode.ai/inference/go/v1/usage` |
| **OpenRouter** | pi's `openrouter` login, `$OPENROUTER_API_KEY` | `openrouter.ai/api/v1/key` |
| **DeepSeek** | pi's `deepseek` key, `$DEEPSEEK_API_KEY` | `api.deepseek.com/user/balance` |
| **Antigravity** | `agy`'s own token (`~/.gemini/antigravity-cli/`) | Cloud Code `retrieveUserQuotaSummary` — one row per quota group, named the way Google names it (`Gemini`, `Claude`, or the free tier's combined `Claude and GPT`) |
| **anything else** | a `[custom.<name>]` command you write | that command's JSON output |

pi is the important one here: it holds OAuth logins for Codex and OpenRouter and
API keys for OpenCode Go and DeepSeek, so a machine that only uses `pi` still gets
all four meters without installing a separate CLI for each vendor.

## Setup pane

`s` inside the popup, or `herdr plugin action invoke hhhai0304.plan-usage.setup`.

| Key | |
|---|---|
| `↑` `↓` | move |
| `space` | show / hide this agent |
| `b` | include / exclude it from the tab bar (the popup keeps it) |
| `l` | edit its tab bar label (Enter keeps, Esc cancels, empty resets) |
| `r` | refresh now |
| `c` | copy the config snippet |
| `s` | back to the usage screen (the standalone pane has no usage screen; `q` closes) |
| `q` | close |

Every change is written to `config.toml` in the plugin config directory
(`herdr plugin config-dir hhhai0304.plan-usage`) the moment you make it.

## Config

All keys, with defaults, live in [`config.example.toml`](config.example.toml). The
ones that change the tab bar line:

```toml
focus = "all"          # all · agent-first · agent-only (follow the focused pane's agent)
bar_errors = false     # true = an agent with no reading shows `⚠` instead of nothing
order = ["codex", "deepseek"]        # explicit order, the rest follow
separator = " · "

[codex]
label = "GPT"                        # the tab bar label
bar = true                           # false = popup only
enabled = true                       # false = hide everywhere (and stop fetching)
windows = ["session", "weekly"]      # which meters; absent = all
```

`windows` accepts window kinds (`session`, `daily`, `weekly`, `monthly`, `credits`,
`spend`, `balance`), a window's label, or a model name. An unknown token is
ignored rather than blanking the row.

### Anything not on the table

A command that prints one JSON object:

```toml
[custom.together]
name = "Together"
label = "TG"
command = "my-usage-script"
```

```json
{"plan": "Pay-as-you-go", "account": "me@example.com",
 "windows": [{"kind": "weekly", "used": 42, "resets_at": "2026-12-01T00:00:00Z"}]}
```

`kind` is one of `session`, `daily`, `weekly`, `monthly`, `credits`, `spend`,
`balance`. `amount` replaces `used` for a money meter.
(`{"kind": "balance", "amount": "$6.43"}`.)

## When it refreshes

| Who | When |
|---|---|
| tab bar | every 15 s, but it only reads the cache |
| cache | one fetch per agent per 5 minutes, in the background |
| popup | one fetch per agent per 2 minutes |
| agent turn ends | a fetch if 2 minutes have passed |
| `r` / the refresh action | one fetch per agent per 30 s |

A 429 or a network failure keeps the last good reading and records the reason;
the reason shows in the popup and `~` in the tab bar means the number is older
than `stale_after` (15 minutes by default). An agent whose subscription lapsed
keeps its explanation but leaves the tab bar alone.

## Security

Read-only, by design:

- Credentials are read per fetch and sent only to that vendor's own usage endpoint,
  over TLS, with redirects refused so a bearer token cannot be forwarded.
- Nothing is refreshed or written: refreshing is each CLI's job, and rotating a
  refresh token from outside can sign that CLI out. An expired token is reported,
  never renewed.
- The cache holds plan names, percentages and reset times — no tokens, no email
  addresses are stored beyond the account label a vendor returns.

## Development

```bash
python -m unittest discover -s tests -t . -v      # 52 tests, no network, no credentials
python plan_usage.py bar --demo                    # sample line
python plan_usage.py panel --demo                   # sample popup (q closes)
python plan_usage.py probe antigravity             # one provider's normalized answer, no cache write
herdr plugin link .                                 # develop against this checkout
```

Adding an agent: one `Provider(...)` entry in `planusage/providers.py` — its
credential source, a `fetch()` returning `{plan, account, windows}`, and a
`presence()` that answers "login / installed without login / absent". Everything
else (cache, bar, popup, setup, staleness) is generic.

## Credit

The popup layout, the tab bar shim and the event-driven refresh come from
[Plan Meter](https://github.com/JunSeo99/herdr-plan-meter) (MIT) — this is a
multi-provider rewrite of the same idea, with detection, per-agent labels and
selection. Licensed MIT, see [LICENSE](LICENSE).
