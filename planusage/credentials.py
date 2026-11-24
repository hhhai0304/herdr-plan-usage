"""Where each agent keeps its login, and how to read it without touching it.

Every reader is read-only: tokens are read per fetch, never refreshed and never
written anywhere. Refreshing is the agent CLI's own job — rotating a refresh token
from outside can sign that CLI out.

Sources, in the order they are tried per provider:

  Codex / ChatGPT   $CODEX_HOME/auth.json, then pi's auth.json (openai-codex)
  OpenCode Go       pi's auth.json (opencode-go)
  OpenRouter        pi's auth.json (openrouter), then $OPENROUTER_API_KEY
  DeepSeek          pi's auth.json (deepseek), then $DEEPSEEK_API_KEY
  Claude Code       $CLAUDE_CONFIG_DIR/.credentials.json (or ~/.claude/…), then the
                    macOS Keychain item Claude Code writes on login
  Antigravity       ~/.gemini/antigravity-cli/antigravity-oauth-token (written by agy)
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .model import FetchError, to_epoch


def home():
    return Path.home()


def which(*names):
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def exists(path):
    return Path(path).expanduser().exists()


def pi_dir():
    return Path(os.environ.get("PI_CODING_AGENT_DIR") or home() / ".pi" / "agent")


def pi_auth():
    """pi's credential store. One JSON object keyed by provider id."""
    try:
        data = json.loads((pi_dir() / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def pi_entry(provider):
    entry = pi_auth().get(provider)
    return entry if isinstance(entry, dict) else {}


def oauth_cred(token, source, account=None, expires=None, cli=None):
    return {"token": token, "source": source, "account": account,
            "expires": to_epoch(expires), "cli": cli}


def api_key_cred(key, source):
    return {"token": key, "source": source, "account": None, "expires": None, "cli": None}


def check_expiry(cred):
    """An OAuth token past its expiry: the CLI renews it, this plugin cannot."""
    if cred.get("expires") and cred["expires"] < time.time():
        raise FetchError("expired", cli=cred.get("cli") or "the CLI")


# ── pi-backed providers ──────────────────────────────────────────────

def from_pi(provider, oauth_cli="pi"):
    entry = pi_entry(provider)
    if not entry:
        return None
    if entry.get("key"):
        return api_key_cred(entry["key"], f"pi:{provider}")
    if entry.get("access"):
        return oauth_cred(entry["access"], f"pi:{provider}",
                          account=entry.get("accountId"), expires=entry.get("expires"), cli=oauth_cli)
    return None


def from_env(*names):
    for name in names:
        value = os.environ.get(name)
        if value:
            return api_key_cred(value, f"env:{name}")
    return None


# ── Codex ────────────────────────────────────────────────────────────

def codex_cred():
    home_dir = Path(os.environ.get("CODEX_HOME") or home() / ".codex")
    try:
        tokens = json.loads((home_dir / "auth.json").read_text(encoding="utf-8")).get("tokens") or {}
    except (OSError, ValueError, AttributeError):
        tokens = {}
    if isinstance(tokens, dict) and tokens.get("access_token"):
        cred = oauth_cred(tokens["access_token"], "codex:~/.codex/auth.json",
                          account=tokens.get("account_id"), cli="codex")
        cred["id"] = tokens.get("account_id")
        return cred
    cred = from_pi("openai-codex")
    if cred:
        cred["id"] = cred.get("account")
        return cred
    return None


def opencode_cred():
    return from_pi("opencode-go") or from_env("OPENCODE_API_KEY")


def openrouter_cred():
    return from_pi("openrouter") or from_env("OPENROUTER_API_KEY")


def deepseek_cred():
    return from_pi("deepseek") or from_env("DEEPSEEK_API_KEY")


# ── Claude Code ──────────────────────────────────────────────────────

def claude_dirs(configured=None):
    if configured:
        return [Path(os.path.expanduser(str(d))) for d in configured]
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return [Path(os.path.expanduser(env)) if env else home() / ".claude"]


def _keychain_blobs(cfg):
    """Claude Code names its Keychain item after the config dir: the default
    ~/.claude keeps the plain name, anything else appends the first 8 hex chars
    of sha256(dir). Only the default dir may fall back to the plain name —
    a second account falling back would silently show the first account's numbers."""
    if sys.platform != "darwin":
        return
    scoped = "Claude Code-credentials-" + hashlib.sha256(str(cfg).encode()).hexdigest()[:8]
    services = [scoped, "Claude Code-credentials"] if cfg == home() / ".claude" else [scoped]
    for service in services:
        try:
            proc = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
                                  capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            yield proc.stdout


def claude_cred(cfg):
    cfg = Path(cfg)
    blobs = list(_keychain_blobs(cfg))
    try:
        blobs.append((cfg / ".credentials.json").read_text(encoding="utf-8"))
    except OSError:
        pass
    for blob in blobs:
        try:
            oauth = json.loads(blob).get("claudeAiOauth") or {}
        except (ValueError, AttributeError):
            continue
        if isinstance(oauth, dict) and oauth.get("accessToken"):
            cred = oauth_cred(oauth["accessToken"], str(cfg), expires=oauth.get("expiresAt"),
                              cli="claude")
            cred["oauth"] = oauth
            return cred
    return None


# ── Antigravity (agy) ────────────────────────────────────────────────

def agy_token_path():
    return home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"


def agy_cred():
    try:
        data = json.loads(agy_token_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    token = data.get("token") if isinstance(data, dict) else None
    if not isinstance(token, dict) or not token.get("access_token"):
        return None
    cred = oauth_cred(token["access_token"], "agy:~/.gemini/antigravity-cli", cli="agy")
    cred["expiry_text"] = token.get("expiry")
    if token.get("expiry"):
        stamp = to_epoch(token["expiry"])
        cred["expires"] = stamp
    return cred
