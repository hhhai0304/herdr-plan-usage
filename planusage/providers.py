"""The provider registry: what can be read on this machine, and how.

Each provider is a small object with

  presence()  cheap, offline: is the agent (or its login) on this machine at all?
  creds()     read-only credentials, or None
  fetch()     the vendor's own usage endpoint -> {plan, account, windows}

Everything else — caching, rendering, choosing which meters to show — is generic.
Adding an agent means adding one Provider here; nothing else changes.
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request

from . import credentials as creds
from .model import FetchError, money, to_epoch, window

VERSION = "1.0.0"
UA = f"herdr-plan-usage/{VERSION}"
_AGY_UA = None


# ── http ─────────────────────────────────────────────────────────────

class HttpError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # never carry a bearer token wherever a 3xx points


OPENER = urllib.request.build_opener(NoRedirect)


def http_json(url, headers, body=None, ua=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers={
        **{"User-Agent": ua or UA, "Accept": "application/json"},
        **({"Content-Type": "application/json"} if data else {}),
        **headers,
    })
    try:
        with OPENER.open(request, timeout=10) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        try:
            text = e.read().decode("utf-8", "replace")
        except OSError:
            text = ""
        raise HttpError(e.code, text)
    except ValueError:
        raise FetchError("format")
    except OSError:
        raise FetchError("network")


def as_fetch_error(e, cli, forbidden=None):
    """Turn an HTTP failure into the word the popup explains."""
    if e.status == 429:
        return FetchError("limited", 300)
    if e.status in (401, 403):
        detail = forbidden(e.body) if forbidden else None
        return FetchError("unentitled", detail=detail) if detail else FetchError("expired", cli=cli)
    return FetchError("http", status=e.status)


def window_kind(seconds, fallback):
    hours = (seconds or 0) / 3600
    if not hours:
        return fallback
    if round(hours) <= 6:
        return "session"
    if 6 <= hours / 24 <= 8:
        return "weekly"
    if 24 <= hours / 24 <= 32:
        return "monthly"
    return "custom"


# ── parsers ──────────────────────────────────────────────────────────

def parse_claude(doc):
    wins = []
    for key, kind in (("five_hour", "session"), ("seven_day", "weekly")):
        w = doc.get(key)
        if isinstance(w, dict) and isinstance(w.get("utilization"), (int, float)):
            wins.append(window(kind, used=w["utilization"], resets_at=to_epoch(w.get("resets_at"))))
    for limit in doc.get("limits") or []:  # per-model weekly caps
        model = ((limit.get("scope") or {}).get("model") or {}).get("display_name")
        if limit.get("kind") == "weekly_scoped" and isinstance(limit.get("percent"), (int, float)):
            wins.append(window("weekly", used=limit["percent"], model=model,
                               resets_at=to_epoch(limit.get("resets_at"))))
    spend = doc.get("spend")
    if isinstance(spend, dict) and spend.get("enabled") and isinstance(spend.get("percent"), (int, float)):
        used = _amount(spend.get("used"))
        cap = _amount(spend.get("limit"))
        wins.append(window("credits", used=spend["percent"], resets_at=None,
                           amount=f"{used} / {cap}" if used and cap else used))
    return wins


def _amount(blob):
    """An {amount_minor, currency, exponent} amount from the Claude endpoint."""
    if not isinstance(blob, dict) or not isinstance(blob.get("amount_minor"), (int, float)):
        return None
    exponent = blob.get("exponent") if isinstance(blob.get("exponent"), int) else 2
    scale = 10 ** max(0, min(6, exponent))
    return money(blob["amount_minor"] / scale, blob.get("currency"))


def parse_codex(doc):
    rl = doc.get("rate_limit") or {}
    wins = []
    for key, fallback in (("primary_window", "session"), ("secondary_window", "weekly")):
        w = rl.get(key)
        if isinstance(w, dict) and isinstance(w.get("used_percent"), (int, float)):
            kind = window_kind(w.get("limit_window_seconds"), fallback)
            wins.append(window(kind, used=w["used_percent"], resets_at=to_epoch(w.get("reset_at"))))
    credits = doc.get("credits") or {}
    if credits.get("has_credits") and credits.get("balance") not in (None, ""):
        wins.append(window("balance", amount=money(credits["balance"], "USD")))
    return wins


def parse_opencode(doc):
    usage = doc.get("usage") if isinstance(doc.get("usage"), dict) else {}
    wins = []
    for key, kind in (("rolling", "session"), ("weekly", "weekly"), ("monthly", "monthly")):
        w = usage.get(key)
        if isinstance(w, dict) and isinstance(w.get("percent"), (int, float)):
            wins.append(window(kind, used=w["percent"], resets_at=to_epoch(w.get("resetsAt"))))
    return wins


def parse_openrouter(doc):
    data = doc.get("data") if isinstance(doc.get("data"), dict) else {}
    budget = data.get("effective_budget") if isinstance(data.get("effective_budget"), dict) else {}
    resets_at = to_epoch(budget.get("resets_at")) or to_epoch(data.get("resets_at"))
    limit = data.get("limit")
    remaining = data.get("limit_remaining")
    spent = _num(budget.get("spend_usd"))
    wins = []
    if isinstance(limit, (int, float)) and limit > 0:
        if spent is None:
            spent = limit - remaining if isinstance(remaining, (int, float)) else 0.0
        wins.append(window("credits", used=round(spent / limit * 100, 2), resets_at=resets_at,
                           amount=f"{money(spent, 'USD')}/{money(limit, 'USD')}"))
    elif isinstance(remaining, (int, float)):
        wins.append(window("balance", amount=money(remaining, "USD")))
    elif _num(data.get("usage_monthly")) is not None:
        wins.append(window("spend", amount=money(data["usage_monthly"], "USD")))
    return wins


def parse_deepseek(doc):
    infos = doc.get("balance_infos") if isinstance(doc.get("balance_infos"), list) else []
    for info in infos:
        amount = money(info.get("total_balance"), info.get("currency"))
        if amount:
            return [window("balance", amount=amount)]
    return []


def family_of(group, bucket):
    """The name of one quota group.

    Google answers `groups[]` and every group carries its own limit; a work account
    typically has one group per model family (Gemini, Claude) while the free tier has
    a single combined "Claude and GPT models" group. The human `displayName` wins;
    `modelFamily` and the bucket id are fallbacks for a response that omits it, so a
    group never ends up unnamed.
    """
    name = re.sub(r"\s+models?$", "", str(group.get("displayName") or "").strip(), flags=re.I)
    if name:
        return name
    family = re.sub(r"^model[ _]family[ _]?", "", str(group.get("modelFamily") or "").strip(), flags=re.I)
    if family and family.lower() not in ("unspecified", "unknown"):
        return family.replace("_", " ").strip().title()
    bucket_id = re.sub(r"-(weekly|daily|monthly|session|hourly|5h)$", "",
                       str(bucket.get("bucketId") or "").strip(), flags=re.I)
    short = {"3p": "Third-party"}
    return short.get(bucket_id.lower(), bucket_id.replace("-", " ").title()) or None


def parse_antigravity_quota(doc):
    wins = []
    for group in doc.get("groups") or []:
        for bucket in group.get("buckets") or []:
            model = family_of(group, bucket)
            resets_at = to_epoch(bucket.get("resetTime"))
            fraction = bucket.get("remainingFraction")
            mapped = {"weekly": "weekly", "daily": "daily", "session": "session",
                      "5h": "session", "monthly": "monthly"}.get(str(bucket.get("window") or "").lower())
            if isinstance(fraction, (int, float)):
                if mapped:
                    wins.append(window(mapped, used=round((1 - fraction) * 100, 1), model=model,
                                       resets_at=resets_at))
                else:
                    wins.append(window("custom", used=round((1 - fraction) * 100, 1), model=model,
                                       label=bucket.get("displayName") or "Quota",
                                       resets_at=resets_at))
                continue
            # Some seats report what is left instead of a fraction; without a cap there
            # is no percentage to draw, so the amount is the whole reading.
            amount = bucket.get("remainingAmount")
            if isinstance(amount, (dict, list)) or amount in (None, ""):
                continue
            wins.append(window("custom", model=model,
                               label=bucket.get("displayName") or "Left",
                               amount=str(amount), resets_at=resets_at))
    return wins


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── fetchers ─────────────────────────────────────────────────────────

def fetch_claude(cfg):
    cred = creds.claude_cred(cfg)
    if not cred:
        raise FetchError("nologin")
    creds.check_expiry(cred)
    try:
        doc = http_json("https://api.anthropic.com/api/oauth/usage",
                        {"Authorization": f"Bearer {cred['token']}",
                         "anthropic-beta": "oauth-2025-04-20"})
    except HttpError as e:
        raise as_fetch_error(e, "claude")
    oauth = cred.get("oauth") or {}
    wins = parse_claude(doc)
    if not wins:
        raise FetchError("format")
    return {"plan": claude_plan(oauth), "windows": wins, "source": cred["source"]}


def claude_plan(oauth):
    tier = oauth.get("rateLimitTier") or ""
    sub = (oauth.get("subscriptionType") or "").lower()
    multiplier = None
    for part in tier.split("_"):
        if part.endswith("x") and part[:-1].isdigit():
            multiplier = part
    table = {("max", "5x"): "Max", ("max", "20x"): "Max 20x", ("team", "5x"): "Team Premium"}
    if (sub, multiplier) in table:
        return table[(sub, multiplier)]
    name = {"pro": "Pro", "max": "Max", "team": "Team", "free": "Free",
            "enterprise": "Enterprise"}.get(sub, sub.title())
    return f"{name} \u00b7 {multiplier}" if multiplier and sub not in ("max",) else name


def fetch_codex():
    cred = creds.codex_cred()
    if not cred:
        raise FetchError("nologin")
    creds.check_expiry(cred)
    headers = {"Authorization": f"Bearer {cred['token']}"}
    if cred.get("account"):
        headers["ChatGPT-Account-Id"] = cred["account"]
    try:
        doc = http_json("https://chatgpt.com/backend-api/wham/usage", headers)
    except HttpError as e:
        raise as_fetch_error(e, "codex")
    wins = parse_codex(doc)
    if not wins:
        raise FetchError("format")
    return {"plan": (doc.get("plan_type") or "").title(), "windows": wins,
            "account": doc.get("email"), "source": cred["source"]}


def fetch_opencode():
    cred = creds.opencode_cred()
    if not cred:
        raise FetchError("nologin")
    try:
        doc = http_json("https://opencode.ai/inference/go/v1/usage",
                        {"Authorization": f"Bearer {cred['token']}"})
    except HttpError as e:
        raise as_fetch_error(e, "opencode", forbidden=opencode_forbidden)
    wins = parse_opencode(doc)
    if not wins:
        raise FetchError("format")
    return {"plan": "Go", "windows": wins, "source": cred["source"]}


def opencode_forbidden(body):
    """A 403 with EntitlementError means the plan is gone, not that the key is bad."""
    try:
        error = json.loads(body).get("error") or {}
    except (ValueError, AttributeError):
        return None
    if str(error.get("type", "")).lower().startswith("entitlement"):
        return "No active subscription \u2014 renew on opencode.ai"
    return None


def fetch_openrouter():
    cred = creds.openrouter_cred()
    if not cred:
        raise FetchError("nologin")
    creds.check_expiry(cred)
    try:
        doc = http_json("https://openrouter.ai/api/v1/key",
                        {"Authorization": f"Bearer {cred['token']}"})
    except HttpError as e:
        raise as_fetch_error(e, "openrouter")
    wins = parse_openrouter(doc)
    if not wins:
        raise FetchError("format")
    return {"plan": "", "windows": wins, "source": cred["source"]}


def fetch_deepseek():
    cred = creds.deepseek_cred()
    if not cred:
        raise FetchError("nologin")
    try:
        doc = http_json("https://api.deepseek.com/user/balance",
                        {"Authorization": f"Bearer {cred['token']}"})
    except HttpError as e:
        raise as_fetch_error(e, "deepseek")
    wins = parse_deepseek(doc)
    if not wins:
        raise FetchError("format")
    return {"plan": "", "windows": wins, "source": cred["source"]}


def antigravity_ua():
    """Google routes the Cloud Code API by User-Agent: anything that does not look
    like the Antigravity client gets an empty tier list instead of the account."""
    global _AGY_UA
    if _AGY_UA is None:
        version = ""
        binary = creds.which("agy")
        if binary:
            try:
                out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=5).stdout
                found = re.search(r"\d+\.\d+\.\d+", out)
                version = found.group(0) if found else ""
            except (OSError, subprocess.SubprocessError):
                pass
        _AGY_UA = f"antigravity/{version}" if version else "antigravity"
    return _AGY_UA


def fetch_antigravity():
    cred = creds.agy_cred()
    if not cred:
        raise FetchError("nologin")
    creds.check_expiry(cred)
    base = "https://daily-cloudcode-pa.googleapis.com/v1internal:"
    headers = {"Authorization": f"Bearer {cred['token']}"}
    ua = antigravity_ua()
    try:
        tier = http_json(base + "loadCodeAssist", headers, {}, ua=ua)
        project = tier.get("cloudaicompanionProject")
        if isinstance(project, dict):
            project = project.get("id") or project.get("name")
        if not project:
            raise FetchError("unsupported", detail="No Antigravity project on this account")
        quota = http_json(base + "retrieveUserQuotaSummary", headers, {"project": project}, ua=ua)
    except HttpError as e:
        raise as_fetch_error(e, "agy", forbidden=antigravity_forbidden)
    wins = parse_antigravity_quota(quota)
    if not wins:
        raise FetchError("unsupported", detail="This Antigravity account reports no quota")
    current = tier.get("currentTier") or {}
    return {"plan": antigravity_plan(current, tier.get("paidTier")), "windows": wins,
            "source": cred["source"]}


def antigravity_plan(current, paid):
    tier = (paid or current or {}).get("id") or ""
    labels = {"free-tier": "Free", "standard-tier": "Pro", "enterprise-tier": "Enterprise"}
    return labels.get(tier, tier.replace("-tier", "").title() or "Antigravity")


def antigravity_forbidden(body):
    try:
        message = ((json.loads(body).get("error") or {}).get("message") or "").strip()
    except (ValueError, AttributeError):
        return None
    if "license" in message.lower():
        return "No quota on this account (no Antigravity license)"
    return None


def fetch_custom(command):
    """A user-defined agent: any command that prints the same JSON shape.

    {"plan": "…", "account": "…",
     "windows": [{"kind": "weekly", "used": 42, "resets_at": "2026-12-01T00:00:00Z"}]}

    `amount` replaces `used` for a money meter (kind `balance` / `spend`).
    """
    try:
        proc = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=8,
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        raise FetchError("network")
    if proc.returncode != 0:
        raise FetchError("unexpected", name=f"exit {proc.returncode}")
    try:
        doc = json.loads(proc.stdout)
    except ValueError:
        raise FetchError("format")
    if not isinstance(doc, dict):
        raise FetchError("format")
    wins = []
    for raw in doc.get("windows") or []:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("kind") if raw.get("kind") in ("session", "daily", "weekly", "monthly",
                                                      "credits", "spend", "balance") else "custom"
        used = raw.get("used") if isinstance(raw.get("used"), (int, float)) else None
        wins.append(window(kind, used=used, resets_at=to_epoch(raw.get("resets_at")),
                           label=raw.get("label"), model=raw.get("model"),
                           amount=raw.get("amount")))
    if not wins:
        raise FetchError("format")
    return {"plan": doc.get("plan") or "", "windows": wins,
            "account": doc.get("account"), "source": "custom"}


# ── registry ─────────────────────────────────────────────────────────

class Provider:
    def __init__(self, pid, name, short, color, icon, fetch, presence, source_hint):
        self.id = pid
        self.name = name
        self.short = short          # default tab bar label
        self.color = color
        self.icon = icon
        self.fetch = fetch
        self.presence = presence    # ("login"|"agent"|"absent", detail)
        self.source_hint = source_hint

    def detect(self):
        """Offline answer: is there a login to read, an agent without one, or nothing?"""
        state, detail = self.presence()
        mapped = {"login": "ready", "agent": "nologin", "absent": "absent"}
        return {"state": mapped.get(state, "absent"), "detail": detail}


def _claude_providers(settings):
    configured = _list(settings.section("claude").get("dirs"))
    out = []
    binary = creds.which("claude")
    for index, path in enumerate(creds.claude_dirs(configured or None)):
        pid = "claude" if index == 0 else f"claude{index + 1}"
        short = settings.label(pid, "Claude" if index == 0 else f"Claude {index + 1}")

        def presence(path=path, binary=binary):
            if creds.claude_cred(path):
                return ("login", str(path))
            if path.exists() or binary:
                return ("agent", f"no login in {path}")
            return ("absent", "not installed")

        out.append(Provider(pid, "Claude Code", short, "claude", "\uec82",
                            lambda path=path: fetch_claude(str(path)), presence, str(path)))
    return out


def _list(raw):
    """`dirs = ["~/.claude", "~/.claude-acc2"]` from the config subset."""
    if isinstance(raw, list):
        return raw
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def registry(settings):
    """Every provider this machine could show, in display order."""
    out = []

    for provider in _claude_providers(settings):
        out.append(provider)

    codex_binary = creds.which("codex")
    out.append(Provider(
        "codex", "Codex", settings.label("codex", "GPT"), "codex", "\uec81",
        fetch_codex,
        lambda: ("login", "pi:openai-codex") if creds.codex_cred()
        else ("agent", codex_binary) if codex_binary
        else ("absent", "no login on this machine"),
        "pi auth.json or ~/.codex/auth.json"))

    opencode_binary = creds.which("opencode")
    out.append(Provider(
        "opencode", "OpenCode", settings.label("opencode", "OC"), "opencode", "\uf120",
        fetch_opencode,
        lambda: ("login", "pi:opencode-go") if creds.opencode_cred()
        else ("agent", opencode_binary) if opencode_binary
        else ("absent", "no login on this machine"),
        "pi auth.json"))

    out.append(Provider(
        "openrouter", "OpenRouter", settings.label("openrouter", "OR"), "openrouter", "\uf0c1",
        fetch_openrouter,
        lambda: ("login", "pi:openrouter") if creds.openrouter_cred()
        else ("absent", "no login on this machine"),
        "pi auth.json or OPENROUTER_API_KEY"))

    out.append(Provider(
        "deepseek", "DeepSeek", settings.label("deepseek", "DS"), "deepseek", "\uf1c0",
        fetch_deepseek,
        lambda: ("login", "pi:deepseek") if creds.deepseek_cred()
        else ("absent", "no login on this machine"),
        "pi auth.json or DEEPSEEK_API_KEY"))

    agy_binary = creds.which("agy")
    out.append(Provider(
        "antigravity", "Antigravity", settings.label("antigravity", "AGY"), "antigravity", "\uf135",
        fetch_antigravity,
        lambda: ("login", "agy login") if creds.agy_cred()
        else ("agent", agy_binary) if agy_binary
        else ("absent", "no login on this machine"),
        "agy login"))

    for custom in settings.custom_providers():
        pid = f"custom.{custom['id']}"
        command = custom["command"]
        out.append(Provider(
            pid, custom["name"], settings.label(pid, custom["label"]), "custom", "\uf013",
            lambda command=command: fetch_custom(command),
            lambda: ("login", "custom command"),
            "custom command"))

    order = settings.order
    if order:
        ranked = {pid: index for index, pid in enumerate(order)}
        out.sort(key=lambda p: ranked.get(p.id, len(ranked) + 1))
    return out


def visible(provider, settings):
    """Show a provider when the user asked for it, or by default once a login exists.

    `enabled = true/false` in the config always wins; without it the answer is
    auto-detection, so a machine that never signed into Claude does not carry an
    empty Claude row in the popup.
    """
    explicit = settings.enabled_setting(provider.id)
    if explicit is not None:
        return explicit
    return provider.detect()["state"] == "ready"
