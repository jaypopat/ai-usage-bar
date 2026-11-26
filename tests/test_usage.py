import http.client
import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from PyQt6.QtCore import QSize
from PyQt6.QtWidgets import QApplication

from ai_usage_bar import (
    ANTIGRAVITY_ENDPOINTS,
    PROVIDERS,
    FetchWorker,
    ProviderUsage,
    TrayApp,
    UsageClient,
    UsageLane,
    UsagePopup,
    UsageWindow,
    _SameOriginRedirectHandler,
    compact_tokens,
    lane_label,
    merge_antigravity_windows,
    parse_antigravity_models,
    parse_antigravity_plan,
    parse_antigravity_summary,
    parse_claude,
    parse_codex,
    provider_icon,
    remaining_text,
    updated_text,
)


class UsageParsingTests(unittest.TestCase):
    def test_claude_windows(self):
        result = parse_claude({
            "five_hour": {"utilization": 22, "resets_at": "2026-07-15T18:00:00Z"},
            "seven_day": {"utilization": 78.5, "resets_at": "2026-07-18T00:00:00+00:00"},
        })
        self.assertEqual([w.label for w in result.windows], ["5 hour", "Weekly"])
        self.assertEqual(result.windows[1].used, 78.5)

    def test_codex_uses_actual_duration(self):
        result = parse_codex({
            "plan_type": "plus",
            "rate_limit_reset_credits": {"available_count": 4},
            "rate_limit": {"primary_window": {
                "used_percent": 4, "reset_at": 1784200000, "limit_window_seconds": 604800
            }},
        })
        self.assertEqual(result.plan, "Plus")
        self.assertEqual(result.windows[0].label, "Weekly")
        self.assertEqual(result.reset_credits, 4)

    def test_codex_five_hour_daily_and_additional_limits(self):
        result = parse_codex({
            "rate_limit": {
                "primary_window": {"used_percent": 12, "reset_at": 1, "limit_window_seconds": 18000},
                "secondary_window": {"used_percent": 25, "reset_at": 2, "limit_window_seconds": 86400},
            },
            "additional_rate_limits": [{
                "limit_name": "Spark",
                "rate_limit": {"primary_window": {
                    "used_percent": 40, "reset_at": 3, "limit_window_seconds": 604800
                }},
            }],
        })
        self.assertEqual([window.label for window in result.windows], ["5 hour", "Daily", "Spark · Weekly"])

    def test_missing_lanes_are_not_invented(self):
        self.assertEqual(parse_codex({"rate_limit": {}}).windows, [])
        self.assertEqual(parse_claude({"five_hour": None}).windows, [])

    def test_remaining_text(self):
        now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        reset = datetime(2026, 7, 16, 14, 30, tzinfo=timezone.utc)
        self.assertEqual(remaining_text(reset, now), "resets in 1d 2h")

    def test_updated_text(self):
        now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(updated_text(now, now), "Updated just now")
        self.assertEqual(updated_text(datetime(2026, 7, 15, 11, 57, tzinfo=timezone.utc), now), "Updated 3 min ago")
        self.assertEqual(updated_text(datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc), now), "Updated 2h ago")
        self.assertEqual(updated_text(None), "Updated")

    def test_friendly_missing_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            client = UsageClient(Path(directory))
            self.assertIn("run claude", client.claude().error or "")
            self.assertIn("codex login", client.codex().error or "")

    def test_claude_plan_name_comes_from_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".claude").mkdir()
            (home / ".claude/.credentials.json").write_text(json.dumps({"claudeAiOauth": {
                "accessToken": "test", "subscriptionType": "max", "rateLimitTier": "default_claude_max_5x"
            }}))
            client = UsageClient(home)
            with patch.object(client, "_json_request", return_value={}):
                self.assertEqual(client.claude().plan, "Max 5×")

    def test_codexbar_cost_output_is_mapped_to_models(self):
        today = datetime.now().astimezone().date().isoformat()
        payload = json.dumps([{
            "provider": "codex", "historyDays": 30,
            "totals": {"totalCost": 3.5, "totalTokens": 12000},
            "daily": [{"date": today, "totalCost": 1.25, "totalTokens": 4000, "modelBreakdowns": [
                {"modelName": "gpt-test", "totalTokens": 12000, "cost": 3.5}
            ]}],
        }])
        with patch("ai_usage_bar.shutil.which", return_value="/usr/bin/codexbar"), patch(
            "ai_usage_bar.subprocess.run", return_value=CompletedProcess([], 0, payload, "")
        ):
            usage = UsageClient().cost_usage()["codex"]
        self.assertEqual(usage.cost, 3.5)
        self.assertEqual(usage.today_cost, 1.25)
        self.assertEqual(usage.today_tokens, 4000)
        self.assertEqual(usage.daily[0].date, today)
        self.assertEqual(usage.daily[0].models[0].name, "gpt-test")
        self.assertEqual(usage.models[0].name, "gpt-test")
        self.assertEqual(compact_tokens(usage.tokens), "12.0K")


class AntigravityParsingTests(unittest.TestCase):
    def test_parse_summary_groups_windows_and_skips_invalid(self):
        payload = {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {
                            "bucketId": "gemini-5h",
                            "window": "5h",
                            "remainingFraction": 0.75,
                            "resetTime": "2026-07-15T18:00:00Z",
                        },
                        {
                            "bucketId": "gemini-weekly",
                            "window": "weekly",
                            "remainingFraction": 1.0,
                        },
                        {
                            "bucketId": "gemini-disabled",
                            "window": "5h",
                            "remainingFraction": 0.5,
                            "disabled": True,
                        },
                        {
                            "bucketId": "gemini-missing",
                            "window": "5h",
                        },
                        {
                            "bucketId": "gemini-bad",
                            "window": "5h",
                            "remainingFraction": "not-a-number",
                        },
                    ],
                },
                {
                    "displayName": "Third Party",
                    "buckets": [
                        {
                            "bucketId": "3p-5h",
                            "window": "5h",
                            "remainingFraction": 0.4,
                            "resetTime": "2026-07-15T20:00:00Z",
                        },
                        {
                            "bucketId": "custom-bucket",
                            "displayName": "Daily",
                            "remainingFraction": 0.9,
                        },
                    ],
                },
            ]
        }
        entries = parse_antigravity_summary(payload)
        by_id = dict(entries)
        self.assertEqual(set(by_id.keys()), {"gemini-5h", "gemini-weekly", "3p-5h", "custom-bucket"})
        self.assertEqual(by_id["gemini-5h"].label, "Gemini · 5 hour")
        self.assertAlmostEqual(by_id["gemini-5h"].used, 25.0)
        self.assertEqual(by_id["gemini-5h"].duration_seconds, 5 * 3600)
        self.assertEqual(by_id["gemini-5h"].resets_at, datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc))

        self.assertEqual(by_id["gemini-weekly"].label, "Gemini · Weekly")
        self.assertAlmostEqual(by_id["gemini-weekly"].used, 0.0)
        self.assertIsNone(by_id["gemini-weekly"].resets_at)
        self.assertEqual(by_id["gemini-weekly"].duration_seconds, 7 * 86400)

        self.assertEqual(by_id["3p-5h"].label, "Claude & GPT · 5 hour")
        self.assertAlmostEqual(by_id["3p-5h"].used, 60.0)

        self.assertEqual(by_id["custom-bucket"].label, "Third Party · Daily")
        self.assertIsNone(by_id["custom-bucket"].duration_seconds)

    def test_parse_summary_top_level_buckets_fallback(self):
        payload = {
            "buckets": [
                {
                    "bucketId": "gemini-weekly",
                    "window": "weekly",
                    "remainingFraction": 0.2,
                    "resetTime": "2026-07-20T00:00:00Z",
                }
            ]
        }
        entries = parse_antigravity_summary(payload)
        self.assertEqual(len(entries), 1)
        bucket_id, window = entries[0]
        self.assertEqual(bucket_id, "gemini-weekly")
        self.assertEqual(window.label, "Gemini · Weekly")
        self.assertAlmostEqual(window.used, 80.0)

    def test_parse_models_fallback_aggregates_max_used_per_family(self):
        payload = {
            "models": {
                "gemini-2.5-pro": {
                    "modelProvider": "MODEL_PROVIDER_GOOGLE",
                    "quotaInfo": {"remainingFraction": 0.8, "resetTime": "2026-07-15T18:00:00Z"},
                    "weeklyQuotaInfo": {"remainingFraction": 0.9, "resetTime": "2026-07-20T00:00:00Z"},
                },
                "gemini-2.5-flash": {
                    "modelProvider": "MODEL_PROVIDER_GOOGLE",
                    "quotaInfo": {"remainingFraction": 0.35, "resetTime": "2026-07-15T19:00:00Z"},
                },
                "claude-sonnet-4": {
                    "modelProvider": "MODEL_PROVIDER_ANTHROPIC",
                    "quotaInfo": {"remainingFraction": 0.5},
                },
                "claude-opus-4": {
                    "modelProvider": "MODEL_PROVIDER_ANTHROPIC",
                    "quotaInfo": {"remainingFraction": 0.5, "resetTime": "2026-07-15T17:00:00Z"},
                },
                "gpt-5": {
                    "quotaInfo": {"remainingFraction": 0.1},
                },
            }
        }
        by_id = dict(parse_antigravity_models(payload))
        self.assertEqual(set(by_id.keys()), {"gemini-quota", "gemini-weekly", "claude-quota", "gpt-quota"})
        self.assertAlmostEqual(by_id["gemini-quota"].used, 65.0)
        self.assertEqual(by_id["gemini-quota"].label, "Gemini · Quota")
        self.assertAlmostEqual(by_id["gemini-weekly"].used, 10.0)
        self.assertEqual(by_id["gemini-weekly"].duration_seconds, 7 * 86400)
        self.assertAlmostEqual(by_id["claude-quota"].used, 50.0)
        self.assertEqual(by_id["claude-quota"].resets_at, datetime(2026, 7, 15, 17, 0, tzinfo=timezone.utc))
        self.assertAlmostEqual(by_id["gpt-quota"].used, 90.0)

    def test_merge_windows_prefers_higher_used_daily_ties_and_sorts_deterministically(self):
        daily_reset = datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc)
        synthetic_reset = datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc)
        entries = [
            ("gpt-quota", UsageWindow("GPT · Quota", 10.0, None, None)),
            ("3p-weekly", UsageWindow("Claude & GPT · Weekly", 15.0, synthetic_reset, 7 * 86400)),
            ("gemini-weekly", UsageWindow("Gemini · Weekly", 0.0, None, 7 * 86400)),
            ("gemini-weekly", UsageWindow("Gemini · Weekly", 0.0, synthetic_reset, 7 * 86400)),
            ("gemini-5h", UsageWindow("Gemini · 5 hour", 20.0, daily_reset, 5 * 3600)),
            ("gemini-5h", UsageWindow("Gemini · 5 hour", 45.0, synthetic_reset, 5 * 3600)),
            ("3p-5h", UsageWindow("Claude & GPT · 5 hour", 30.0, None, 5 * 3600)),
            ("3p-5h", UsageWindow("Claude & GPT · 5 hour", 30.0, daily_reset, 5 * 3600)),
            ("claude-quota", UsageWindow("Claude · Quota", 5.0, None, None)),
        ]
        windows = merge_antigravity_windows(entries)
        self.assertEqual(
            [w.label for w in windows],
            [
                "Gemini · 5 hour",
                "Gemini · Weekly",
                "Claude & GPT · 5 hour",
                "Claude & GPT · Weekly",
                "Claude · Quota",
                "GPT · Quota",
            ],
        )
        self.assertEqual(windows[0].used, 45.0)
        self.assertEqual(windows[0].resets_at, synthetic_reset)
        self.assertEqual(windows[2].used, 30.0)
        self.assertEqual(windows[2].resets_at, daily_reset)
        self.assertEqual(windows[1].used, 0.0)
        self.assertIsNone(windows[1].resets_at)

    def test_parse_antigravity_plan_rules(self):
        self.assertEqual(
            parse_antigravity_plan(
                {
                    "paidTier": {"id": "pro", "name": "Google AI Pro"},
                    "currentTier": {"id": "free-tier", "name": "Free"},
                },
                has_active_five_hour=True,
            ),
            "Google AI Pro",
        )
        self.assertIsNone(
            parse_antigravity_plan(
                {"currentTier": {"id": "free-tier", "name": "Free"}},
                has_active_five_hour=True,
            )
        )
        self.assertEqual(
            parse_antigravity_plan(
                {"currentTier": {"id": "free-tier", "name": "Free"}},
                has_active_five_hour=False,
            ),
            "Free",
        )
        self.assertIsNone(
            parse_antigravity_plan(
                {"currentTier": {"id": "standard", "name": "Antigravity"}},
                has_active_five_hour=False,
            )
        )


class AntigravityClientTests(unittest.TestCase):
    def test_missing_credentials_returns_friendly_error(self):
        with tempfile.TemporaryDirectory() as directory:
            client = UsageClient(Path(directory))
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ANTIGRAVITY_TOKEN_PATH", None)
                usage = client.antigravity()
        self.assertEqual(usage.name, "Antigravity")
        self.assertEqual(usage.error, "Not signed in — open Antigravity or sign in via T3 Code")

    def test_invalid_credentials_file_returns_unavailable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            token_path = home / ".gemini/antigravity/antigravity-oauth-token"
            token_path.parent.mkdir(parents=True)
            token_path.write_text("{broken-json")
            client = UsageClient(home)
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ANTIGRAVITY_TOKEN_PATH", None)
                usage = client.antigravity()
        self.assertEqual(usage.error, "Antigravity credentials are unavailable")

    def test_oauth_token_picks_freshest_valid_and_merges_endpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            expired_path = home / ".gemini/antigravity/antigravity-oauth-token"
            expired_path.parent.mkdir(parents=True)
            expired_path.write_text(
                json.dumps({
                    "token": {
                        "access_token": "expired-token",
                        "expiry": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
                    }
                })
            )
            fresh_path = home / ".gemini/antigravity-cli/antigravity-oauth-token"
            fresh_path.parent.mkdir(parents=True)
            fresh_path.write_text(
                json.dumps({
                    "token": {
                        "access_token": "fresh-token",
                        "expiry": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                    }
                })
            )

            seen_calls: list[tuple[str, str, dict]] = []

            def fake_post(url: str, headers: dict, body: dict):
                seen_calls.append((url, headers["Authorization"], body))
                if url.endswith(":loadCodeAssist"):
                    return {
                        "paidTier": {"name": "Google AI Pro"},
                        "cloudaicompanionProject": "proj-123",
                    }
                if url.startswith(ANTIGRAVITY_ENDPOINTS[0]):
                    return {
                        "groups": [{
                            "buckets": [
                                {"bucketId": "gemini-5h", "window": "5h", "remainingFraction": 0.6, "resetTime": "2026-07-15T18:00:00Z"},
                                {"bucketId": "gemini-weekly", "window": "weekly", "remainingFraction": 1.0},
                            ]
                        }]
                    }
                return {
                    "groups": [{
                        "buckets": [
                            {"bucketId": "gemini-weekly", "window": "weekly", "remainingFraction": 1.0, "resetTime": "2026-07-22T00:00:00Z"},
                            {"bucketId": "3p-5h", "window": "5h", "remainingFraction": 0.25, "resetTime": "2026-07-15T19:00:00Z"},
                        ]
                    }]
                }

            client = UsageClient(home)
            with patch.dict(os.environ, {}, clear=False), patch.object(client, "_json_post", side_effect=fake_post):
                os.environ.pop("ANTIGRAVITY_TOKEN_PATH", None)
                usage = client.antigravity()

        self.assertIsNone(usage.error)
        self.assertEqual(usage.plan, "Google AI Pro")
        self.assertEqual([w.label for w in usage.windows], ["Gemini · 5 hour", "Gemini · Weekly", "Claude & GPT · 5 hour"])
        self.assertIsNone(usage.windows[1].resets_at)
        self.assertTrue(all(auth == "Bearer fresh-token" for _url, auth, _body in seen_calls))
        self.assertEqual(seen_calls[1][2], {"project": "proj-123"})

    def test_acp_token_refreshes_and_caches_in_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            acp_path = home / ".t3/userdata/providers/antigravity/profile1/antigravity-acp/acp_token.json"
            acp_path.parent.mkdir(parents=True)
            original_content = json.dumps({
                "client_id": "cid",
                "client_secret": "csecret",
                "refresh_token": "rtoken",
                "project_id": "proj-abc",
            })
            acp_path.write_text(original_content)

            refresh_calls = 0

            def fake_form_post(_url: str, form: dict[str, str]):
                nonlocal refresh_calls
                refresh_calls += 1
                self.assertEqual(form["grant_type"], "refresh_token")
                return {"access_token": f"access-{refresh_calls}", "expires_in": 3600}

            def fake_json_post(url: str, headers: dict, _body: dict):
                if url.endswith(":loadCodeAssist"):
                    return {"currentTier": {"id": "free-tier", "name": "Free"}}
                return {
                    "buckets": [
                        {"bucketId": "gemini-5h", "window": "5h", "remainingFraction": 0.5, "resetTime": "2026-07-15T18:00:00Z"}
                    ]
                }

            client = UsageClient(home)
            with (
                patch.dict(os.environ, {}, clear=False),
                patch.object(client, "_form_post", side_effect=fake_form_post),
                patch.object(client, "_json_post", side_effect=fake_json_post),
            ):
                os.environ.pop("ANTIGRAVITY_TOKEN_PATH", None)
                first = client.antigravity()
                second = client.antigravity()

            self.assertEqual(refresh_calls, 1)
            self.assertIsNone(first.error)
            self.assertIsNone(first.plan)
            self.assertEqual(len(second.windows), 1)
            self.assertEqual(acp_path.read_text(), original_content)

    def test_http_401_reports_session_expired(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            token_path = home / ".gemini/antigravity/antigravity-oauth-token"
            token_path.parent.mkdir(parents=True)
            token_path.write_text(json.dumps({"token": {"access_token": "stale"}}))

            http_401 = urllib.error.HTTPError(
                f"{ANTIGRAVITY_ENDPOINTS[0]}/v1internal:loadCodeAssist",
                401,
                "Unauthorized",
                hdrs={},  # type: ignore[arg-type]
                fp=io.BytesIO(b""),
            )
            client = UsageClient(home)
            with patch.dict(os.environ, {}, clear=False), patch.object(client, "_json_post", side_effect=http_401):
                os.environ.pop("ANTIGRAVITY_TOKEN_PATH", None)
                usage = client.antigravity()

        self.assertEqual(usage.error, "Session expired — open Antigravity to refresh")

    def test_empty_summary_falls_back_to_fetch_available_models(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            token_path = home / ".gemini/antigravity/antigravity-oauth-token"
            token_path.parent.mkdir(parents=True)
            token_path.write_text(json.dumps({"token": {"access_token": "tok"}}))

            def fake_post(url: str, _headers: dict, _body: dict):
                if url.endswith(":loadCodeAssist"):
                    return {}
                if url.endswith(":retrieveUserQuotaSummary"):
                    return {"groups": []}
                if url.endswith(":fetchAvailableModels") and url.startswith(ANTIGRAVITY_ENDPOINTS[0]):
                    return {
                        "models": {
                            "gemini-2.5-pro": {
                                "modelProvider": "MODEL_PROVIDER_GOOGLE",
                                "quotaInfo": {"remainingFraction": 0.3},
                            }
                        }
                    }
                return {"models": {}}

            client = UsageClient(home)
            with patch.dict(os.environ, {}, clear=False), patch.object(client, "_json_post", side_effect=fake_post):
                os.environ.pop("ANTIGRAVITY_TOKEN_PATH", None)
                usage = client.antigravity()

        self.assertIsNone(usage.error)
        self.assertEqual([w.label for w in usage.windows], ["Gemini · Quota"])
        self.assertAlmostEqual(usage.windows[0].used, 70.0)

    def test_explicit_token_path_and_network_error(self):
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "custom-token.json"
            token_path.write_text(json.dumps({"token": {"access_token": "custom-tok"}}))
            client = UsageClient(Path(directory))
            with (
                patch.dict(os.environ, {"ANTIGRAVITY_TOKEN_PATH": str(token_path)}, clear=False),
                patch.object(client, "_json_post", side_effect=urllib.error.URLError("offline")),
            ):
                usage = client.antigravity()
        self.assertEqual(usage.error, "Could not reach Google")

    def test_fetch_returns_all_three_providers(self):
        with tempfile.TemporaryDirectory() as directory:
            client = UsageClient(Path(directory))
            with (
                patch.object(client, "claude", return_value=ProviderUsage("Claude")),
                patch.object(client, "codex", return_value=ProviderUsage("Codex")),
                patch.object(client, "antigravity", return_value=ProviderUsage("Antigravity")),
                patch.object(client, "cost_usage", return_value={}),
            ):
                providers = client.fetch()
        self.assertEqual([p.name for p in providers], ["Claude", "Codex", "Antigravity"])
        self.assertIsNone(providers[2].local_usage)

    def test_stale_acp_refresh_falls_back_to_valid_oauth_token(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            acp_path = home / ".t3/userdata/providers/antigravity/stale/antigravity-acp/acp_token.json"
            acp_path.parent.mkdir(parents=True)
            acp_path.write_text(json.dumps({
                "client_id": "cid",
                "client_secret": "csec",
                "refresh_token": "revoked-refresh",
            }))
            oauth_path = home / ".gemini/antigravity/antigravity-oauth-token"
            oauth_path.parent.mkdir(parents=True)
            oauth_path.write_text(json.dumps({
                "token": {
                    "access_token": "valid-oauth-tok",
                    "expiry": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                }
            }))

            http_400 = urllib.error.HTTPError(
                "https://oauth2.googleapis.com/token", 400, "Bad Request", hdrs={}, fp=io.BytesIO(b"")  # type: ignore[arg-type]
            )
            seen_auth: list[str] = []

            def fake_post(url: str, headers: dict, _body: dict):
                seen_auth.append(headers["Authorization"])
                if url.endswith(":loadCodeAssist"):
                    return {"paidTier": {"name": "Google AI Pro"}}
                if url.endswith(":retrieveUserQuotaSummary") and url.startswith(ANTIGRAVITY_ENDPOINTS[0]):
                    return {"groups": [{"buckets": [{"bucketId": "gemini-5h", "window": "5h", "remainingFraction": 0.6}]}]}
                return {"groups": []}

            client = UsageClient(home)
            with (
                patch.dict(os.environ, {}, clear=False),
                patch.object(client, "_form_post", side_effect=http_400),
                patch.object(client, "_json_post", side_effect=fake_post),
            ):
                os.environ.pop("ANTIGRAVITY_TOKEN_PATH", None)
                usage = client.antigravity()

        self.assertIsNone(usage.error)
        self.assertTrue(all(h == "Bearer valid-oauth-tok" for h in seen_auth))
        self.assertAlmostEqual(usage.windows[0].used, 40.0)

    def test_403_on_load_code_assist_and_summary_still_tries_remaining_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            token_path = home / ".gemini/antigravity/antigravity-oauth-token"
            token_path.parent.mkdir(parents=True)
            token_path.write_text(json.dumps({"token": {"access_token": "tok"}, "project_id": "proj-1"}))

            http_403 = urllib.error.HTTPError("https://example.invalid", 403, "Forbidden", hdrs={}, fp=io.BytesIO(b""))  # type: ignore[arg-type]

            def fake_post(url: str, _headers: dict, _body: dict):
                if url.endswith(":loadCodeAssist"):
                    raise http_403
                if url.endswith(":retrieveUserQuotaSummary") and url.startswith(ANTIGRAVITY_ENDPOINTS[0]):
                    raise http_403
                if url.endswith(":retrieveUserQuotaSummary"):
                    return {"groups": []}
                if url.endswith(":fetchAvailableModels") and url.startswith(ANTIGRAVITY_ENDPOINTS[0]):
                    return {
                        "models": {
                            "gemini-2.5-pro": {
                                "modelProvider": "MODEL_PROVIDER_GOOGLE",
                                "quotaInfo": {"remainingFraction": 0.4},
                            }
                        }
                    }
                return {"models": {}}

            client = UsageClient(home)
            with patch.dict(os.environ, {}, clear=False), patch.object(client, "_json_post", side_effect=fake_post):
                os.environ.pop("ANTIGRAVITY_TOKEN_PATH", None)
                usage = client.antigravity()

        self.assertIsNone(usage.error)
        self.assertEqual([w.label for w in usage.windows], ["Gemini · Quota"])
        self.assertAlmostEqual(usage.windows[0].used, 60.0)

    def test_all_403_reports_permission_denied_distinct_from_session_expired(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            token_path = home / ".gemini/antigravity/antigravity-oauth-token"
            token_path.parent.mkdir(parents=True)
            token_path.write_text(json.dumps({"token": {"access_token": "tok"}}))
            http_403 = urllib.error.HTTPError("https://example.invalid", 403, "Forbidden", hdrs={}, fp=io.BytesIO(b""))  # type: ignore[arg-type]
            client = UsageClient(home)
            with patch.dict(os.environ, {}, clear=False), patch.object(client, "_json_post", side_effect=http_403):
                os.environ.pop("ANTIGRAVITY_TOKEN_PATH", None)
                usage = client.antigravity()
        self.assertEqual(usage.error, "Antigravity quota access denied")

    def test_unrecognized_summary_does_not_mask_fallback_http_500(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            token_path = home / ".gemini/antigravity/antigravity-oauth-token"
            token_path.parent.mkdir(parents=True)
            token_path.write_text(json.dumps({"token": {"access_token": "tok"}}))
            http_500 = urllib.error.HTTPError("https://example.invalid", 500, "Server Error", hdrs={}, fp=io.BytesIO(b""))  # type: ignore[arg-type]

            def fake_post(url: str, _headers: dict, _body: dict):
                if url.endswith(":loadCodeAssist"):
                    return {}
                if url.endswith(":retrieveUserQuotaSummary"):
                    return {}
                raise http_500

            client = UsageClient(home)
            with patch.dict(os.environ, {}, clear=False), patch.object(client, "_json_post", side_effect=fake_post):
                os.environ.pop("ANTIGRAVITY_TOKEN_PATH", None)
                usage = client.antigravity()
        self.assertEqual(usage.error, "Service returned HTTP 500")

    def test_naive_oauth_expiry_is_accepted_without_type_error(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            token_path = home / ".gemini/antigravity/antigravity-oauth-token"
            token_path.parent.mkdir(parents=True)
            naive_future = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(tzinfo=None).isoformat()
            token_path.write_text(json.dumps({"token": {"access_token": "naive-tok", "expiry": naive_future}}))

            def fake_post(url: str, headers: dict, _body: dict):
                self.assertEqual(headers["Authorization"], "Bearer naive-tok")
                if url.endswith(":retrieveUserQuotaSummary") and url.startswith(ANTIGRAVITY_ENDPOINTS[0]):
                    return {"groups": [{"buckets": [{"bucketId": "gemini-5h", "window": "5h", "remainingFraction": 0.9}]}]}
                return {"groups": []}

            client = UsageClient(home)
            with patch.dict(os.environ, {}, clear=False), patch.object(client, "_json_post", side_effect=fake_post):
                os.environ.pop("ANTIGRAVITY_TOKEN_PATH", None)
                usage = client.antigravity()
        self.assertIsNone(usage.error)
        self.assertAlmostEqual(usage.windows[0].used, 10.0)

    def test_incomplete_read_is_handled_and_cross_origin_redirect_blocked(self):
        client = UsageClient()
        req = urllib.request.Request("https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary")
        with patch("ai_usage_bar._HTTP_OPENER.open", side_effect=http.client.IncompleteRead(b"partial")):
            with self.assertRaises(urllib.error.URLError):
                client._open_json(req)

        handler = _SameOriginRedirectHandler()
        with self.assertRaises(urllib.error.URLError):
            handler.redirect_request(
                req, None, 302, "Found", {}, "https://evil.example.com/steal"
            )
        same_origin = handler.redirect_request(
            req, None, 302, "Found", {}, "https://daily-cloudcode-pa.googleapis.com/other"
        )
        self.assertIsNotNone(same_origin)

    def test_fetch_worker_always_emits_done_on_unexpected_exception(self):
        client = UsageClient()
        emitted: list[list[ProviderUsage]] = []
        with patch.object(client, "fetch", side_effect=RuntimeError("boom")):
            worker = FetchWorker(client)
            worker.signals.done.connect(emitted.append)
            worker.run()
        self.assertEqual(len(emitted), 1)
        self.assertEqual([p.name for p in emitted[0]], [spec.label for spec in PROVIDERS])
        self.assertTrue(all(p.error == "Refresh failed" for p in emitted[0]))

    def test_non_object_acp_refresh_response_falls_back_to_oauth_token(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            acp_path = home / ".t3/userdata/providers/antigravity/bad/antigravity-acp/acp_token.json"
            acp_path.parent.mkdir(parents=True)
            acp_path.write_text(json.dumps({
                "client_id": "cid",
                "client_secret": "csec",
                "refresh_token": "tok",
            }))
            oauth_path = home / ".gemini/antigravity/antigravity-oauth-token"
            oauth_path.parent.mkdir(parents=True)
            oauth_path.write_text(json.dumps({"token": {"access_token": "fallback-tok"}}))

            def fake_post(url: str, headers: dict, _body: dict):
                self.assertEqual(headers["Authorization"], "Bearer fallback-tok")
                if url.endswith(":retrieveUserQuotaSummary") and url.startswith(ANTIGRAVITY_ENDPOINTS[0]):
                    return {"groups": [{"buckets": [{"bucketId": "gemini-5h", "window": "5h", "remainingFraction": 0.75}]}]}
                return {"groups": []}

            client = UsageClient(home)
            with (
                patch.dict(os.environ, {}, clear=False),
                patch.object(client, "_form_post", return_value=[]),
                patch.object(client, "_json_post", side_effect=fake_post),
            ):
                os.environ.pop("ANTIGRAVITY_TOKEN_PATH", None)
                usage = client.antigravity()
        self.assertIsNone(usage.error)
        self.assertAlmostEqual(usage.windows[0].used, 25.0)


class QtUiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            cls.app = QApplication.instance() or QApplication([])
        except Exception as exc:
            raise unittest.SkipTest(f"QApplication unavailable: {exc}")

    def test_popup_three_tabs_icon_and_untouched_lane_text(self):
        popup = UsagePopup()
        self.assertEqual(list(popup.tab_buttons.keys()), [spec.key for spec in PROVIDERS])
        self.assertEqual(len(popup.tab_buttons), 3)
        for spec in PROVIDERS:
            icon = provider_icon(spec.key)
            self.assertFalse(icon.isNull(), f"icon for {spec.key} should not be null")
            pixmap = icon.pixmap(QSize(21, 21))
            image = pixmap.toImage()
            has_visible_pixel = any(
                image.pixelColor(x, y).alpha() > 0
                for x in range(image.width())
                for y in range(image.height())
            )
            self.assertTrue(has_visible_pixel, f"rendered icon for {spec.key} should have visible pixels")

        providers = [
            ProviderUsage("Claude", plan="Max", windows=[UsageWindow("5 hour", 0.0, None, 5 * 3600)]),
            ProviderUsage("Codex", plan="Plus", windows=[UsageWindow("Weekly", 50.0, None, 7 * 86400)]),
            ProviderUsage(
                "Antigravity",
                plan="Google AI Pro",
                windows=[UsageWindow("Gemini · 5 hour", 0.0, None, 5 * 3600, starts_on_use=True)],
            ),
        ]
        popup.set_data(providers)
        initial_height = popup.height()
        popup.set_filter("antigravity")
        self.assertEqual(popup.height(), initial_height)
        self.assertEqual(len(popup.cards), 3)
        self.assertEqual(popup.cards[0].lanes[0].reset.text(), "reset unknown")
        antigravity_card = popup.cards[2]
        self.assertEqual(antigravity_card.lanes[0].reset.text(), "starts on use")

        touched_lane = UsageLane(UsageWindow("Gemini · 5 hour", 15.0, None, 5 * 3600))
        self.assertEqual(touched_lane.reset.text(), "reset unknown")

        tray_icon = TrayApp.make_icon(providers)
        self.assertFalse(tray_icon.isNull())
        self.assertEqual(tray_icon.pixmap(QSize(44, 44)).size(), QSize(44, 44))

        default_icon = TrayApp.make_icon([])
        self.assertFalse(default_icon.isNull())
        popup.close()


if __name__ == "__main__":
    unittest.main()
