#!/usr/bin/env python3
"""Unit tests for the agent usage collector. No network and no home directory."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collector"))
import agent_usage as usage


NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


class FormatTests(unittest.TestCase):
    def test_token_labels(self):
        self.assertEqual(usage.format_tokens(999), "999")
        self.assertEqual(usage.format_tokens(12000), "1.2万")
        self.assertEqual(usage.format_tokens(120000), "12万")
        self.assertEqual(usage.format_tokens(100000000), "1亿")

    def test_pace_follows_the_window(self):
        window = 7 * 86400
        resets = NOW + timedelta(seconds=window * 0.3)
        self.assertEqual(usage.limit_tone(0.76, resets, window, NOW), "hot")
        self.assertEqual(usage.limit_tone(0.58, resets, window, NOW), "ok")
        self.assertEqual(usage.limit_tone(0.91, resets, window, NOW), "hot")

    def test_fractional_timestamp(self):
        parsed = usage.parse_time("2026-10-28T21:20:28.156344483Z")
        self.assertEqual(parsed.year, 2026)
        self.assertEqual(parsed.second, 28)


class LimitTests(unittest.TestCase):
    def test_grok_weekly_credits(self):
        limit = usage.credits_limit({
            "config": {
                "currentPeriod": {"type": "WEEKLY", "end": "2026-11-04T00:00:00Z"},
                "creditUsagePercent": 31,
            }
        }, NOW)
        self.assertEqual(limit["label"], "每周")
        self.assertAlmostEqual(limit["percent"], 0.31)

    def test_unified_billing_has_no_weekly_window(self):
        limit = usage.credits_limit({
            "config": {
                "isUnifiedBillingUser": True,
                "currentPeriod": {"type": "WEEKLY", "end": "2026-11-04T00:00:00Z"},
            }
        }, NOW)
        self.assertIsNone(limit)

    def test_protobuf_amounts(self):
        limit = usage.monthly_limit({
            "config": {
                "monthlyLimit": {"val": 200},
                "used": {"val": 50},
                "billingPeriodEnd": "2026-11-01T00:00:00Z",
            }
        })
        self.assertAlmostEqual(limit["percent"], 0.25)
        self.assertEqual(limit["label"], "每月")

    def test_claude_windows_and_scoped_model(self):
        limits = usage.claude_limits_from_payload({
            "five_hour": {"utilization": 12, "resets_at": "2026-10-28T16:00:00Z"},
            "seven_day": {"utilization": 40, "resets_at": "2026-11-01T00:00:00Z"},
            "limits": [{
                "kind": "weekly_scoped",
                "percent": 8,
                "resets_at": "2026-11-01T00:00:00Z",
                "scope": {"model": {"display_name": "Fable"}},
            }],
        })
        self.assertEqual([item["label"] for item in limits], ["会话（5 小时）", "每周（7 天）", "Fable 每周"])
        self.assertEqual([item["model"] for item in limits], ["", "", "Fable"])

    def test_codex_buckets(self):
        limits, plan = usage.codex_limits_from_result({
            "rateLimitsByLimitId": {
                "codex": {
                    "planType": "plus",
                    "primary": {"usedPercent": 18, "windowDurationMins": 300, "resetsAt": 1761667200},
                    "secondary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 1762000000},
                }
            }
        })
        self.assertEqual(plan, "Plus")
        self.assertEqual([item["label"] for item in limits], ["会话", "每周"])
        self.assertEqual([item["model"] for item in limits], ["", ""])
        self.assertAlmostEqual(limits[0]["percent"], 0.18)

    def test_host_pin_does_not_touch_the_network(self):
        with self.assertRaises(urllib.error.URLError):
            usage.fetch_json("https://example.com/usage", "secret-token", "cli-chat-proxy.grok.com")


class ScanTests(unittest.TestCase):
    def test_codex_prefers_usage_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            rows = [
                {"timestamp": "2026-10-28T01:00:00Z", "type": "session_meta", "payload": {"session_id": "s1"}},
                {"timestamp": "2026-10-28T01:01:00Z", "type": "turn_context", "payload": {"model": "gpt-test"}},
                {"timestamp": "2026-10-28T01:02:00Z", "type": "token_usage_record", "payload": {"usage": {"total_tokens": 100}}},
                {"timestamp": "2026-10-28T01:02:00Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"total_tokens": 9999}}}},
                {"timestamp": "2026-10-28T01:03:00Z", "type": "event_msg", "payload": {"type": "user_message"}},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            part = usage.scan_codex_file(path)
        self.assertEqual(sum(part["tokenDays"].values()), 100)
        self.assertEqual(part["models"].get("gpt-test"), 100)
        self.assertEqual(part["prompts"], 1)
        self.assertEqual(part["projects"], {})

    def test_codex_project_prefers_repository_name(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            rows = [
                {
                    "timestamp": "2026-10-28T01:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "cwd": "/tmp/project",
                        "git": {"repository_url": "https://user:secret@github.com/org/fedora_enhance.git"},
                    },
                },
                {"timestamp": "2026-10-28T01:01:00Z", "type": "turn_context", "payload": {"model": "gpt-test", "cwd": "/tmp/other"}},
                {"timestamp": "2026-10-28T01:02:00Z", "type": "token_usage_record", "payload": {"usage": {"total_tokens": 100}}},
                {"timestamp": "2026-10-28T01:02:00Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"total_tokens": 9999}}}},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            part = usage.scan_codex_file(path)
        self.assertEqual(part["projects"], {"fedora_enhance": 100})
        self.assertNotIn("secret", json.dumps(part))

    def test_claude_project_comes_from_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            rows = [
                {"type": "system", "cwd": "/home/tope/Documents/slicer5-projects", "timestamp": "2026-10-28T02:00:00Z"},
                {
                    "type": "assistant",
                    "uuid": "m1",
                    "sessionId": "s1",
                    "timestamp": "2026-10-28T02:00:01Z",
                    "message": {"model": "claude-test", "usage": {"input_tokens": 10, "output_tokens": 5}},
                },
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            part = usage.scan_claude_file(path)
        self.assertEqual(part["projects"].get("slicer5-projects"), 15)

    def test_project_labels_drop_userinfo_and_agent_dirs(self):
        self.assertEqual(usage.repository_label("git@github.com:org/OrcaSlicer.git"), "OrcaSlicer")
        self.assertEqual(usage.path_project("%2Fhome%2Ftope%2FDocuments%2Ffedora_enhance"), "fedora_enhance")
        self.assertEqual(usage.path_project("/tmp/agent-aa87c9488a6f07583"), "subagent")
        self.assertEqual(usage.model_name('{"id":"MiniMax-M3","providerID":"example"}'), "MiniMax-M3")
        self.assertEqual(usage.model_name({"id": "gpt-6-astra"}), "gpt-6-astra")

    def test_merge_keeps_every_model_and_project(self):
        today = NOW.date()
        stats = usage.merge_parts([
            {
                "prompts": 0,
                "promptDays": {},
                "sessionDays": {},
                "tokenDays": {"2026-10-28": 15},
                "models": {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5},
                "dayModels": {},
                "projects": {"slicer5-projects": 10},
            },
            {
                "prompts": 0,
                "promptDays": {},
                "sessionDays": {},
                "tokenDays": {"2026-10-28": 7},
                "models": {"e": 7},
                "dayModels": {},
                "projects": {"slicer5-projects": 4, "fedora_enhance": 3},
            },
        ], today)
        self.assertEqual([item["name"] for item in stats["models"]], ["e", "d", "c", "b", "a"])
        self.assertEqual(stats["projects"][0], {"name": "slicer5-projects", "tokens": 14})
        self.assertEqual(stats["projects"][1], {"name": "fedora_enhance", "tokens": 3})

    def test_claude_transcript_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            row = {
                "type": "assistant",
                "uuid": "m1",
                "sessionId": "s1",
                "timestamp": "2026-10-28T02:00:00Z",
                "message": {
                    "model": "claude-test",
                    "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 2, "cache_creation_input_tokens": 1},
                },
            }
            path.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
            part = usage.scan_claude_file(path)
        self.assertEqual(sum(part["tokenDays"].values()), 18)
        self.assertEqual(part["prompts"], 0)

    def test_snapshot_scrub_removes_secrets(self):
        cleaned = usage.scrub({
            "email": "person@example.com",
            "note": "Bearer abc.def.ghi",
            "nested": {"access_token": "secret", "ok": "fine"},
            "agents": [{"help": "sk-ant-secret-value"}],
        })
        self.assertNotIn("email", cleaned)
        self.assertNotIn("access_token", cleaned["nested"])
        self.assertEqual(cleaned["note"], "")
        self.assertEqual(cleaned["agents"][0]["help"], "")

    def test_grok_limit_uses_fake_fetch_and_isolated_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            os.environ["XDG_CACHE_HOME"] = directory
            home = Path(directory) / "grok"
            home.mkdir()
            (home / "auth.json").write_text(json.dumps({
                "issuer": {
                    "key": "test-token",
                    "expires_at": "2026-10-28T18:00:00Z",
                    "refresh_token": "refresh",
                }
            }), encoding="utf-8")

            def fetch(url, token, host):
                self.assertEqual(token, "test-token")
                self.assertEqual(host, "cli-chat-proxy.grok.com")
                if "format=credits" in url:
                    return {"config": {"currentPeriod": {"type": "WEEKLY", "end": "2026-11-04T00:00:00Z"}, "creditUsagePercent": 20}}
                raise AssertionError(url)

            result = usage.collect_grok_limits(home, force=True, now=NOW, fetch=fetch)
        self.assertEqual(result["status"], "")
        self.assertAlmostEqual(result["limits"][0]["percent"], 0.2)
        self.assertNotIn("test-token", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
