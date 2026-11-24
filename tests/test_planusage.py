"""Unit tests: parsers, config, cache scheduling, rendering, detection.

Run from the repo root:

    python -m unittest discover -s tests -t . -v

No test touches a credential or a network endpoint: every provider answer is a
literal, and every path is redirected into a temporary directory.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from planusage import cache, config, credentials, render, ui  # noqa: E402
from planusage.model import FetchError, headline, money, select, to_epoch, window, windows_now  # noqa: E402
from planusage.providers import (Provider, fetch_custom, opencode_forbidden,  # noqa: E402
                                 parse_antigravity_quota, parse_claude, parse_codex,
                                 parse_deepseek, parse_opencode, parse_openrouter,
                                 registry, visible)


class TempEnv(unittest.TestCase):
    """Every path this plugin reads or writes points into a throwaway directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.env = {
            "HERDR_PLUGIN_CONFIG_DIR": str(base / "plugin-config"),
            "XDG_CACHE_HOME": str(base / "cache"),
            "PI_CODING_AGENT_DIR": str(base / "pi"),
            "HOME": str(base / "home"),
        }
        self.saved = {k: os.environ.get(k) for k in list(self.env) + [
            "CLAUDE_CONFIG_DIR", "CODEX_HOME", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY",
            "OPENCODE_API_KEY", "PLAN_USAGE_DEMO"]}
        os.environ.update(self.env)
        for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY",
                     "OPENCODE_API_KEY", "PLAN_USAGE_DEMO"):
            os.environ.pop(name, None)
        (base / "home").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def write_pi_auth(self, entries):
        path = Path(self.env["PI_CODING_AGENT_DIR"])
        path.mkdir(parents=True, exist_ok=True)
        (path / "auth.json").write_text(json.dumps(entries), encoding="utf-8")


# ── model ────────────────────────────────────────────────────────────

class ModelTest(unittest.TestCase):
    def test_to_epoch_accepts_seconds_millis_and_iso(self):
        self.assertEqual(to_epoch(1_700_000_000), 1_700_000_000)
        self.assertEqual(to_epoch(1_700_000_000_000), 1_700_000_000)
        self.assertEqual(to_epoch("2026-12-01T00:00:00Z"), 1796083200)
        self.assertIsNone(to_epoch("nope"))
        self.assertIsNone(to_epoch(0))
        self.assertIsNone(to_epoch(True))

    def test_to_epoch_survives_nanosecond_fractions(self):
        # agy writes 2026-11-24T16:13:35.753901215+07:00 — nine fractional digits
        self.assertIsNotNone(to_epoch("2026-11-24T16:13:35.753901215+07:00"))

    def test_money(self):
        self.assertEqual(money(6.43, "USD"), "$6.43")
        self.assertEqual(money("1", "CNY"), "\u00a51.00")
        self.assertEqual(money(3.5, "SEK"), "3.50 SEK")
        self.assertIsNone(money("n/a"))

    def test_window_reset_in_the_past_reads_zero(self):
        now = 1_000_000
        wins = windows_now({"windows": [
            window("session", used=80, resets_at=now - 10),
            window("weekly", used=40, resets_at=now + 10),
            window("balance", amount="$5.00"),
            {"kind": "bogus", "used": 1},
            {"kind": "weekly"},
        ]}, now)
        self.assertEqual(len(wins), 3)
        self.assertEqual(wins[0]["used"], 0)
        self.assertIsNone(wins[0]["resets_at"])
        self.assertEqual(wins[1]["used"], 40)
        self.assertEqual(wins[2]["amount"], "$5.00")

    def test_headline_picks_the_closest_limit(self):
        wins = [window("session", used=12), window("weekly", used=71)]
        self.assertEqual(headline(wins)["kind"], "weekly")
        self.assertEqual(headline([]), None)

    def test_select_filters_by_kind_label_and_model(self):
        wins = [window("session", used=1), window("weekly", used=2, model="Fable"),
                window("balance", amount="$1")]
        self.assertEqual(len(select(wins, [])), 3)
        self.assertEqual(select(wins, ["weekly"])[0]["model"], "Fable")
        self.assertEqual(select(wins, ["Balance"])[0]["kind"], "balance")
        # an unknown token keeps everything rather than blanking the row
        self.assertEqual(len(select(wins, ["nonsense"])), 3)


# ── parsers ──────────────────────────────────────────────────────────

class ParserTest(unittest.TestCase):
    def test_codex_windows_and_credits(self):
        wins = parse_codex({"rate_limit": {
            "primary_window": {"used_percent": 12, "limit_window_seconds": 18000, "reset_at": 1_700_000_000},
            "secondary_window": {"used_percent": 2, "limit_window_seconds": 604800,
                                 "reset_at": 1_700_500_000}},
            "credits": {"has_credits": False, "balance": "0"}})
        self.assertEqual([w["kind"] for w in wins], ["session", "weekly"])
        self.assertEqual(wins[0]["used"], 12)
        self.assertEqual(len(parse_codex({"rate_limit": {"primary_window": {"used_percent": 5},
                                                        "secondary_window": None}})), 1)

    def test_codex_balance_only_when_credits_exist(self):
        wins = parse_codex({"rate_limit": {}, "credits": {"has_credits": True, "balance": "3.5"}})
        self.assertEqual(wins[0]["kind"], "balance")
        self.assertEqual(wins[0]["amount"], "$3.50")

    def test_claude_five_hour_weekly_and_scoped_models(self):
        wins = parse_claude({
            "five_hour": {"utilization": 42, "resets_at": "2026-12-01T00:00:00Z"},
            "seven_day": {"utilization": 67, "resets_at": "2026-12-02T00:00:00Z"},
            "limits": [{"kind": "weekly_scoped", "percent": 83, "resets_at": None,
                        "scope": {"model": {"display_name": "Fable"}}}],
        })
        self.assertEqual([w["kind"] for w in wins], ["session", "weekly", "weekly"])
        self.assertEqual(wins[2]["model"], "Fable")
        self.assertEqual(wins[1]["used"], 67)

    def test_claude_credit_pool(self):
        wins = parse_claude({"spend": {"enabled": True, "percent": 42,
                                       "used": {"amount_minor": 4200, "currency": "USD", "exponent": 2},
                                       "limit": {"amount_minor": 10000, "currency": "USD", "exponent": 2}}})
        self.assertEqual(wins[0]["kind"], "credits")
        self.assertEqual(wins[0]["amount"], "$42.00 / $100.00")

    def test_claude_empty_document_is_a_format_error(self):
        self.assertEqual(parse_claude({}), [])

    def test_opencode_three_windows(self):
        wins = parse_opencode({"usage": {"rolling": {"percent": 0, "resetsAt": "2026-12-01T00:00:00Z"},
                                         "weekly": {"percent": 3}, "monthly": {"percent": 15}}})
        self.assertEqual([w["kind"] for w in wins], ["session", "weekly", "monthly"])
        self.assertEqual(wins[0]["resets_at"], 1796083200)

    def test_opencode_entitlement_body_becomes_a_readable_reason(self):
        body = json.dumps({"type": "error", "error": {"type": "EntitlementError",
                                                     "message": "OpenCode Go subscription required."}})
        self.assertIn("subscription", opencode_forbidden(body).lower())
        self.assertIsNone(opencode_forbidden('{"error":{"type":"authentication_error"}}'))
        self.assertIsNone(opencode_forbidden("not json"))

    def test_openrouter_budget(self):
        wins = parse_openrouter({"data": {
            "limit": 1, "limit_remaining": 0.75, "usage": 9.9,
            "effective_budget": {"spend_usd": 0.25, "resets_at": "2026-12-01T00:00:00Z"}}})
        self.assertEqual(wins[0]["kind"], "credits")
        self.assertEqual(wins[0]["used"], 25)
        self.assertEqual(wins[0]["amount"], "$0.25/$1.00")
        self.assertEqual(wins[0]["resets_at"], 1796083200)

    def test_openrouter_without_budget_reports_spend(self):
        wins = parse_openrouter({"data": {"limit": None, "usage_monthly": 12.5}})
        self.assertEqual(wins[0]["kind"], "spend")
        self.assertEqual(wins[0]["amount"], "$12.50")

    def test_deepseek_balance(self):
        wins = parse_deepseek({"balance_infos": [{"currency": "USD", "total_balance": "6.43"}]})
        self.assertEqual(wins[0]["amount"], "$6.43")
        self.assertEqual(parse_deepseek({"balance_infos": []}), [])

    def test_antigravity_groups_become_weekly_windows(self):
        doc = {"groups": [{"displayName": "Claude and GPT models", "buckets": [
            {"bucketId": "3p-weekly", "displayName": "Weekly Limit Remaining",
             "window": "weekly", "resetTime": "2026-12-01T08:15:47Z", "remainingFraction": 0.75}]}]}
        wins = parse_antigravity_quota(doc)
        self.assertEqual(wins[0]["kind"], "weekly")
        self.assertEqual(wins[0]["used"], 25.0)
        self.assertEqual(wins[0]["model"], "Claude and GPT")
        self.assertEqual(wins[0]["resets_at"], 1796112947)

    def test_antigravity_unknown_window_keeps_its_name(self):
        doc = {"groups": [{"displayName": "Gemini", "buckets": [
            {"displayName": "Daily credits", "window": "per-day", "remainingFraction": 0.5}]}]}
        wins = parse_antigravity_quota(doc)
        self.assertEqual(wins[0]["kind"], "custom")
        self.assertEqual(wins[0]["label"], "Daily credits")
        self.assertEqual(wins[0]["used"], 50.0)


# ── config ───────────────────────────────────────────────────────────

class ConfigTest(TempEnv):
    def test_roundtrip_keeps_unknown_lines(self):
        path = config.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# my settings\nlang = "en"\n\n[codex]\nlabel = "GPT"\nweird = inline\n',
                        encoding="utf-8")
        settings = config.load()
        self.assertEqual(settings.label("codex", "Codex"), "GPT")
        settings.set_provider("codex", label="CX")
        settings.save()
        text = path.read_text(encoding="utf-8")
        self.assertIn('label = "CX"', text)
        self.assertIn("# my settings", text)

    def test_accessors(self):
        settings = config.Settings({"values": {
            "stale_after": "60", "focus": "agent-only", "order": '["deepseek", "codex"]',
            "bar_errors": "true", "separator": " | ",
            "codex": {"enabled": "false", "bar": "false", "windows": '["session"]'}}})
        self.assertEqual(settings.stale_after, 60)
        self.assertEqual(settings.focus, "agent-only")
        self.assertEqual(settings.order, ["deepseek", "codex"])
        self.assertTrue(settings.bar_errors)
        self.assertEqual(settings.separator, " | ")
        self.assertIs(settings.enabled_setting("codex"), False)
        self.assertIsNone(settings.enabled_setting("deepseek"))  # auto
        self.assertFalse(settings.in_bar("codex"))
        self.assertEqual(settings.windows("codex"), ["session"])

    def test_bad_numbers_fall_back(self):
        settings = config.Settings({"values": {"stale_after": "soon", "focus": "nonsense",
                                                "order": "not json"}})
        self.assertEqual(settings.stale_after, 900)
        self.assertEqual(settings.focus, "all")
        self.assertEqual(settings.order, [])

    def test_custom_providers_are_sections(self):
        settings = config.Settings({"values": {"custom.mine": {
            "name": "Mine", "command": "echo {}", "label": "MN"}}})
        found = settings.custom_providers()
        self.assertEqual(found[0]["id"], "mine")
        self.assertEqual(found[0]["name"], "Mine")

    def test_scalars_keep_their_types(self):
        parsed = config.parse('n = 900\nflag = false\nlist = ["a", "b"]\nname = "x"\n')
        values = parsed["values"]
        self.assertEqual(values["n"], 900)
        self.assertIs(values["flag"], False)
        self.assertEqual(values["list"], ["a", "b"])
        self.assertEqual(values["name"], "x")

    def test_save_writes_only_what_changed(self):
        settings = config.load()
        settings.set_provider("codex", label="GPT", bar=False)
        settings.save()
        text = config.config_path().read_text(encoding="utf-8")
        self.assertIn('label = "GPT"', text)
        self.assertIn("bar = false", text)
        self.assertNotIn("lang", text)          # untouched defaults are not written
        self.assertNotIn("stale_after", text)
        self.assertNotIn("_extra", text)
        reloaded = config.load()
        self.assertEqual(reloaded.label("codex", "x"), "GPT")
        self.assertFalse(reloaded.in_bar("codex"))


# ── cache ────────────────────────────────────────────────────────────

def fake_provider(pid="fake", fetch=None, presence=("login", "test")):
    return Provider(pid, "Fake", "FK", "custom", "?", fetch or (lambda: {"plan": "X", "windows": []}),
                    lambda: presence, "test")


class CacheTest(TempEnv):
    def settings(self):
        return config.load()

    def test_refresh_writes_and_records(self):
        provider = fake_provider(fetch=lambda: {"plan": "Pro", "windows": [window("weekly", used=5)]})
        data = cache.refresh([provider], 0, self.settings())
        self.assertEqual(data["fake"]["plan"], "Pro")
        self.assertIsNone(data["fake"]["error"])
        self.assertGreater(data["fake"]["fetched_at"], 0)
        # a second call inside the window does not fetch again
        calls = []
        provider.fetch = lambda: calls.append(1) or {"plan": "Pro", "windows": []}
        cache.refresh([provider], 300, self.settings())
        self.assertEqual(calls, [])

    def test_failure_keeps_the_last_good_reading(self):
        provider = fake_provider(fetch=lambda: {"plan": "Pro", "windows": [window("weekly", used=5)]})
        cache.refresh([provider], 0, self.settings())

        def boom():
            raise FetchError("expired", cli="fake")
        provider.fetch = boom
        data = cache.refresh([provider], 0, self.settings())
        self.assertEqual(data["fake"]["plan"], "Pro")
        self.assertEqual(data["fake"]["error"]["code"], "expired")
        self.assertEqual(data["fake"]["error"]["cli"], "fake")

    def test_retry_after_defers_the_next_attempt(self):
        provider = fake_provider(fetch=lambda: (_ for _ in ()).throw(FetchError("limited", 300)))
        cache.refresh([provider], 0, self.settings())
        calls = []
        provider.fetch = lambda: calls.append(1) or {"plan": "Pro", "windows": []}
        cache.refresh([provider], 0, self.settings())
        self.assertEqual(calls, [])

    def test_disabled_provider_is_not_fetched(self):
        settings = config.load()
        settings.set_provider("fake", enabled="false")
        calls = []
        provider = fake_provider(fetch=lambda: calls.append(1) or {"plan": "X", "windows": []})
        cache.refresh([provider], 0, settings)
        self.assertEqual(calls, [])

    def test_corrupt_cache_is_ignored(self):
        path = cache.cache_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(cache.load(), {})

    def test_demo_covers_every_provider(self):
        data = cache.demo([fake_provider("codex"), fake_provider("custom.mine")])
        self.assertIn("codex", data)
        self.assertIn("custom.mine", data)
        self.assertTrue(data["codex"]["windows"])


# ── rendering ────────────────────────────────────────────────────────

class RenderTest(TempEnv):
    def providers(self):
        return [Provider("codex", "Codex", "GPT", "codex", "?", lambda: {}, lambda: ("login", "x"), ""),
                Provider("deepseek", "DeepSeek", "DS", "deepseek", "?", lambda: {}, lambda: ("login", "x"), ""),
                Provider("opencode", "OpenCode", "OC", "opencode", "?", lambda: {}, lambda: ("login", "x"), "")]

    def states(self, now):
        return {
            "codex": {"plan": "Plus", "fetched_at": now, "error": None, "windows": [
                window("session", used=12, resets_at=now + 4 * 3600),
                window("weekly", used=2, resets_at=now + 6 * 86400)]},
            "deepseek": {"plan": "", "fetched_at": now, "error": None,
                         "windows": [window("balance", amount="$6.43")]},
            "opencode": {"fetched_at": now, "error": {"code": "unentitled", "detail": "No active subscription"},
                         "windows": []},
        }

    def test_bar_shows_percent_for_quotas_and_money_for_balances(self):
        now = 1_000_000
        text = render.bar_text(self.providers(), self.states(now), now, config.load())
        self.assertIn("GPT 12/02", text)
        self.assertIn("DS $6.43", text)
        self.assertNotIn("OC", text)  # no reading, and bar_errors is off

    def test_bar_marks_a_stale_reading(self):
        now = 1_000_000
        states = self.states(now)
        states["codex"]["fetched_at"] = now - 1000
        settings = config.Settings({"values": {"stale_after": "900"}})
        self.assertIn("GPT ~12/02", render.bar_text(self.providers(), states, now, settings))

    def test_bar_respects_bar_false_and_error_flag(self):
        now = 1_000_000
        settings = config.Settings({"values": {"bar_errors": "true",
                                               "codex": {"bar": "false"}}})
        text = render.bar_text(self.providers(), self.states(now), now, settings)
        self.assertNotIn("GPT", text)
        self.assertIn("OC \u26a0", text)

    def test_bar_window_filter(self):
        now = 1_000_000
        settings = config.Settings({"values": {"codex": {"windows": '["weekly"]'}}})
        text = render.bar_text(self.providers(), self.states(now), now, settings)
        self.assertIn("GPT 02%", text)

    def test_panel_renders_every_width_without_error(self):
        now = 1_000_000
        for width in (60, 76, 120):
            lines = render.panel_lines(self.providers(), self.states(now), now, "detail", False,
                                       width, config.load())
            plain = [render.ANSI.sub("", line) for line in lines]
            self.assertTrue(any("Codex" in line for line in plain))
            self.assertTrue(any("No active subscription" in line for line in plain))
            self.assertLessEqual(max(len(line) for line in plain), width + 2)

    def test_panel_compact_hides_window_rows(self):
        now = 1_000_000
        detail = render.panel_lines(self.providers(), self.states(now), now, "detail", False, 76, config.load())
        compact = render.panel_lines(self.providers(), self.states(now), now, "compact", False, 76, config.load())
        self.assertGreater(len(detail), len(compact))

    def test_hidden_absent_provider_is_not_rendered(self):
        provider = Provider("claude", "Claude Code", "Claude", "claude", "?",
                            lambda: {}, lambda: ("absent", "not installed"), "")
        data = {"claude": {"error": {"code": "nologin"}}}
        lines = render.panel_lines([provider], data, time.time(), "detail", False, 76, config.load())
        self.assertFalse(any("Claude" in render.ANSI.sub("", line) for line in lines))

    def test_setup_rows_report_detection_and_state(self):
        now = time.time()
        settings = config.load()
        rows = ui.rows_of(self.providers(), settings, self.states(now))
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id["codex"]["state"], "ready")
        self.assertEqual(by_id["opencode"]["state"], "unentitled")
        self.assertIn("subscription", by_id["opencode"]["problem"])
        self.assertTrue(by_id["codex"]["enabled"])

    def test_setup_lines_fit_the_width(self):
        rows = ui.rows_of(self.providers(), config.load(), self.states(time.time()))
        for width in (60, 92):
            lines = render.setup_lines(rows, 0, "", width)
            plain = [render.ANSI.sub("", line) for line in lines]
            self.assertLessEqual(max(len(line) for line in plain), width + 1)


# ── registry + credentials ───────────────────────────────────────────

class RegistryTest(TempEnv):
    def test_all_agents_are_registered(self):
        ids = [p.id for p in registry(config.load())]
        for pid in ("claude", "codex", "opencode", "openrouter", "deepseek", "antigravity"):
            self.assertIn(pid, ids)

    def test_order_override(self):
        settings = config.Settings({"values": {"order": '["deepseek", "codex"]'}})
        ids = [p.id for p in registry(settings)]
        self.assertEqual(ids[:2], ["deepseek", "codex"])

    def test_custom_provider_from_config(self):
        settings = config.Settings({"values": {"custom.mine": {
            "name": "Mine", "command": "true", "label": "MN"}}})
        providers = registry(settings)
        self.assertEqual(providers[-1].id, "custom.mine")
        self.assertEqual(providers[-1].short, "MN")

    def test_claude_second_account_gets_its_own_row(self):
        settings = config.Settings({"values": {"claude": {
            "dirs": json.dumps(["~/.claude", "~/.claude-acc2"])}}})
        ids = [p.id for p in registry(settings) if p.id.startswith("claude")]
        self.assertEqual(ids, ["claude", "claude2"])

    def test_codex_reads_pi_login(self):
        self.write_pi_auth({"openai-codex": {"type": "oauth", "access": "tok",
                                             "accountId": "acc-1", "expires": 4_000_000_000_000}})
        cred = credentials.codex_cred()
        self.assertEqual(cred["source"], "pi:openai-codex")
        self.assertEqual(cred["account"], "acc-1")
        codex = next(p for p in registry(config.load()) if p.id == "codex")
        self.assertEqual(codex.detect()["state"], "ready")

    def test_pi_gateway_key_is_used_for_opencode(self):
        self.write_pi_auth({"opencode-go": {"type": "api_key", "key": "oc_sk_x"}})
        self.assertEqual(credentials.opencode_cred()["token"], "oc_sk_x")

    def test_expired_oauth_is_reported_not_refreshed(self):
        self.write_pi_auth({"openai-codex": {"type": "oauth", "access": "tok", "expires": 1_000}})
        cred = credentials.codex_cred()
        with self.assertRaises(FetchError) as caught:
            credentials.check_expiry(cred)
        self.assertEqual(caught.exception.code, "expired")

    def test_absent_agent_is_hidden_by_default_and_can_be_forced(self):
        provider = Provider("x", "X", "X", "custom", "?", lambda: {}, lambda: ("absent", "nope"), "")
        self.assertFalse(visible(provider, config.load()))
        forced = config.Settings({"values": {"x": {"enabled": "true"}}})
        self.assertTrue(visible(provider, forced))

    def test_custom_command_is_parsed(self):
        code = ("import json;print(json.dumps({'plan':'P','windows':"
                "[{'kind':'weekly','used':40,'resets_at':'2026-12-01T00:00:00Z'}]}))")
        result = fetch_custom(f'"{sys.executable}" -c "{code}"')
        self.assertEqual(result["plan"], "P")
        self.assertEqual(result["windows"][0]["used"], 40)
        with self.assertRaises(FetchError):
            fetch_custom(f'"{sys.executable}" -c "print(1)"')


# ── shim + CLI ───────────────────────────────────────────────────────

class ShimTest(TempEnv):
    def test_shim_points_at_this_install(self):
        path = ui.write_shim()
        self.assertTrue(path.exists())
        body = path.read_text(encoding="utf-8")
        self.assertIn(str(ROOT / "plan_usage.py"), body)
        self.assertIn(" bar", body)
        if sys.platform != "win32":
            self.assertTrue(os.access(path, os.X_OK))


class CliTest(TempEnv):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "plan_usage.py"), *args],
                              capture_output=True, text=True, timeout=60)

    def test_bar_demo_prints_one_line(self):
        result = self.run_cli("bar", "--demo")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.strip().splitlines()), 1)
        for label in ("GPT", "DS", "AGY"):
            self.assertIn(label, result.stdout)

    def test_detect_json_lists_every_provider(self):
        result = self.run_cli("detect", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)
        ids = {row["id"] for row in rows}
        self.assertIn("codex", ids)
        self.assertIn("antigravity", ids)

    def test_unknown_command_exits_with_usage(self):
        result = self.run_cli("nonsense")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("plan_usage.py bar", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
