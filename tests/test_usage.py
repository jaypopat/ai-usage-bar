import json
import tempfile
import unittest
from subprocess import CompletedProcess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from ai_usage_bar import UsageClient, compact_tokens, lane_label, parse_claude, parse_codex, remaining_text


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
        payload = json.dumps([{
            "provider": "codex", "historyDays": 30,
            "totals": {"totalCost": 3.5, "totalTokens": 12000},
            "daily": [{"totalCost": 1.25, "totalTokens": 4000, "modelBreakdowns": [
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
        self.assertEqual(usage.models[0].name, "gpt-test")
        self.assertEqual(compact_tokens(usage.tokens), "12.0K")


if __name__ == "__main__":
    unittest.main()
