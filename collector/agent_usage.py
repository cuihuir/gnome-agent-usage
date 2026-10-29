#!/usr/bin/env python3
"""Collect Claude, Codex, Grok and OpenCode usage into one display snapshot.

Purpose: give the GNOME Agent Usage extension the same kind of record
Omarchy's Agents panel reads: subscription windows when a sign-in can
answer them, otherwise the tokens already stored on this machine.
Dependency: Python 3.11+ standard library. No third-party packages.
Effect: reads agent homes, may GET Grok's billing endpoint with the CLI's
existing access token, and can write ~/.local/state/gnome-agent-usage/.
Does not print, log, or store credentials.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

SCHEMA_VERSION = 1
REUSE_SECONDS = 900
FILE_CACHE_VERSION = 3
GROK_CREDITS_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
GROK_MONTHLY_URL = "https://cli-chat-proxy.grok.com/v1/billing"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
REFRESH_LIFETIME_SECONDS = 30 * 24 * 3600
_AGENT_DIR = re.compile(r"agent-[0-9a-f]{8,}$")

CODEX_PLAN_NAMES = {
    "free": "Free",
    "go": "Go",
    "plus": "Plus",
    "pro": "Pro 200",
    "prolite": "Pro 100",
    "promax": "Pro 500",
    "team": "Business",
    "self_serve_business_usage_based": "Business",
    "self_serve_business_prolite": "Business Premium",
    "business": "Enterprise",
    "enterprise": "Enterprise",
    "edu": "Edu",
    "edu_plus": "Edu Plus",
    "edu_pro": "Edu Pro",
}


def format_tokens(value) -> str:
    """Compact token counts the way a Chinese desktop label reads them."""
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        number = 0
    if number < 0:
        number = 0
    if number < 10000:
        return str(number)
    if number < 100_000_000:
        scaled = number / 10000
        text = f"{scaled:.1f}".rstrip("0").rstrip(".") if scaled < 100 else str(round(scaled))
        return f"{text}万"
    scaled = number / 100_000_000
    text = f"{scaled:.1f}".rstrip("0").rstrip(".")
    return f"{text}亿"


def parse_time(value):
    """Parse an ISO time, epoch seconds, or epoch milliseconds."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            return None
        stamp = float(value)
        if stamp > 1e12:
            stamp /= 1000
        try:
            return datetime.fromtimestamp(stamp, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return parse_time(int(text))
    text = text.replace("Z", "+00:00")
    dot = text.find(".")
    if dot != -1:
        end = dot + 1
        while end < len(text) and text[end].isdigit():
            end += 1
        if end - (dot + 1) > 6:
            text = text[: dot + 7] + text[end:]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def local_day(value) -> str:
    parsed = parse_time(value)
    if parsed is None:
        return ""
    try:
        return parsed.astimezone().date().isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def recent_dates(today):
    return [(today - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)]


def amount(value) -> float:
    """A plain number, or a protobuf JSON field shaped {\"val\": n}."""
    if isinstance(value, dict):
        value = value.get("val") or 0
    if isinstance(value, bool):
        raise ValueError("not a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("not a number")
    return number


def clamp_ratio(value: float) -> float:
    return max(0.0, min(1.0, value))


def limit_tone(percent: float, resets_at, window_seconds, now: datetime) -> str:
    """Red when the window is nearly full, or ahead of its prorated pace."""
    if percent >= 0.9:
        return "hot"
    resets = parse_time(resets_at)
    if window_seconds and resets is not None:
        span = float(window_seconds)
        elapsed = (now.timestamp() - (resets.timestamp() - span)) / span
        elapsed = min(1.0, max(0.0, elapsed))
        if percent > elapsed + 0.05:
            return "hot"
    if percent >= 0.75:
        return "warn"
    return "ok"


def model_name(value) -> str:
    """A model id from a string, a JSON object, or a JSON string."""
    if isinstance(value, dict):
        return str(value.get("id") or value.get("name") or "").strip()
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return ""
        if isinstance(obj, dict):
            return str(obj.get("id") or obj.get("name") or "").strip()
    return text


def clean_project(name: str) -> str:
    """A folder or repository label safe to put on the panel."""
    text = str(name or "").strip()
    if text.endswith(".git"):
        text = text[:-4]
    if not text or text in {".", ".."} or "/" in text or "\\" in text or "@" in text:
        return ""
    if _AGENT_DIR.fullmatch(text):
        return "subagent"
    if len(text) > 48:
        text = text[:47] + "…"
    return text


def repository_label(url: str) -> str:
    """Repository name from a git URL, without userinfo or the rest of the path."""
    text = unquote(str(url or "").strip())
    if not text:
        return ""
    if "://" in text:
        text = urlparse(text).path or ""
    elif ":" in text:
        host, _sep, rest = text.partition(":")
        if "@" in host or "/" not in host:
            text = rest
    name = text.rstrip("/").split("/")[-1]
    return clean_project(name)


def path_project(value: str) -> str:
    """Last folder of a filesystem path. Percent-encoded paths are decoded first."""
    text = unquote(str(value or "").strip())
    if not text:
        return ""
    return clean_project(Path(text.rstrip("/\\")).name)


def empty_part() -> dict:
    return {
        "prompts": 0,
        "promptDays": {},
        "sessionDays": {},
        "tokenDays": {},
        "models": {},
        "dayModels": {},
        "projects": {},
    }


def _add(bucket: dict, key: str, amount_value: int) -> None:
    if not key or amount_value <= 0:
        return
    bucket[key] = int(bucket.get(key) or 0) + int(amount_value)


def _add_session(part: dict, day: str, session: str) -> None:
    if not day or not session:
        return
    days = part["sessionDays"].setdefault(day, [])
    if session not in days:
        days.append(session)


def merge_parts(parts: list[dict], today) -> dict:
    merged = empty_part()
    sessions: dict[str, set] = {}
    for part in parts:
        if not isinstance(part, dict):
            continue
        merged["prompts"] += int(part.get("prompts") or 0)
        for day, count in (part.get("promptDays") or {}).items():
            _add(merged["promptDays"], str(day), int(count or 0))
        for day, count in (part.get("tokenDays") or {}).items():
            _add(merged["tokenDays"], str(day), int(count or 0))
        for name, count in (part.get("models") or {}).items():
            _add(merged["models"], str(name), int(count or 0))
        for name, count in (part.get("projects") or {}).items():
            _add(merged["projects"], str(name), int(count or 0))
        for day, models in (part.get("dayModels") or {}).items():
            if not isinstance(models, dict):
                continue
            target = merged["dayModels"].setdefault(str(day), {})
            for name, count in models.items():
                _add(target, str(name), int(count or 0))
        for day, values in (part.get("sessionDays") or {}).items():
            if isinstance(values, list):
                sessions.setdefault(str(day), set()).update(str(item) for item in values if item)
            elif values:
                sessions.setdefault(str(day), set()).add(str(day))
    merged["sessionDays"] = {day: sorted(items) for day, items in sessions.items()}
    days = recent_dates(today)
    today_key = today.isoformat()
    week = [day for day in days]
    token_days = merged["tokenDays"]
    models_today = merged["dayModels"].get(today_key) or {}

    def ranked(models: dict) -> list[dict]:
        ordered = sorted(models.items(), key=lambda item: item[1], reverse=True)
        return [{"name": name, "tokens": int(tokens)} for name, tokens in ordered if int(tokens) > 0]

    return {
        "todayTokens": int(token_days.get(today_key) or 0),
        "todayPrompts": int(merged["promptDays"].get(today_key) or 0),
        "todaySessions": len(sessions.get(today_key) or ()),
        "weekTokens": sum(int(token_days.get(day) or 0) for day in week),
        "totalTokens": sum(int(value or 0) for value in token_days.values()),
        "totalPrompts": int(merged["prompts"] or 0),
        "recentDays": [{"date": day, "tokens": int(token_days.get(day) or 0)} for day in days],
        "models": ranked(merged["models"]),
        "modelsToday": ranked(models_today),
        "projects": ranked(merged["projects"]),
        "hasLocalStats": bool(token_days or merged["prompts"] or sessions),
    }


def finish_limit(limit: dict, now: datetime) -> dict:
    percent = clamp_ratio(float(limit.get("percent") or 0))
    window = int(limit.get("windowSeconds") or 0)
    resets = limit.get("resetsAt") or ""
    item = {
        "id": str(limit.get("id") or limit.get("label") or "limit"),
        "label": str(limit.get("label") or "额度"),
        "percent": percent,
        "resetsAt": resets,
        "windowSeconds": window,
        "tone": limit_tone(percent, resets, window, now),
    }
    parsed = parse_time(resets)
    if parsed is not None and parsed.timestamp() <= now.timestamp():
        item["percent"] = 0
        item["tone"] = "ok"
    return item


def scrub(value):
    """Drop anything that could carry a credential before a record is printed."""
    blocked = {
        "key",
        "token",
        "authorization",
        "email",
        "refresh_token",
        "access_token",
        "refreshToken",
        "accessToken",
        "id_token",
        "apiKey",
        "OPENAI_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
    }
    if isinstance(value, dict):
        return {str(key): scrub(item) for key, item in value.items() if str(key) not in blocked}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        if value.startswith("sk-") or value.lower().startswith("bearer "):
            return ""
        if len(value) > 40 and value.count(".") == 2:
            return ""
        return value
    return value


def public_error(exc: BaseException) -> str:
    text = str(exc).replace("\n", " ")
    lowered = text.lower()
    for marker in ("bearer ", "sk-ant", "sk-"):
        start = lowered.find(marker)
        if start != -1:
            text = text[:start] + "[redacted]"
            break
    return text[:240]


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


def state_dir() -> Path:
    root = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(root) / "gnome-agent-usage"


def cache_dir() -> Path:
    root = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(root) / "gnome-agent-usage"


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


class Lock:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("a", encoding="utf-8")

    def __enter__(self):
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *_args):
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()


def cached_file_stats(cache_name: str, paths: list[Path], scan_one) -> list[dict]:
    """Reuse a file's aggregate until its size or mtime changes."""
    path = cache_dir() / cache_name
    with Lock(cache_dir() / ".lock"):
        cache = read_json(path)
        files = cache.get("files") if isinstance(cache, dict) else None
        if not isinstance(files, dict):
            files = {}
        fresh = {}
        changed = False
        total = len(paths)
        for index, item in enumerate(paths, start=1):
            if index == 1 or index == total or index % 25 == 0:
                print(f"{cache_name} {index}/{total}", file=sys.stderr, flush=True)
            key = str(item)
            try:
                stat = item.stat()
            except OSError:
                continue
            stamp = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
            old = files.get(key)
            if isinstance(old, dict) and old.get("v") == FILE_CACHE_VERSION and old.get("size") == stamp["size"] and old.get("mtime_ns") == stamp["mtime_ns"] and isinstance(old.get("stats"), dict):
                fresh[key] = old
                continue
            try:
                stats = scan_one(item)
            except (OSError, UnicodeError, json.JSONDecodeError):
                stats = empty_part()
            fresh[key] = {**stamp, "v": FILE_CACHE_VERSION, "stats": stats}
            changed = True
        if changed or set(files) != set(fresh):
            write_json(path, {"files": fresh})
        return [item["stats"] for item in fresh.values()]


def _token_total(usage: dict) -> int:
    if not isinstance(usage, dict):
        return 0
    if "total_tokens" in usage:
        try:
            return max(0, int(usage.get("total_tokens") or 0))
        except (TypeError, ValueError):
            return 0
    total = 0
    for key in ("input_tokens", "output_tokens", "cache_write_input_tokens", "cache_creation_input_tokens"):
        try:
            total += max(0, int(usage.get(key) or 0))
        except (TypeError, ValueError):
            continue
    return total


def scan_codex_file(path: Path) -> dict:
    """Sum one Codex rollout. Prefer per-response records over cumulative counts."""
    part = empty_part()
    model = "Codex"
    session = path.stem
    project = ""
    pinned = False
    saw_usage = False
    usage_days: dict[str, int] = {}
    usage_models: dict[str, int] = {}
    usage_day_models: dict[str, dict] = {}
    usage_projects: dict[str, int] = {}
    count_days: dict[str, int] = {}
    count_models: dict[str, int] = {}
    count_day_models: dict[str, dict] = {}
    count_projects: dict[str, int] = {}
    pending_usage: list[int] = []
    pending_count: list[int] = []

    def set_project(label: str, pin: bool = False) -> None:
        nonlocal project, pinned
        if not label or (pinned and not pin):
            return
        project = label
        pinned = pinned or pin

    def remember(store_days, store_models, store_day_models, store_projects, pending, day: str, tokens: int):
        if tokens <= 0:
            return
        _add(store_days, day, tokens)
        _add(store_models, model, tokens)
        if day:
            _add(store_day_models.setdefault(day, {}), model, tokens)
        if project:
            _add(store_projects, project, tokens)
        else:
            pending.append(tokens)

    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not any(marker in line for marker in ("token_usage_record", "token_count", "turn_context", "session_meta", "user_message", '"role":"user"', '"role": "user"')):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            kind = obj.get("type")
            day = local_day(obj.get("timestamp"))
            if kind == "session_meta":
                session = str(payload.get("session_id") or payload.get("id") or session)
                if payload.get("model"):
                    model = str(payload.get("model"))
                git = payload.get("git") if isinstance(payload.get("git"), dict) else {}
                repo = repository_label(str(git.get("repository_url") or ""))
                if repo:
                    set_project(repo, pin=True)
                else:
                    set_project(path_project(str(payload.get("cwd") or "")))
                _add_session(part, day, session)
            elif kind == "turn_context":
                if payload.get("model"):
                    model = str(payload.get("model"))
                if not pinned:
                    set_project(path_project(str(payload.get("cwd") or "")))
            elif kind == "token_usage_record":
                saw_usage = True
                tokens = _token_total(payload.get("usage") or {})
                remember(usage_days, usage_models, usage_day_models, usage_projects, pending_usage, day, tokens)
            elif kind == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                tokens = _token_total(info.get("last_token_usage") or {})
                remember(count_days, count_models, count_day_models, count_projects, pending_count, day, tokens)
            elif kind == "event_msg" and payload.get("type") == "user_message":
                part["prompts"] += 1
                _add(part["promptDays"], day, 1)
                _add_session(part, day, session)
            elif kind == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
                part["prompts"] += 1
                _add(part["promptDays"], day, 1)
                _add_session(part, day, session)
    if saw_usage:
        part["tokenDays"] = usage_days
        part["models"] = usage_models
        part["dayModels"] = usage_day_models
        part["projects"] = usage_projects
        pending = pending_usage
    else:
        part["tokenDays"] = count_days
        part["models"] = count_models
        part["dayModels"] = count_day_models
        part["projects"] = count_projects
        pending = pending_count
    if project:
        for tokens in pending:
            _add(part["projects"], project, tokens)
    for day in part["tokenDays"]:
        _add_session(part, day, session)
    if not part["sessionDays"] and (part["tokenDays"] or part["prompts"]):
        _add_session(part, local_day(path.stat().st_mtime), session)
    return part


def scan_claude_file(path: Path) -> dict:
    part = empty_part()
    seen = set()
    project = ""
    unassigned = 0
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if '"assistant"' not in line and '"cwd"' not in line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if not project and isinstance(obj.get("cwd"), str):
                project = path_project(obj.get("cwd") or "")
                if project and unassigned:
                    _add(part["projects"], project, unassigned)
                    unassigned = 0
            if obj.get("type") != "assistant":
                continue
            day = local_day(obj.get("timestamp"))
            session = str(obj.get("sessionId") or path.stem)
            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
            tokens = 0
            for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                try:
                    tokens += max(0, int(usage.get(key) or 0))
                except (TypeError, ValueError):
                    continue
            if tokens <= 0:
                continue
            identity = str(obj.get("uuid") or obj.get("messageId") or "")
            if identity:
                if identity in seen:
                    continue
                seen.add(identity)
            model = str(message.get("model") or "Claude")
            _add(part["tokenDays"], day, tokens)
            _add(part["models"], model, tokens)
            if day:
                _add(part["dayModels"].setdefault(day, {}), model, tokens)
            if project:
                _add(part["projects"], project, tokens)
            else:
                unassigned += tokens
            _add_session(part, day, session)
    return part


def scan_claude_history(path: Path) -> dict:
    """Prompt log kept beside the transcripts. Tokens stay in the transcripts."""
    part = empty_part()
    if not path.exists():
        return part
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict) or not obj.get("sessionId"):
                continue
            day = local_day(obj.get("timestamp"))
            part["prompts"] += 1
            _add(part["promptDays"], day, 1)
            _add_session(part, day, str(obj.get("sessionId")))
    return part


def scan_grok_home(home: Path) -> dict:
    part = empty_part()
    sessions = home / "sessions"
    if not sessions.exists():
        return part
    seen_turns = set()
    for summary_path in sessions.rglob("summary.json"):
        summary = read_json(summary_path)
        if not isinstance(summary, dict):
            continue
        session = str(summary.get("attempt_id") or summary_path.parent.name)
        project = path_project(summary_path.parent.parent.name)
        if project == "sessions":
            project = ""
        subagent = str(summary.get("session_kind") or "").startswith("subagent")
        active = local_day(summary.get("last_active_at"))
        _add_session(part, active, session)
        usage = read_json(summary_path.parent / "usage.json")
        turns = usage.get("turns") if isinstance(usage, dict) else None
        if not isinstance(turns, list):
            continue
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            day = local_day(turn.get("endedAt"))
            if not day:
                continue
            fingerprint = json.dumps([turn.get("endedAt"), turn.get("modelUsage")], sort_keys=True, default=str)
            if fingerprint in seen_turns:
                continue
            seen_turns.add(fingerprint)
            if not subagent:
                part["prompts"] += 1
                _add(part["promptDays"], day, 1)
            model_usage = turn.get("modelUsage") if isinstance(turn.get("modelUsage"), dict) else {}
            if not model_usage and turn.get("totalTokens"):
                model_usage = {str(turn.get("primaryModelId") or "Grok"): {"totalTokens": turn.get("totalTokens")}}
            for model, bucket in model_usage.items():
                if not isinstance(bucket, dict):
                    continue
                cached = _safe_int(bucket.get("cachedReadTokens"))
                uncached = max(0, _safe_int(bucket.get("inputTokens")) - cached)
                output = _safe_int(bucket.get("outputTokens"))
                created = _safe_int(bucket.get("cacheCreationTokens"))
                tokens = uncached + cached + output + created
                if tokens <= 0:
                    tokens = _safe_int(bucket.get("totalTokens"))
                _add(part["tokenDays"], day, tokens)
                _add(part["models"], str(model), tokens)
                _add(part["dayModels"].setdefault(day, {}), str(model), tokens)
                if project:
                    _add(part["projects"], project, tokens)
    return part


def _safe_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def scan_opencode(db_path: Path) -> dict:
    part = empty_part()
    if not db_path.exists():
        return part
    uri = f"file:{db_path}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return part
    try:
        models = {}
        session_projects: dict[str, str] = {}
        session_models: dict[str, str] = {}

        def remember_session(session_id, name, directory, model_value) -> None:
            label = clean_project(name) if isinstance(name, str) else ""
            if not label:
                label = path_project(directory or "")
            if label:
                session_projects[str(session_id)] = label
            model = model_name(model_value)
            if model:
                session_models[str(session_id)] = model

        for query in (
            'select session.id, "project".name, session.directory, session.model '
            'from session left join "project" on "project".id = session.project_id',
            'select session_v2.id, "project".name, session_v2.directory, session_v2.model '
            'from session_v2 left join "project" on "project".id = session_v2.project_id',
        ):
            try:
                for session_id, name, directory, model_value in connection.execute(query):
                    remember_session(session_id, name, directory, model_value)
            except sqlite3.Error:
                pass
        try:
            for message_id, created, data in connection.execute("select id, time_created, data from message"):
                try:
                    obj = json.loads(data)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("model"):
                    label = model_name(obj.get("model"))
                    if label:
                        models[message_id] = label
                if obj.get("role") == "user":
                    day = local_day(obj.get("time") or created)
                    part["prompts"] += 1
                    _add(part["promptDays"], day, 1)
                    _add_session(part, day, str(message_id))
        except sqlite3.Error:
            pass
        try:
            rows = connection.execute("select message_id, session_id, time_created, data from part")
        except sqlite3.Error:
            return part
        for message_id, session_id, created, data in rows:
            try:
                obj = json.loads(data)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(obj, dict) or obj.get("type") != "step-finish":
                continue
            tokens_field = obj.get("tokens") if isinstance(obj.get("tokens"), dict) else {}
            tokens = _safe_int(tokens_field.get("total"))
            if tokens <= 0:
                continue
            day = local_day(created)
            model = models.get(message_id) or session_models.get(str(session_id or "")) or "未命名模型"
            _add(part["tokenDays"], day, tokens)
            _add(part["models"], model, tokens)
            if day:
                _add(part["dayModels"].setdefault(day, {}), model, tokens)
            label = session_projects.get(str(session_id or ""))
            if label:
                _add(part["projects"], label, tokens)
            _add_session(part, day, str(session_id or message_id))
    finally:
        connection.close()
    return part


def credits_limit(data: dict, now: datetime | None = None):
    """One Grok credits window. None means the monthly quota is the real meter."""
    now = now or datetime.now().astimezone()
    config = data["config"]
    period = config["currentPeriod"]
    ends = parse_time(period.get("end"))
    if ends is None:
        raise ValueError("missing period end")
    used = config.get("creditUsagePercent")
    if used is None:
        if config.get("isUnifiedBillingUser") is True:
            return None
        if ends.timestamp() <= now.timestamp():
            raise ValueError("ended period")
        used = 0
    weekly = "WEEK" in str(period.get("type") or "").upper()
    return {
        "id": "weekly" if weekly else "monthly",
        "label": "每周" if weekly else "每月",
        "percent": clamp_ratio(amount(used) / 100),
        "resetsAt": ends.isoformat(),
        "windowSeconds": 7 * 86400 if weekly else 30 * 86400,
    }


def monthly_limit(data: dict):
    config = data["config"]
    quota = amount(config.get("monthlyLimit"))
    if quota <= 0:
        raise ValueError("no monthly quota")
    ends = parse_time(config.get("billingPeriodEnd"))
    if ends is None:
        raise ValueError("missing billing period")
    return {
        "id": "monthly",
        "label": "每月",
        "percent": clamp_ratio(amount(config.get("used")) / quota),
        "resetsAt": ends.isoformat(),
        "windowSeconds": 30 * 86400,
    }


class RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """The usage host is pinned. A redirect is not a place a token may follow."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def fetch_json(url: str, token: str, allowed_host: str, timeout: float = 8):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != allowed_host:
        raise urllib.error.URLError("refusing host")
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "gnome-agent-usage",
        },
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(urllib.request.getproxies()),
        RefuseRedirect,
        urllib.request.HTTPHandler(),
        urllib.request.HTTPSHandler(),
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError:
        raise
    if not isinstance(payload, dict):
        raise ValueError("usage endpoint did not return an object")
    return payload


def grok_plan(home: Path) -> str:
    cached = read_json(home / "settings_cache.json")
    try:
        payload = json.loads(cached.get("payload") or "{}")
    except (TypeError, json.JSONDecodeError):
        return ""
    settings = payload.get("settings") if isinstance(payload, dict) else None
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except json.JSONDecodeError:
            return ""
    if not isinstance(settings, dict):
        return ""
    return str(settings.get("subscription_tier_display") or "")


def grok_login(home: Path) -> dict:
    auth = read_json(home / "auth.json")
    if not isinstance(auth, dict):
        return {}
    for value in auth.values():
        if isinstance(value, dict) and value.get("key"):
            return value
    return {}


def load_limit_cache(name: str) -> dict:
    data = read_json(cache_dir() / name)
    return data if isinstance(data, dict) else {}


def save_limit_cache(name: str, limits: list, plan: str, fetched_at: str) -> None:
    write_json(cache_dir() / name, {"fetchedAt": fetched_at, "plan": plan, "limits": limits})


def cache_is_fresh(cached: dict, force: bool, now: datetime) -> bool:
    if force or not isinstance(cached.get("limits"), list) or not cached.get("limits"):
        return False
    fetched = parse_time(cached.get("fetchedAt"))
    if fetched is None:
        return False
    return now.timestamp() - fetched.timestamp() < REUSE_SECONDS


def collect_grok_limits(home: Path, force: bool, now: datetime, fetch=fetch_json) -> dict:
    result = {"limits": [], "plan": grok_plan(home), "status": "", "help": "", "stale": False, "fetchedAt": ""}
    cached = load_limit_cache("grok-limits.json")
    entry = grok_login(home)
    token = str(entry.get("key") or "")
    if not token:
        result["status"] = "等待登录"
        result["help"] = "运行 grok login，或先打开一次 Grok。"
        return result
    if cache_is_fresh(cached, force, now):
        result["limits"] = [finish_limit(item, now) for item in cached.get("limits") or []]
        result["plan"] = result["plan"] or str(cached.get("plan") or "")
        result["fetchedAt"] = str(cached.get("fetchedAt") or "")
        return result

    expires = parse_time(entry.get("expires_at"))
    fallback = [finish_limit(item, now) for item in cached.get("limits") or [] if isinstance(item, dict)]
    if expires and expires.timestamp() <= now.timestamp():
        result["limits"] = fallback
        result["stale"] = bool(fallback)
        result["fetchedAt"] = str(cached.get("fetchedAt") or "")
        if entry.get("refresh_token") and now.timestamp() - expires.timestamp() < REFRESH_LIFETIME_SECONDS:
            result["status"] = "额度已暂停"
            result["help"] = "打开一次 Grok 就会续上登录，不必重新登录。"
        else:
            result["status"] = "登录已过期"
            result["help"] = "运行 grok login 重新登录。"
        return result

    limits = []
    try:
        credits = fetch(GROK_CREDITS_URL, token, "cli-chat-proxy.grok.com")
        weekly = credits_limit(credits, now)
        unified = isinstance(credits.get("config"), dict) and credits["config"].get("isUnifiedBillingUser") is True
        if weekly is None:
            limits = [monthly_limit(fetch(GROK_MONTHLY_URL, token, "cli-chat-proxy.grok.com"))]
        else:
            limits = [weekly]
            if unified and weekly["id"] == "weekly":
                try:
                    limits.append(monthly_limit(fetch(GROK_MONTHLY_URL, token, "cli-chat-proxy.grok.com")))
                except (urllib.error.URLError, TimeoutError, ValueError, KeyError, json.JSONDecodeError):
                    pass
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            result["status"] = "登录已过期"
            result["help"] = "xAI 拒绝了保存的登录。运行 grok login 重新登录。"
        else:
            result["status"] = "额度暂不可用"
            result["help"] = f"Grok 用量接口返回了 {error.code}。本机记录仍会显示。"
    except (urllib.error.URLError, TimeoutError, OSError):
        result["status"] = "连不上用量接口"
        result["help"] = "连不上 Grok 的用量接口。下面仍是本机记录。"
    except (ValueError, KeyError, json.JSONDecodeError):
        result["status"] = "额度暂不可用"
        result["help"] = "Grok 的用量接口没有返回可用额度。本机记录仍会显示。"

    if limits:
        fetched_at = now.astimezone().isoformat()
        result["limits"] = [finish_limit(item, now) for item in limits]
        result["fetchedAt"] = fetched_at
        result["status"] = ""
        result["help"] = ""
        save_limit_cache("grok-limits.json", result["limits"], result["plan"], fetched_at)
    else:
        result["limits"] = fallback
        result["stale"] = bool(fallback)
        result["fetchedAt"] = str(cached.get("fetchedAt") or "")
        if not result["status"] and not result["limits"]:
            result["status"] = "额度暂不可用"
    return result


def claude_oauth(home: Path) -> dict:
    data = read_json(home / ".credentials.json")
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    return oauth if isinstance(oauth, dict) else {}


def claude_route(home: Path) -> str:
    """official, gateway, or none. The gateway token is never sent anywhere."""
    settings = read_json(home / "settings.json")
    env = settings.get("env") if isinstance(settings, dict) and isinstance(settings.get("env"), dict) else {}
    base = str(env.get("ANTHROPIC_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL") or "")
    host = (urlparse(base).hostname or "").lower()
    if host and host != "api.anthropic.com":
        return "gateway"
    if claude_oauth(home).get("accessToken"):
        return "official"
    if env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"):
        return "gateway" if host else "apikey"
    return "none"


def claude_limits_from_payload(payload: dict) -> list[dict]:
    limits = []

    def add(bucket, limit_id, label, window):
        if not isinstance(bucket, dict):
            return
        try:
            percent = amount(bucket.get("utilization")) / 100
        except (TypeError, ValueError):
            return
        if percent < 0:
            return
        limits.append({
            "id": limit_id,
            "label": label,
            "percent": clamp_ratio(percent),
            "resetsAt": str(bucket.get("resets_at") or ""),
            "windowSeconds": window,
        })

    add(payload.get("five_hour"), "session", "会话（5 小时）", 5 * 3600)
    add(payload.get("seven_day"), "weekly", "每周（7 天）", 7 * 86400)
    entries = payload.get("limits")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            scope = entry.get("scope") if isinstance(entry.get("scope"), dict) else {}
            model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
            name = str(model.get("display_name") or model.get("id") or "").strip()
            if not name:
                continue
            try:
                percent = amount(entry.get("percent")) / 100
            except (TypeError, ValueError):
                continue
            kind = str(entry.get("kind") or "").lower()
            if "week" in kind or "day" in kind:
                label, window, suffix = f"{name} 每周", 7 * 86400, "weekly"
            elif "hour" in kind or "session" in kind:
                label, window, suffix = f"{name} 会话", 5 * 3600, "session"
            else:
                label, window, suffix = name, 0, "other"
            limits.append({
                "id": f"{name}-{suffix}",
                "label": label,
                "percent": clamp_ratio(percent),
                "resetsAt": str(entry.get("resets_at") or ""),
                "windowSeconds": window,
            })
    return limits


def collect_claude_limits(home: Path, force: bool, now: datetime, fetch=fetch_json) -> dict:
    route = claude_route(home)
    result = {"limits": [], "plan": "", "status": "", "help": "", "stale": False, "fetchedAt": ""}
    if route == "gateway":
        result["plan"] = "本地网关"
        result["status"] = "没有订阅窗口"
        result["help"] = "Claude 走的是本地网关，这里只统计本机对话记录。"
        return result
    if route != "official":
        if route == "apikey":
            result["plan"] = "API key"
            result["status"] = "没有订阅窗口"
            result["help"] = "API key 登录没有 Claude 订阅额度。下面是本机记录。"
        elif not (home / "projects").exists() and not (home / "history.jsonl").exists():
            result["status"] = "等待登录"
            result["help"] = "运行 claude auth login 后，订阅额度会出现在这里。"
        return result

    oauth = claude_oauth(home)
    cached = load_limit_cache("claude-limits.json")
    if cache_is_fresh(cached, force, now):
        result["limits"] = [finish_limit(item, now) for item in cached.get("limits") or []]
        result["plan"] = str(cached.get("plan") or "")
        result["fetchedAt"] = str(cached.get("fetchedAt") or "")
        return result
    expires = parse_time(oauth.get("expiresAt"))
    fallback = [finish_limit(item, now) for item in cached.get("limits") or [] if isinstance(item, dict)]
    if expires and expires.timestamp() <= now.timestamp():
        result["limits"] = fallback
        result["stale"] = bool(fallback)
        result["fetchedAt"] = str(cached.get("fetchedAt") or "")
        result["status"] = "额度已暂停"
        result["help"] = "打开一次 Claude Code，它会自己续上登录。"
        return result
    plan = str(oauth.get("subscriptionType") or "")
    try:
        payload = fetch(CLAUDE_USAGE_URL, str(oauth.get("accessToken") or ""), "api.anthropic.com")
        limits = claude_limits_from_payload(payload)
    except urllib.error.HTTPError as error:
        limits = []
        if error.code in (401, 403):
            result["status"] = "登录已过期"
            result["help"] = "运行 claude auth login 重新登录。"
        else:
            result["status"] = "额度暂不可用"
            result["help"] = f"Anthropic 用量接口返回了 {error.code}。本机记录仍会显示。"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        limits = []
        result["status"] = "连不上用量接口"
        result["help"] = "连不上 Anthropic 的用量接口。下面仍是本机记录。"
    if limits:
        fetched_at = now.astimezone().isoformat()
        result["limits"] = [finish_limit(item, now) for item in limits]
        result["plan"] = plan
        result["fetchedAt"] = fetched_at
        result["status"] = ""
        result["help"] = ""
        save_limit_cache("claude-limits.json", result["limits"], plan, fetched_at)
    else:
        result["limits"] = fallback
        result["stale"] = bool(fallback)
        result["fetchedAt"] = str(cached.get("fetchedAt") or "")
        result["plan"] = plan or str(cached.get("plan") or "")
    return result


def codex_auth_kind(home: Path) -> str:
    auth = read_json(home / "auth.json")
    if not isinstance(auth, dict) or not auth:
        return "none"
    mode = str(auth.get("auth_mode") or "")
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    if tokens.get("access_token") and mode != "apikey":
        return "chatgpt"
    if mode == "apikey" or auth.get("OPENAI_API_KEY"):
        return "apikey"
    return "none"


def codex_plan_name(plan: str) -> str:
    plan = str(plan or "")
    return CODEX_PLAN_NAMES.get(plan, plan.replace("_", " "))


def codex_limits_from_result(result: dict) -> tuple[list[dict], str]:
    if not isinstance(result, dict):
        return [], ""
    buckets = result.get("rateLimitsByLimitId")
    entries = []
    if isinstance(buckets, dict) and buckets:
        entries = [(str(key), value) for key, value in buckets.items() if isinstance(value, dict)]
        entries.sort(key=lambda item: item[0] != "codex")
    else:
        legacy = result.get("rateLimits")
        if isinstance(legacy, dict):
            entries = [("codex", legacy)]
    limits = []
    plan = ""
    for limit_id, bucket in entries:
        if not plan and bucket.get("planType"):
            plan = str(bucket.get("planType"))
        general = limit_id == "codex" or (len(entries) == 1 and not bucket.get("limitName"))
        name = str(bucket.get("limitName") or limit_id).strip()
        for window in (bucket.get("primary"), bucket.get("secondary")):
            if not isinstance(window, dict):
                continue
            try:
                used = float(window.get("usedPercent"))
            except (TypeError, ValueError):
                continue
            if isinstance(window.get("usedPercent"), bool) or not math.isfinite(used):
                continue
            try:
                minutes = int(window.get("windowDurationMins") or 0)
            except (TypeError, ValueError):
                minutes = 0
            if minutes >= 28 * 24 * 60:
                label, suffix = "每月", "monthly"
            elif minutes >= 24 * 60:
                label, suffix = "每周", "weekly"
            elif minutes > 0:
                label, suffix = "会话", "session"
            else:
                label, suffix = "额度", "limit"
            if not general:
                label = f"{name} {label}"
            resets = parse_time(window.get("resetsAt"))
            limits.append({
                "id": f"{limit_id}-{suffix}",
                "label": label,
                "percent": clamp_ratio(used / 100),
                "resetsAt": resets.isoformat() if resets else "",
                "windowSeconds": minutes * 60,
            })
    if not plan and isinstance(result.get("rateLimits"), dict):
        plan = str(result["rateLimits"].get("planType") or "")
    return limits, codex_plan_name(plan)


def codex_rpc(home: Path) -> dict:
    binary = shutil.which("codex")
    if not binary:
        raise RuntimeError("codex 不在 PATH 里")
    env = dict(os.environ)
    env["CODEX_HOME"] = str(home)
    stderr = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [binary, "-s", "read-only", "-a", "on-request", "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr,
        text=True,
        env=env,
    )
    try:
        def send(payload):
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()

        def request(number, method, params=None, timeout=8):
            send({"jsonrpc": "2.0", "id": number, "method": method, "params": params or {}})
            deadline = time.monotonic() + timeout
            assert proc.stdout is not None
            while time.monotonic() < deadline:
                line = proc.stdout.readline()
                if not line:
                    raise RuntimeError("codex app-server 已退出")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict) and message.get("id") == number:
                    return message
            raise TimeoutError(method)
        request(1, "initialize", {"clientInfo": {"name": "gnome-agent-usage", "version": "1"}})
        send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        answer = request(2, "account/rateLimits/read")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=1)
        except Exception:
            proc.kill()
        stderr.close()
    if not isinstance(answer, dict):
        raise RuntimeError("codex app-server 没有返回额度")
    if isinstance(answer.get("error"), dict):
        raise RuntimeError(str(answer["error"].get("message") or "codex 拒绝了额度请求"))
    result = answer.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("codex app-server 没有返回额度")
    return result


def collect_codex_limits(home: Path, force: bool, now: datetime, rpc=codex_rpc) -> dict:
    kind = codex_auth_kind(home)
    result = {"limits": [], "plan": "", "status": "", "help": "", "stale": False, "fetchedAt": ""}
    if kind == "apikey":
        result["plan"] = "API key"
        result["status"] = "没有订阅窗口"
        result["help"] = "API key 登录没有 ChatGPT 订阅额度。下面是本机会话。"
        return result
    if kind != "chatgpt":
        result["status"] = "等待登录"
        result["help"] = "运行 codex login 后，订阅额度会出现在这里。"
        return result
    cached = load_limit_cache("codex-limits.json")
    if cache_is_fresh(cached, force, now):
        result["limits"] = [finish_limit(item, now) for item in cached.get("limits") or []]
        result["plan"] = str(cached.get("plan") or "")
        result["fetchedAt"] = str(cached.get("fetchedAt") or "")
        return result
    fallback = [finish_limit(item, now) for item in cached.get("limits") or [] if isinstance(item, dict)]
    try:
        payload = rpc(home)
        limits, plan = codex_limits_from_result(payload)
    except (OSError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
        limits, plan = [], ""
        result["status"] = "额度暂不可用"
        result["help"] = public_error(exc) or "Codex 没有返回额度。本机会话仍会显示。"
    if limits:
        fetched_at = now.astimezone().isoformat()
        result["limits"] = [finish_limit(item, now) for item in limits]
        result["plan"] = plan
        result["fetchedAt"] = fetched_at
        result["status"] = ""
        result["help"] = ""
        save_limit_cache("codex-limits.json", result["limits"], plan, fetched_at)
    else:
        result["limits"] = fallback
        result["stale"] = bool(fallback)
        result["fetchedAt"] = str(cached.get("fetchedAt") or "")
        result["plan"] = str(cached.get("plan") or "")
        if not result["status"]:
            result["status"] = "额度暂不可用"
            result["help"] = "Codex 没有返回额度。本机会话仍会显示。"
    return result


def agent_record(agent_id: str, name: str, mark: str, limits: dict, stats: dict) -> dict:
    return {
        "id": agent_id,
        "name": name,
        "mark": mark,
        "plan": limits.get("plan") or "",
        "status": limits.get("status") or "",
        "help": limits.get("help") or "",
        "stale": bool(limits.get("stale")),
        "fetchedAt": limits.get("fetchedAt") or "",
        "limits": limits.get("limits") or [],
        **stats,
    }


def collect_snapshot(force: bool = False, now: datetime | None = None) -> dict:
    now = now or datetime.now().astimezone()
    today = now.date()
    home = Path.home()
    agents = []

    claude_home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (home / ".claude"))
    if claude_home.exists():
        transcripts = list((claude_home / "projects").rglob("*.jsonl")) if (claude_home / "projects").exists() else []
        parts = cached_file_stats("claude-files.json", transcripts, scan_claude_file)
        parts.append(scan_claude_history(claude_home / "history.jsonl"))
        agents.append(agent_record("claude", "Claude", "C", collect_claude_limits(claude_home, force, now), merge_parts(parts, today)))

    codex_home = Path(os.environ.get("CODEX_HOME") or (home / ".codex"))
    if codex_home.exists():
        rollouts = list((codex_home / "sessions").rglob("*.jsonl")) if (codex_home / "sessions").exists() else []
        parts = cached_file_stats("codex-files.json", rollouts, scan_codex_file)
        agents.append(agent_record("codex", "Codex", "X", collect_codex_limits(codex_home, force, now), merge_parts(parts, today)))

    grok_home = Path(os.environ.get("GROK_HOME") or (home / ".grok"))
    if grok_home.exists():
        agents.append(agent_record("grok", "Grok", "G", collect_grok_limits(grok_home, force, now), merge_parts([scan_grok_home(grok_home)], today)))

    opencode_db = Path(os.environ.get("OPENCODE_DB") or (home / ".local" / "share" / "opencode" / "opencode.db"))
    if opencode_db.exists() or shutil.which("opencode"):
        stats = merge_parts([scan_opencode(opencode_db)], today)
        limits = {
            "limits": [],
            "plan": "本地",
            "status": "" if stats["hasLocalStats"] else "没有记录",
            "help": "" if stats["hasLocalStats"] else "OpenCode 这里只统计本机数据库里的用量。",
            "stale": False,
            "fetchedAt": "",
        }
        agents.append(agent_record("opencode", "OpenCode", "O", limits, stats))

    worst = "ok"
    for agent in agents:
        for limit in agent["limits"]:
            if limit.get("tone") == "hot":
                worst = "hot"
            elif limit.get("tone") == "warn" and worst != "hot":
                worst = "warn"
    return scrub({
        "schemaVersion": SCHEMA_VERSION,
        "updatedAt": now.isoformat(),
        "tone": worst,
        "agents": agents,
    })


def summary_lines(snapshot: dict) -> list[str]:
    lines = []
    for agent in snapshot.get("agents") or []:
        bits = [agent.get("name") or agent.get("id") or "agent"]
        if agent.get("plan"):
            bits.append(str(agent["plan"]))
        limits = agent.get("limits") or []
        if limits:
            for limit in limits:
                percent = round(float(limit.get("percent") or 0) * 100)
                bits.append(f"{limit.get('label')} {percent}%")
        elif agent.get("status"):
            bits.append(str(agent["status"]))
        bits.append(f"今天 {format_tokens(agent.get('todayTokens'))}")
        lines.append("  ".join(bits))
    return lines or ["没有读到 agent 用量"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print local AI agent usage as JSON.")
    parser.add_argument("--write", action="store_true", help="also write the snapshot under XDG state")
    parser.add_argument("--force", action="store_true", help="ignore the recent limit cache")
    parser.add_argument("--summary", action="store_true", help="print one line per agent instead of JSON")
    args = parser.parse_args(argv)
    try:
        snapshot = collect_snapshot(force=args.force)
    except Exception as exc:
        snapshot = scrub({
            "schemaVersion": SCHEMA_VERSION,
            "updatedAt": datetime.now().astimezone().isoformat(),
            "tone": "ok",
            "agents": [],
            "error": public_error(exc) or "collector failed",
        })
    if args.write:
        write_json(state_dir() / "snapshot.json", snapshot)
    if args.summary:
        print("\n".join(summary_lines(snapshot)))
    else:
        print(json.dumps(snapshot, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
