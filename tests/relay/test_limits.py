"""Usage limits (relay.limits): noticed in what providers say, and what's left of them, asked from the
providers (Provider.limits, /api/usage/limits): those answers' shapes as the providers gave them (ids
left out), logins read but never renewed."""

import base64
import datetime as dt
import json
import sqlite3
import time

import pytest

from relay import limits
from relay.providers import KINDS
from relay.providers.base import Template

NOW = dt.datetime(2026, 9, 30, 12, 0, tzinfo=dt.timezone.utc).timestamp()


@pytest.mark.parametrize("message, expected", [
    ("Claude AI usage limit reached|1759186800", 1759186800),
    ("You've hit your usage limit. Try again in 2 hours 5 minutes.", NOW + 2 * 3600 + 300),
    ("Rate limit reached. Please try again in 1h2m3s.", NOW + 3723),
    ("429 Too Many Requests: retry after 30 seconds", NOW + 30),
    ("usage limit, resets at 2026-09-30T15:00:00Z", NOW + 3 * 3600),
])
def test_reset_times(message, expected):
    assert limits.is_limit(message)
    assert limits.parse_reset(message, NOW) == pytest.approx(expected)


def test_a_clock_time_is_the_next_one():
    found = limits.parse_reset("5-hour limit reached ∙ resets 3pm (UTC)", NOW)
    assert found == pytest.approx(NOW + 3 * 3600)
    found = limits.parse_reset("You've hit your limit · resets 11am (UTC)", NOW)
    assert found == pytest.approx(NOW + 23 * 3600)  # tomorrow


def test_not_a_limit():
    assert not limits.is_limit("SyntaxError: invalid syntax")
    assert not limits.is_limit("SyntaxError at line 429 of main.py")
    assert not limits.is_limit("disk quota is 80% full")
    assert limits.is_limit("HTTP 429: slow down") and limits.is_limit("You exceeded your current quota")
    assert limits.parse_reset("Something failed", NOW) is None
    assert limits.reached([{"type": "error", "message": "boom"}]) is None


def test_headers_and_events():
    assert limits.from_headers({"retry-after": "120"}, NOW) == NOW + 120
    assert limits.from_headers({"x-ratelimit-reset-requests": "6m0s"}, NOW) == pytest.approx(NOW + 360)
    events = [{"type": "limit", "status": "rejected", "resets_at": 1790000000, "window": "five_hour"},
              {"type": "error", "message": "limit"}]
    assert limits.reached(events)["resets_at"] == 1790000000
    assert limits.reached([{"type": "error", "message": "Usage limit reached|1759186800"}])["resets_at"] == 1759186800


def test_claude_codes_rate_limit_event():
    from opencode_manager.bridge.claude_backend import Translator
    out = Translator().translate({"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "resetsAt": 1790723400, "rateLimitType": "five_hour",
        "unifiedWindows": {"five_hour": {"utilization": 1.0, "resetsAt": 1790723400}}}})
    assert out == [{"type": "limit", "status": "rejected", "resets_at": 1790723400, "window": "five_hour",
                    "windows": {"five_hour": {"utilization": 1.0, "resets_at": 1790723400}}}]


def test_ratelimit_reset_in_seconds():
    assert limits.from_headers({"ratelimit-reset": "120"}, NOW) == NOW + 120
    assert limits.from_headers({"x-ratelimit-reset": "1790769600"}, NOW) == 1790769600  # a moment


# What's left of a plan's limits, asked from the providers

CLAUDE = {  # (Claude Code's /usage: api.anthropic.com/api/oauth/usage)
    "five_hour": {"utilization": 30.0, "resets_at": "2026-10-04T11:50:00.466992+00:00"},
    "seven_day": {"utilization": 49.0, "resets_at": "2026-10-09T17:00:00.467012+00:00"},
    "seven_day_opus": None,
    "limits": [
        {"kind": "session", "group": "session", "percent": 30, "severity": "normal",
         "resets_at": "2026-10-04T11:50:00.466992+00:00", "scope": None, "is_active": False},
        {"kind": "weekly_all", "group": "weekly", "percent": 49, "severity": "normal",
         "resets_at": "2026-10-09T17:00:00.467012+00:00", "scope": None, "is_active": True},
    ],
}
CHATGPT = {  # (Codex's /status: chatgpt.com/backend-api/wham/usage)
    "plan_type": "plus",
    "rate_limit": {"allowed": True, "limit_reached": False,
                   "primary_window": {"used_percent": 4, "limit_window_seconds": 18000,
                                      "reset_after_seconds": 14709, "reset_at": 1791114679},
                   "secondary_window": {"used_percent": 5, "limit_window_seconds": 604800,
                                        "reset_after_seconds": 520328, "reset_at": 1791620298}},
    "additional_rate_limits": None,
}
COPILOT = {  # (api.github.com/copilot_internal/user)
    "access_type_sku": "free_limited_copilot", "copilot_plan": "individual",
    "quota_snapshots": {
        "chat": {"percent_remaining": 93.5, "unlimited": False, "has_quota": True, "remaining": 187, "entitlement": 200},
        "completions": {"percent_remaining": 100.0, "unlimited": False, "has_quota": True, "remaining": 2000,
                        "entitlement": 2000},
        "premium_interactions": {"percent_remaining": 0.0, "unlimited": False, "has_quota": False, "remaining": 0,
                                 "entitlement": 0},
    },
    "quota_reset_date": "2026-11-01", "quota_reset_date_utc": "2026-11-01T00:00:00.000Z",
}


def test_claude_windows_from_its_limits_and_else_its_windows():
    stamp = lambda text: dt.datetime.fromisoformat(text).timestamp()  # noqa: E731
    assert limits.claude_windows(CLAUDE) == [
        {"label": "5-hour", "used": .3, "resets_at": stamp("2026-10-04T11:50:00.466992+00:00"), "left": None},
        {"label": "Weekly", "used": .49, "resets_at": stamp("2026-10-09T17:00:00.467012+00:00"), "left": None}]
    older = {k: v for k, v in CLAUDE.items() if k != "limits"}  # (no "limits": the windows themselves)
    assert [(w["label"], w["used"]) for w in limits.claude_windows(older)] == [("5-hour", .3), ("Weekly", .49)]
    assert limits.claude_windows({"limits": [{"kind": "weekly_opus", "percent": 80}]})[0]["label"] == "Weekly opus"
    assert limits.claude_windows(None) == limits.claude_windows({"limits": "?"}) == []


def test_chatgpt_windows():
    assert limits.chatgpt_windows(CHATGPT) == [
        {"label": "5-hour", "used": .04, "resets_at": 1791114679.0, "left": None},
        {"label": "Weekly", "used": .05, "resets_at": 1791620298.0, "left": None}]
    # Only a delay to the reset, and a window of another length
    odd = {"rate_limit": {"primary_window": {"used_percent": 50, "limit_window_seconds": 3 * 3600,
                                             "reset_after_seconds": 60}, "secondary_window": None}}
    assert limits.chatgpt_windows(odd, now=1000) == [{"label": "3-hour", "used": .5, "resets_at": 1060, "left": None}]
    assert limits.chatgpt_windows({"rate_limit": None}) == limits.chatgpt_windows([]) == []


def test_copilot_windows_only_the_quotas_that_count():
    assert limits.copilot_windows(COPILOT) == [
        {"label": "Chat messages", "used": pytest.approx(.065), "resets_at": 1793491200.0, "left": "187 of 200"}]
    pro = {"quota_snapshots": {"premium_interactions": {"entitlement": 300, "percent_remaining": 40.0,
                                                        "unlimited": False},
                               "chat": {"unlimited": True}}, "quota_reset_date": "2026-11-01"}
    assert limits.copilot_windows(pro) == [{"label": "Premium requests", "used": pytest.approx(.6),
                                            "resets_at": 1793491200.0, "left": "120 of 300"}]
    assert limits.plan_name("free_limited_copilot") == "Free" and limits.plan_name("plus") == "Plus"


def test_windows_a_run_told():
    usage = {"status": "allowed", "windows": {"five_hour": {"utilization": .49, "resets_at": 1791063000},
                                              "seven_day": {"utilization": .33, "resets_at": "x"}}}
    assert limits.run_windows(usage) == [{"label": "5-hour", "used": .49, "resets_at": 1791063000.0, "left": None},
                                         {"label": "Weekly", "used": .33, "resets_at": None, "left": None}]
    assert limits.run_windows(None) == limits.run_windows({"windows": None}) == []


def provider(kind, **options):
    return KINDS[kind](Template(id=kind, name=kind, kind=kind, options=options))


def asked(monkeypatch, answer):
    calls = []

    def fetch(url, headers, timeout=8):
        calls.append((url, headers))
        return (answer, None) if answer is not None else (None, "its login was refused")
    monkeypatch.setattr(limits, "fetch_json", fetch)
    return calls


def test_claude_asks_with_its_login_and_never_with_an_expired_one(tmp_path, monkeypatch):
    folder = tmp_path / "claude"
    folder.mkdir()
    login = {"accessToken": "sk-ant-oat-test", "expiresAt": (time.time() + 3600) * 1000, "subscriptionType": "max"}
    (folder / ".credentials.json").write_text(json.dumps({"claudeAiOauth": login}))
    calls = asked(monkeypatch, CLAUDE)
    found = provider("claude-code", config_dir=str(folder)).limits()
    assert (found["service"], found["plan"]) == ("Claude", "Max") and [w["label"] for w in found["windows"]] == ["5-hour", "Weekly"]
    assert calls == [(limits.CLAUDE_USAGE, {"Authorization": "Bearer sk-ant-oat-test", "anthropic-beta": "oauth-2025-04-20"})]
    login["expiresAt"] = (time.time() - 60) * 1000  # (Claude Code renews it; Relay doesn't, and doesn't ask)
    (folder / ".credentials.json").write_text(json.dumps({"claudeAiOauth": login}))
    assert "expired" in provider("claude-code", config_dir=str(folder)).limits()["error"] and len(calls) == 1
    (folder / ".credentials.json").unlink()
    assert provider("claude-code", config_dir=str(folder)).limits() is None  # (no plan login)


def chatgpt_login(folder, **claims):
    folder.mkdir(parents=True)
    payload = base64.urlsafe_b64encode(json.dumps({"https://api.openai.com/auth": claims}).encode()).decode().rstrip("=")
    db = sqlite3.connect(folder / "opencode.db")
    db.execute("CREATE TABLE credential (id TEXT, integration_id TEXT, label TEXT, value TEXT, connector_id TEXT, "
               "method_id TEXT, active INTEGER, time_created INTEGER, time_updated INTEGER)")
    value = {"type": "oauth", "access": f"head.{payload}.sig", "refresh": "r", "expires": (time.time() + 3600) * 1000,
             "metadata": {}}
    db.execute("INSERT INTO credential VALUES ('c', 'openai', 'OpenAI', ?, NULL, NULL, 1, 0, 0)", (json.dumps(value),))
    db.commit()
    db.close()


def test_opencode_asks_chatgpt_with_its_profiles_openai_login(tmp_path, monkeypatch):
    from relay.providers import opencode as opencode_provider

    class Profile:
        data_home = tmp_path / "profile" / "data"
    monkeypatch.setattr(opencode_provider.OpencodeProvider, "_manager", lambda self: (None, None, Profile()))
    assert provider("opencode").limits() is None  # (no OpenAI login in this profile)
    chatgpt_login(Profile.data_home / "opencode", chatgpt_account_id="acc-1", chatgpt_plan_type="plus")
    calls = asked(monkeypatch, CHATGPT)
    found = provider("opencode").limits()
    assert found["account"] == "chatgpt:acc-1" and (found["service"], found["plan"]) == ("ChatGPT", "Plus") and len(found["windows"]) == 2
    assert calls[0][0] == limits.CHATGPT_USAGE and calls[0][1]["ChatGPT-Account-Id"] == "acc-1"
    assert calls[0][1]["Authorization"].startswith("Bearer head.")


def test_copilot_reads_its_login_from_a_config_with_comments(tmp_path, monkeypatch):
    home = tmp_path / "copilot"
    home.mkdir()
    (home / "config.json").write_text(
        "// User settings belong in settings.json.\n// This file is managed automatically.\n" + json.dumps({
            "authTokens": {"https://github.com:old": {"token": "gho_old"}, "https://github.com:me": {"token": "gho_me"}},
            "lastLoggedInUser": {"host": "https://github.com", "login": "me"}}))
    calls = asked(monkeypatch, COPILOT)
    found = provider("copilot", env={"COPILOT_HOME": str(home)}).limits()
    assert (found["service"], found["plan"]) == ("Copilot", "Free") and found["windows"][0]["left"] == "187 of 200"
    assert calls == [(limits.COPILOT_USER, {"Authorization": "token gho_me"})]
    assert provider("copilot", env={"COPILOT_HOME": str(home), "COPILOT_PROVIDER_BASE_URL": "http://x"}).limits() is None
