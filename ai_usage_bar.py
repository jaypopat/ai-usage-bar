#!/usr/bin/env python3
"""A small, local-first Claude Code, Codex, and Antigravity tray meter for KDE Plasma."""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PyQt6.QtCore import (
    QEvent,
    QLockFile,
    QObject,
    QPoint,
    QRect,
    QRunnable,
    QSize,
    QStandardPaths,
    Qt,
    QThreadPool,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QAction, QColor, QCursor, QIcon, QPainter, QPalette, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSystemTrayIcon,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "AI Usage Bar"
APP_ID = "io.github.aiusagebar"
VERSION = "0.3.0"
REFRESH_MS = 120_000
ASSET_DIR = Path(__file__).resolve().parent / "assets"

ANTIGRAVITY_ENDPOINTS = (
    "https://daily-cloudcode-pa.googleapis.com",
    "https://cloudcode-pa.googleapis.com",
)
ANTIGRAVITY_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
ANTIGRAVITY_CLIENT_METADATA = {
    "ideType": "ANTIGRAVITY",
    "platform": "PLATFORM_UNSPECIFIED",
    "pluginType": "GEMINI",
}


def _antigravity_user_agent() -> str:
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("aarch64", "arm64") else "amd64"
    return f"antigravity/hub/2.9.1 (aidev_client; os_type=linux; arch={arch})"


@dataclass(frozen=True)
class ProviderSpec:
    key: str
    label: str
    icon_asset: str


PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec("claude", "Claude", "claude.svg"),
    ProviderSpec("codex", "Codex", "openai.svg"),
    ProviderSpec("antigravity", "Antigravity", "antigravity.svg"),
)
PROVIDER_MAP: dict[str, ProviderSpec] = {spec.key: spec for spec in PROVIDERS}


@dataclass
class UsageWindow:
    label: str
    used: float
    resets_at: datetime | None
    duration_seconds: int | None = None
    starts_on_use: bool = False


@dataclass
class ModelBreakdown:
    name: str
    tokens: int
    cost: float


@dataclass
class DailyUsage:
    date: str
    cost: float
    tokens: int
    models: list[ModelBreakdown] = field(default_factory=list)


@dataclass
class LocalUsage:
    cost: float
    tokens: int
    models: list[ModelBreakdown] = field(default_factory=list)
    history_days: int = 30
    today_cost: float = 0.0
    today_tokens: int = 0
    daily: list[DailyUsage] = field(default_factory=list)


@dataclass
class ProviderUsage:
    name: str
    plan: str | None = None
    windows: list[UsageWindow] = field(default_factory=list)
    error: str | None = None
    local_usage: LocalUsage | None = None
    reset_credits: int | None = None


def parse_date(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError, OSError):
        return None


def lane_label(seconds: int | None, fallback: str) -> str:
    if not seconds:
        return fallback
    hours = seconds / 3600
    if hours < 24:
        return f"{hours:g} hour"
    days = hours / 24
    if 0.9 <= days <= 1.1:
        return "Daily"
    return "Weekly" if 6.5 <= days <= 7.5 else f"{days:g} day"


def parse_claude(data: dict[str, Any]) -> ProviderUsage:
    usage = ProviderUsage("Claude")
    for key, label, seconds in (
        ("five_hour", "5 hour", 5 * 3600),
        ("seven_day", "Weekly", 7 * 86400),
    ):
        value = data.get(key)
        if isinstance(value, dict) and value.get("utilization") is not None:
            usage.windows.append(
                UsageWindow(label, float(value["utilization"]), parse_date(value.get("resets_at")), seconds)
            )
    return usage


def parse_codex(data: dict[str, Any]) -> ProviderUsage:
    usage = ProviderUsage("Codex", str(data.get("plan_type") or "").replace("_", " ").title() or None)
    reset_credits = data.get("rate_limit_reset_credits")
    if isinstance(reset_credits, dict) and reset_credits.get("available_count") is not None:
        usage.reset_credits = int(reset_credits["available_count"])
    seen: set[tuple[str, int | None, float]] = set()

    def append_limits(limits: Any, prefix: str | None = None) -> None:
        if not isinstance(limits, dict):
            return
        for key, fallback in (("primary_window", "Primary"), ("secondary_window", "Secondary")):
            value = limits.get(key)
            if not isinstance(value, dict) or value.get("used_percent") is None:
                continue
            seconds = value.get("limit_window_seconds")
            used = float(value["used_percent"])
            label = lane_label(seconds, fallback)
            if prefix:
                label = f"{prefix} · {label}"
            identity = (label, seconds, used)
            if identity in seen:
                continue
            seen.add(identity)
            usage.windows.append(UsageWindow(label, used, parse_date(value.get("reset_at")), seconds))

    append_limits(data.get("rate_limit"))
    additional = data.get("additional_rate_limits")
    if isinstance(additional, list):
        for entry in additional:
            if not isinstance(entry, dict):
                continue
            name = entry.get("limit_name") or entry.get("metered_feature")
            append_limits(entry.get("rate_limit"), str(name) if name else "Model")

    duration_order = {5 * 3600: 0, 86400: 1, 7 * 86400: 2}
    usage.windows.sort(key=lambda window: (duration_order.get(window.duration_seconds or 0, 3), window.label))
    return usage


def _parse_remaining_fraction(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        fraction = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(fraction):
        return None
    return max(0.0, min(1.0, fraction))


def parse_antigravity_summary(data: dict[str, Any]) -> list[tuple[str, UsageWindow]]:
    if not isinstance(data, dict):
        return []
    bucket_sources: list[tuple[dict[str, Any], str]] = []
    groups = data.get("groups")
    if isinstance(groups, list) and groups:
        for group in groups:
            if not isinstance(group, dict):
                continue
            group_name = str(group.get("displayName") or "").strip()
            buckets = group.get("buckets")
            if isinstance(buckets, list):
                for bucket in buckets:
                    if isinstance(bucket, dict):
                        bucket_sources.append((bucket, group_name))
    else:
        buckets = data.get("buckets")
        if isinstance(buckets, list):
            for bucket in buckets:
                if isinstance(bucket, dict):
                    bucket_sources.append((bucket, ""))

    entries: list[tuple[str, UsageWindow]] = []
    for bucket, group_name in bucket_sources:
        if bucket.get("disabled"):
            continue
        fraction = _parse_remaining_fraction(bucket.get("remainingFraction"))
        if fraction is None:
            continue
        used = (1.0 - fraction) * 100.0
        bucket_id = str(bucket.get("bucketId") or "").strip()
        if bucket_id.startswith("gemini-"):
            family = "Gemini"
        elif bucket_id.startswith("3p-"):
            family = "Claude & GPT"
        else:
            family = (group_name or bucket_id).strip() or "Quota"

        window_kind = str(bucket.get("window") or "").strip().lower()
        if window_kind == "5h":
            window_label, duration_seconds = "5 hour", 5 * 3600
        elif window_kind == "weekly":
            window_label, duration_seconds = "Weekly", 7 * 86400
        else:
            window_label = str(bucket.get("displayName") or "").strip() or "Quota"
            duration_seconds = None

        entry_id = bucket_id or f"{family.lower()}-{window_label.lower()}"
        label = f"{family} · {window_label}"
        resets_at = parse_date(bucket.get("resetTime"))
        entries.append(
            (
                entry_id,
                UsageWindow(
                    label,
                    used,
                    resets_at,
                    duration_seconds,
                    starts_on_use=(resets_at is None and used <= 0.0),
                ),
            )
        )
    return entries


def _should_replace_window(existing: UsageWindow | None, candidate: UsageWindow) -> bool:
    if existing is None or candidate.used > existing.used:
        return True
    return (
        candidate.used == existing.used
        and candidate.used > 0.0
        and existing.resets_at is None
        and candidate.resets_at is not None
    )


def parse_antigravity_models(data: dict[str, Any]) -> list[tuple[str, UsageWindow]]:
    if not isinstance(data, dict):
        return []
    models = data.get("models")
    if not isinstance(models, dict):
        return []

    merged: dict[str, UsageWindow] = {}
    for model_id, info in models.items():
        if not isinstance(info, dict):
            continue
        provider = str(info.get("modelProvider") or "").upper()
        lower_id = str(model_id).lower()
        if provider == "MODEL_PROVIDER_GOOGLE" or lower_id.startswith("gemini-"):
            family = "Gemini"
        elif provider == "MODEL_PROVIDER_ANTHROPIC" or lower_id.startswith("claude-"):
            family = "Claude"
        elif provider == "MODEL_PROVIDER_OPENAI" or lower_id.startswith("gpt-"):
            family = "GPT"
        else:
            continue

        for field_name, suffix, window_label, duration_seconds in (
            ("quotaInfo", "quota", "Quota", None),
            ("weeklyQuotaInfo", "weekly", "Weekly", 7 * 86400),
        ):
            quota = info.get(field_name)
            if not isinstance(quota, dict):
                continue
            fraction = _parse_remaining_fraction(quota.get("remainingFraction"))
            if fraction is None:
                continue
            used = (1.0 - fraction) * 100.0
            bucket_id = f"{family.lower()}-{suffix}"
            candidate = UsageWindow(
                f"{family} · {window_label}",
                used,
                parse_date(quota.get("resetTime")),
                duration_seconds,
            )
            if _should_replace_window(merged.get(bucket_id), candidate):
                merged[bucket_id] = candidate
    return list(merged.items())


def merge_antigravity_windows(entries: list[tuple[str, UsageWindow]]) -> list[UsageWindow]:
    merged: dict[str, UsageWindow] = {}
    for bucket_id, window in entries:
        if _should_replace_window(merged.get(bucket_id), window):
            merged[bucket_id] = window

    duration_order = {5 * 3600: 0, 86400: 1, 7 * 86400: 2}

    def sort_key(item: tuple[str, UsageWindow]) -> tuple[int, int, str]:
        bucket_id, window = item
        if bucket_id.startswith("gemini-"):
            family_rank = 0
        elif bucket_id.startswith("3p-"):
            family_rank = 1
        elif bucket_id.startswith("claude-"):
            family_rank = 2
        elif bucket_id.startswith("gpt-"):
            family_rank = 3
        else:
            family_rank = 4
        return (family_rank, duration_order.get(window.duration_seconds or 0, 3), window.label)

    return [window for _bucket_id, window in sorted(merged.items(), key=sort_key)]


def parse_antigravity_plan(data: dict[str, Any], has_active_five_hour: bool = False) -> str | None:
    if not isinstance(data, dict):
        return None
    paid_tier = data.get("paidTier")
    if isinstance(paid_tier, dict):
        paid_name = str(paid_tier.get("name") or "").strip()
        if paid_name:
            return paid_name
    current_tier = data.get("currentTier")
    if isinstance(current_tier, dict):
        tier_id = str(current_tier.get("id") or "").strip()
        tier_name = str(current_tier.get("name") or "").strip()
        if tier_id == "free-tier" and has_active_five_hour:
            return None
        if tier_name and tier_name != "Antigravity":
            return tier_name
    return None


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        origin = urllib.parse.urlsplit(req.full_url)
        target = urllib.parse.urlsplit(urllib.parse.urljoin(req.full_url, newurl))
        if (origin.scheme, origin.netloc) != (target.scheme, target.netloc):
            raise urllib.error.URLError("Cross-origin redirect blocked")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_HTTP_OPENER = urllib.request.build_opener(_SameOriginRedirectHandler())


class UsageClient:
    """Reads existing CLI credentials and fetches only provider quota endpoints."""

    def __init__(self, home: Path | None = None, timeout: int = 15):
        self.home = home or Path.home()
        self.timeout = timeout
        self._cost_cache: dict[str, LocalUsage] = {}
        self._cost_cached_at = 0.0
        self._antigravity_token_cache: tuple[str, str, float] | None = None

    def _open_json(self, request: urllib.request.Request) -> Any:
        try:
            with _HTTP_OPENER.open(request, timeout=self.timeout) as response:
                return json.load(response)
        except http.client.HTTPException as error:
            raise urllib.error.URLError(error) from error

    def _json_request(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"Accept": "application/json", **headers})
        return self._open_json(request)

    def _json_post(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Accept": "application/json", "Content-Type": "application/json", **headers},
            method="POST",
        )
        return self._open_json(request)

    def _form_post(self, url: str, form: dict[str, str]) -> dict[str, Any]:
        payload = urllib.parse.urlencode(form).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        return self._open_json(request)

    def claude(self) -> ProviderUsage:
        try:
            credentials = json.loads((self.home / ".claude/.credentials.json").read_text())
            token = credentials["claudeAiOauth"]["accessToken"]
            data = self._json_request(
                "https://api.anthropic.com/api/oauth/usage",
                {
                    "Authorization": f"Bearer {token}",
                    "anthropic-beta": "oauth-2025-04-20",
                    "User-Agent": "claude-code/2.1.0",
                },
            )
            usage = parse_claude(data)
            plan = credentials["claudeAiOauth"].get("subscriptionType")
            tier = credentials["claudeAiOauth"].get("rateLimitTier")
            if plan:
                usage.plan = str(plan).replace("_", " ").title()
                if tier and "5x" in str(tier).lower():
                    usage.plan += " 5×"
            return usage
        except FileNotFoundError:
            return ProviderUsage("Claude", error="Not signed in — run claude")
        except (KeyError, json.JSONDecodeError):
            return ProviderUsage("Claude", error="Claude credentials are unavailable")
        except urllib.error.HTTPError as error:
            message = "Sign in again with claude" if error.code in (401, 403) else f"Service returned HTTP {error.code}"
            return ProviderUsage("Claude", error=message)
        except (urllib.error.URLError, TimeoutError, OSError):
            return ProviderUsage("Claude", error="Could not reach Anthropic")

    def codex(self) -> ProviderUsage:
        try:
            auth = json.loads((self.home / ".codex/auth.json").read_text())
            tokens = auth["tokens"]
            headers = {
                "Authorization": f"Bearer {tokens['access_token']}",
                "User-Agent": f"AIUsageBar/{VERSION}",
            }
            account_id = tokens.get("account_id")
            if account_id:
                headers["ChatGPT-Account-Id"] = account_id
            data = self._json_request("https://chatgpt.com/backend-api/wham/usage", headers)
            return parse_codex(data)
        except FileNotFoundError:
            return ProviderUsage("Codex", error="Not signed in — run codex login")
        except (KeyError, json.JSONDecodeError):
            return ProviderUsage("Codex", error="Codex credentials are unavailable")
        except urllib.error.HTTPError as error:
            message = "Sign in again with codex login" if error.code in (401, 403) else f"Service returned HTTP {error.code}"
            return ProviderUsage("Codex", error=message)
        except (urllib.error.URLError, TimeoutError, OSError):
            return ProviderUsage("Codex", error="Could not reach OpenAI")

    def _refresh_antigravity_acp_token(self, path: Path, payload: dict[str, Any]) -> tuple[str, str | None]:
        client_id = str(payload.get("client_id") or "").strip()
        client_secret = str(payload.get("client_secret") or "").strip()
        refresh_token = str(payload.get("refresh_token") or "").strip()
        if not (client_id and client_secret and refresh_token):
            raise KeyError("missing refresh credentials")
        project_id = str(payload.get("project_id") or "").strip() or None
        stat = path.stat()
        digest = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()[:16]
        fingerprint = f"{path}:{stat.st_mtime_ns}:{stat.st_size}:{digest}"
        now_mono = time.monotonic()
        if (
            self._antigravity_token_cache is not None
            and self._antigravity_token_cache[0] == fingerprint
            and now_mono < self._antigravity_token_cache[2] - 60.0
        ):
            return self._antigravity_token_cache[1], project_id
        refreshed = self._form_post(
            ANTIGRAVITY_OAUTH_TOKEN_URL,
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if not isinstance(refreshed, dict):
            raise TypeError("invalid refresh response")
        access_token = str(refreshed.get("access_token") or "").strip()
        if not access_token:
            raise KeyError("missing access_token in refresh response")
        raw_expires = refreshed.get("expires_in")
        expires_in = float(raw_expires) if isinstance(raw_expires, (int, float)) and not isinstance(raw_expires, bool) else 3600.0
        self._antigravity_token_cache = (fingerprint, access_token, time.monotonic() + expires_in)
        return access_token, project_id

    @staticmethod
    def _extract_oauth_token(payload: dict[str, Any]) -> tuple[str, datetime | None, str | None] | None:
        token_block = payload.get("token")
        if not isinstance(token_block, dict):
            return None
        access_token = str(token_block.get("access_token") or "").strip()
        if not access_token:
            return None
        expiry = parse_date(token_block.get("expiry"))
        project_id = str(payload.get("project_id") or token_block.get("project_id") or "").strip() or None
        return access_token, expiry, project_id

    def _resolve_antigravity_token(self) -> tuple[str, str | None]:
        explicit = os.environ.get("ANTIGRAVITY_TOKEN_PATH")
        if explicit:
            path = Path(explicit)
            payload = json.loads(path.read_text())
            if not isinstance(payload, dict):
                raise KeyError("invalid token file")
            if "refresh_token" in payload:
                return self._refresh_antigravity_acp_token(path, payload)
            extracted = self._extract_oauth_token(payload)
            if extracted is None:
                raise KeyError("missing access_token")
            return extracted[0], extracted[2]

        acp_paths = sorted(
            (p for p in self.home.glob(".t3/userdata/providers/antigravity/*/antigravity-acp/acp_token.json") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        oauth_paths = [
            p
            for p in (
                *self.home.glob(".t3/userdata/providers/antigravity/*/antigravity-acp/antigravity-oauth-token"),
                self.home / ".gemini/antigravity/antigravity-oauth-token",
                self.home / ".gemini/antigravity-cli/antigravity-oauth-token",
            )
            if p.is_file()
        ]
        if not acp_paths and not oauth_paths:
            raise FileNotFoundError("no Antigravity credential files found")

        auth_error: urllib.error.HTTPError | None = None
        network_error: Exception | None = None
        parse_error: Exception | None = None
        for path in acp_paths:
            try:
                payload = json.loads(path.read_text())
                if not isinstance(payload, dict):
                    raise KeyError("invalid acp_token.json")
                return self._refresh_antigravity_acp_token(path, payload)
            except urllib.error.HTTPError as err:
                auth_error = auth_error or err
            except (urllib.error.URLError, TimeoutError, OSError) as err:
                network_error = network_error or err
            except (KeyError, json.JSONDecodeError, ValueError, TypeError) as err:
                parse_error = parse_error or err

        now = datetime.now(timezone.utc)
        oauth_candidates: list[tuple[int, float, float, str, str | None]] = []
        for path in oauth_paths:
            try:
                payload = json.loads(path.read_text())
                if not isinstance(payload, dict):
                    raise KeyError("invalid oauth token file")
                extracted = self._extract_oauth_token(payload)
                if extracted is None:
                    raise KeyError("missing access_token")
                access_token, expiry, project_id = extracted
                if expiry is not None:
                    expiry_utc = (
                        expiry.astimezone(timezone.utc)
                        if expiry.tzinfo is not None
                        else expiry.replace(tzinfo=timezone.utc)
                    )
                    is_valid = 1 if expiry_utc > now else 0
                    expiry_ts = expiry_utc.timestamp()
                else:
                    is_valid = 0
                    expiry_ts = float("-inf")
                oauth_candidates.append((is_valid, expiry_ts, path.stat().st_mtime, access_token, project_id))
            except (KeyError, json.JSONDecodeError, ValueError, TypeError, OSError) as err:
                parse_error = parse_error or err

        if oauth_candidates:
            oauth_candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
            best = oauth_candidates[0]
            return best[3], best[4]
        if auth_error is not None:
            raise auth_error
        if network_error is not None:
            raise network_error
        if parse_error is not None:
            raise parse_error
        raise FileNotFoundError("no Antigravity credential files found")

    def antigravity(self) -> ProviderUsage:
        try:
            access_token, project_id = self._resolve_antigravity_token()
        except FileNotFoundError:
            return ProviderUsage("Antigravity", error="Not signed in — open Antigravity or sign in via T3 Code")
        except (KeyError, json.JSONDecodeError, ValueError, TypeError):
            return ProviderUsage("Antigravity", error="Antigravity credentials are unavailable")
        except urllib.error.HTTPError as error:
            message = (
                "Session expired — open Antigravity to refresh"
                if error.code in (400, 401, 403)
                else f"Service returned HTTP {error.code}"
            )
            return ProviderUsage("Antigravity", error=message)
        except (urllib.error.URLError, TimeoutError, OSError):
            return ProviderUsage("Antigravity", error="Could not reach Google")

        headers = {
            "Authorization": f"Bearer {access_token}",
            "User-Agent": _antigravity_user_agent(),
        }

        code_assist: dict[str, Any] = {}
        permission_denied = False
        try:
            response = self._json_post(
                f"{ANTIGRAVITY_ENDPOINTS[0]}/v1internal:loadCodeAssist",
                headers,
                {"metadata": ANTIGRAVITY_CLIENT_METADATA},
            )
            if isinstance(response, dict):
                code_assist = response
                if not project_id:
                    project_field = response.get("cloudaicompanionProject")
                    if isinstance(project_field, str) and project_field.strip():
                        project_id = project_field.strip()
                    elif isinstance(project_field, dict):
                        nested_id = str(project_field.get("id") or "").strip()
                        if nested_id:
                            project_id = nested_id
        except urllib.error.HTTPError as error:
            if error.code == 401:
                self._antigravity_token_cache = None
                return ProviderUsage("Antigravity", error="Session expired — open Antigravity to refresh")
            if error.code == 403:
                permission_denied = True
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError, TypeError):
            pass

        quota_body: dict[str, Any] = {"project": project_id} if project_id else {}
        entries: list[tuple[str, UsageWindow]] = []
        unauthorized = False
        http_error_code: int | None = None
        network_errors = 0
        summary_valid = 0

        for endpoint in ANTIGRAVITY_ENDPOINTS:
            try:
                payload = self._json_post(f"{endpoint}/v1internal:retrieveUserQuotaSummary", headers, quota_body)
                if isinstance(payload, dict) and ("groups" in payload or "buckets" in payload):
                    summary_valid += 1
                    entries.extend(parse_antigravity_summary(payload))
            except urllib.error.HTTPError as error:
                if error.code == 401:
                    unauthorized = True
                elif error.code == 403:
                    permission_denied = True
                else:
                    http_error_code = error.code
            except (urllib.error.URLError, TimeoutError, OSError):
                network_errors += 1
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

        if not entries:
            if unauthorized:
                self._antigravity_token_cache = None
                return ProviderUsage("Antigravity", error="Session expired — open Antigravity to refresh")
            if summary_valid == 0 and network_errors == len(ANTIGRAVITY_ENDPOINTS):
                return ProviderUsage("Antigravity", error="Could not reach Google")

            fallback_valid = 0
            fallback_network_errors = 0
            fallback_http_error: int | None = None
            for endpoint in ANTIGRAVITY_ENDPOINTS:
                try:
                    payload = self._json_post(f"{endpoint}/v1internal:fetchAvailableModels", headers, quota_body)
                    if isinstance(payload, dict) and isinstance(payload.get("models"), dict):
                        fallback_valid += 1
                        entries.extend(parse_antigravity_models(payload))
                except urllib.error.HTTPError as error:
                    if error.code == 401:
                        unauthorized = True
                    elif error.code == 403:
                        permission_denied = True
                    else:
                        fallback_http_error = error.code
                        http_error_code = error.code
                except (urllib.error.URLError, TimeoutError, OSError):
                    fallback_network_errors += 1
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass

            if not entries:
                if unauthorized:
                    self._antigravity_token_cache = None
                    return ProviderUsage("Antigravity", error="Session expired — open Antigravity to refresh")
                if fallback_valid == 0:
                    if fallback_http_error is not None or http_error_code is not None:
                        code = fallback_http_error if fallback_http_error is not None else http_error_code
                        return ProviderUsage("Antigravity", error=f"Service returned HTTP {code}")
                    if permission_denied:
                        return ProviderUsage("Antigravity", error="Antigravity quota access denied")
                    if summary_valid == 0 or fallback_network_errors == len(ANTIGRAVITY_ENDPOINTS):
                        return ProviderUsage("Antigravity", error="Could not reach Google")

        windows = merge_antigravity_windows(entries)
        has_active_five_hour = any(window.duration_seconds == 5 * 3600 for window in windows)
        plan = parse_antigravity_plan(code_assist, has_active_five_hour)
        return ProviderUsage("Antigravity", plan=plan, windows=windows)

    def fetch(self) -> list[ProviderUsage]:
        providers = [self.claude(), self.codex(), self.antigravity()]
        costs = self.cost_usage()
        for provider in providers:
            provider.local_usage = costs.get(provider.name.lower())
        return providers

    def cost_usage(self) -> dict[str, LocalUsage]:
        if self._cost_cache and time.monotonic() - self._cost_cached_at < 600:
            return self._cost_cache
        helper = shutil.which("codexbar")
        xdg_data = os.environ.get("XDG_DATA_HOME")
        data_home = Path(xdg_data) if xdg_data else self.home / ".local/share"
        bundled = data_home / "ai-usage-bar/bin/codexbar"
        if helper is None and bundled.is_file():
            helper = str(bundled)
        if helper is None:
            return self._claude_stats_fallback()
        try:
            result = subprocess.run(
                [helper, "cost", "--provider", "both", "--json"],
                check=True,
                capture_output=True,
                text=True,
                timeout=25,
            )
            payload = json.loads(result.stdout)
            parsed: dict[str, LocalUsage] = {}
            for item in payload if isinstance(payload, list) else []:
                provider = str(item.get("provider") or "")
                totals = item.get("totals") or {}
                models: dict[str, ModelBreakdown] = {}
                daily: list[DailyUsage] = []
                for day in item.get("daily") or []:
                    day_models: list[ModelBreakdown] = []
                    for model in day.get("modelBreakdowns") or []:
                        name = str(model.get("modelName") or "Unknown")
                        tokens = int(model.get("totalTokens") or 0)
                        cost = float(model.get("cost") or 0)
                        day_models.append(ModelBreakdown(name, tokens, cost))
                        aggregate = models.get(name, ModelBreakdown(name, 0, 0.0))
                        aggregate.tokens += tokens
                        aggregate.cost += cost
                        models[name] = aggregate
                    date = str(day.get("date") or "")
                    if date:
                        daily.append(
                            DailyUsage(
                                date,
                                float(day.get("totalCost") or 0),
                                int(day.get("totalTokens") or 0),
                                sorted(day_models, key=lambda model: model.cost, reverse=True),
                            )
                        )
                daily.sort(key=lambda day: day.date)
                today_date = datetime.now().astimezone().date().isoformat()
                today = next((day for day in reversed(daily) if day.date == today_date), None)
                parsed[provider] = LocalUsage(
                    cost=float(totals.get("totalCost") or item.get("last30DaysCostUSD") or 0),
                    tokens=int(totals.get("totalTokens") or item.get("last30DaysTokens") or 0),
                    models=sorted(models.values(), key=lambda model: model.cost, reverse=True),
                    history_days=int(item.get("historyDays") or 30),
                    today_cost=today.cost if today else 0.0,
                    today_tokens=today.tokens if today else 0,
                    daily=daily,
                )
            if parsed:
                self._cost_cache = parsed
                self._cost_cached_at = time.monotonic()
            return parsed
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError, TypeError, ValueError):
            return self._claude_stats_fallback()

    def _claude_stats_fallback(self) -> dict[str, LocalUsage]:
        try:
            data = json.loads((self.home / ".claude/stats-cache.json").read_text())
            models = []
            for name, value in (data.get("modelUsage") or {}).items():
                tokens = sum(int(value.get(key) or 0) for key in (
                    "inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"
                ))
                models.append(ModelBreakdown(name, tokens, float(value.get("costUSD") or 0)))
            models.sort(key=lambda model: model.cost, reverse=True)
            return {"claude": LocalUsage(sum(model.cost for model in models), sum(model.tokens for model in models), models, 0)}
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return {}


class WorkerSignals(QObject):
    done = pyqtSignal(object)


class FetchWorker(QRunnable):
    def __init__(self, client: UsageClient):
        super().__init__()
        self.client = client
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            providers = self.client.fetch()
        except Exception:
            providers = [ProviderUsage(spec.label, error="Refresh failed") for spec in PROVIDERS]
        self.signals.done.emit(providers)


def remaining_text(reset: datetime | None, now: datetime | None = None) -> str:
    if reset is None:
        return "reset unknown"
    now = now or datetime.now(timezone.utc)
    seconds = max(0, int((reset.astimezone(timezone.utc) - now).total_seconds()))
    if seconds == 0:
        return "resetting"
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return f"resets in {days}d {hours}h"
    if hours:
        return f"resets in {hours}h {minutes}m"
    return f"resets in {max(1, minutes)}m"


class UsageLane(QWidget):
    def __init__(self, quota: UsageWindow, parent: QWidget | None = None):
        super().__init__(parent)
        self.quota = quota
        self.setObjectName("usageLane")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 8)
        layout.setSpacing(4)
        name = QLabel(quota.label)
        name.setObjectName("usageLaneName")
        layout.addWidget(name)
        self.bar = QProgressBar()
        self.bar.setObjectName("quotaBar")
        self.bar.setRange(0, 1000)
        self.bar.setValue(round(max(0, min(100, quota.used)) * 10))
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(5)
        if quota.used >= 90:
            self.bar.setProperty("level", "critical")
        elif quota.used >= 70:
            self.bar.setProperty("level", "warn")
        layout.addWidget(self.bar)
        details = QHBoxLayout()
        percent = QLabel(f"{max(0, min(100, quota.used)):.0f}% used")
        percent.setObjectName("usageMeta")
        self.reset = QLabel()
        self.reset.setObjectName("usageMeta")
        self.reset.setAlignment(Qt.AlignmentFlag.AlignRight)
        details.addWidget(percent)
        details.addStretch()
        details.addWidget(self.reset)
        layout.addLayout(details)
        self.update_time()

    def update_time(self) -> None:
        if self.quota.resets_at is None and self.quota.starts_on_use:
            self.reset.setText("starts on use")
        else:
            self.reset.setText(remaining_text(self.quota.resets_at))


def updated_text(when: datetime | None, now: datetime | None = None) -> str:
    if when is None:
        return "Updated"
    now = now or datetime.now(timezone.utc)
    seconds = max(0, int((now - when.astimezone(timezone.utc)).total_seconds()))
    if seconds < 45:
        return "Updated just now"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"Updated {minutes} min ago"
    hours = round(minutes / 60)
    return f"Updated {hours}h ago"


def compact_tokens(tokens: int) -> str:
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.1f}K"
    return str(tokens)


def daily_label(day: DailyUsage) -> str:
    try:
        selected = datetime.fromisoformat(day.date).date()
    except ValueError:
        return day.date
    today = datetime.now().astimezone().date()
    if selected == today:
        return "Today"
    if (today - selected).days == 1:
        return "Yesterday"
    return selected.strftime("%a, %-d %b")


class ModelRow(QWidget):
    def __init__(self, model: ModelBreakdown | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(23)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 4, 0, 4)
        row.setSpacing(10)
        self.name = QLabel()
        self.name.setObjectName("modelName")
        self.tokens = QLabel()
        self.tokens.setObjectName("modelTokens")
        self.tokens.setFixedWidth(92)
        self.tokens.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.cost = QLabel()
        self.cost.setObjectName("modelCost")
        self.cost.setAlignment(Qt.AlignmentFlag.AlignRight)
        row.addWidget(self.name, 1)
        row.addWidget(self.tokens)
        row.addWidget(self.cost)
        self.set_model(model)

    def set_model(self, model: ModelBreakdown | None) -> None:
        self.name.setText(model.name if model else "")
        self.tokens.setText(f"{compact_tokens(model.tokens)} tokens" if model else "")
        self.cost.setText(f"${model.cost:,.2f}" if model else "")


class ActivityHeatmap(QWidget):
    day_selected = pyqtSignal(str)

    def __init__(self, daily: list[DailyUsage], parent: QWidget | None = None):
        super().__init__(parent)
        self.daily = {day.date: day for day in daily}
        self.today = datetime.now().astimezone().date()
        self.start = self.today - timedelta(days=34)
        self.selected_date = self.today.isoformat()
        self.setObjectName("activityHeatmap")
        self.setFixedSize(82, 58)
        self.setMouseTracking(True)

    def _cell_rect(self, index: int) -> QRect:
        column, row = divmod(index, 7)
        return QRect(2 + column * 16, 2 + row * 8, 12, 5)

    def _day_at(self, position: QPoint) -> tuple[str, DailyUsage | None] | None:
        for index in range(35):
            if self._cell_rect(index).contains(position):
                day = self.start + timedelta(days=index)
                date = day.isoformat()
                return date, self.daily.get(date)
        return None

    def paintEvent(self, a0: Any) -> None:
        del a0
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        maximum = max((day.tokens for day in self.daily.values()), default=0)
        base = QApplication.palette().color(QApplication.palette().ColorRole.Midlight)
        accent = QColor("#27ae60")
        for index in range(35):
            date = (self.start + timedelta(days=index)).isoformat()
            day = self.daily.get(date)
            color = QColor(base)
            if day and day.tokens > 0 and maximum > 0:
                strength = 0.26 + 0.74 * (day.tokens / maximum) ** 0.45
                color = QColor(accent)
                color.setAlpha(round(255 * strength))
            rect = self._cell_rect(index)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(rect, 2, 2)
            if date == self.selected_date:
                painter.setPen(QApplication.palette().color(QApplication.palette().ColorRole.Highlight))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), 3, 3)
        painter.end()

    def mouseMoveEvent(self, a0: Any) -> None:
        result = self._day_at(a0.position().toPoint())
        if result is None:
            QToolTip.hideText()
            return
        date, day = result
        if day:
            text = f"{daily_label(day)} · ${day.cost:,.2f} · {compact_tokens(day.tokens)} tokens"
        else:
            text = f"{date} · no recorded activity"
        QToolTip.showText(a0.globalPosition().toPoint(), text, self)

    def mousePressEvent(self, a0: Any) -> None:
        result = self._day_at(a0.position().toPoint())
        if result and result[1]:
            self.day_selected.emit(result[0])

    def set_selected_date(self, date: str) -> None:
        self.selected_date = date
        self.update()


class ModelHeader(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 4, 0, 1)
        row.setSpacing(10)
        name = QLabel("MODELS")
        name.setObjectName("tableHeader")
        tokens = QLabel("TOKENS")
        tokens.setObjectName("tableHeader")
        tokens.setFixedWidth(92)
        tokens.setAlignment(Qt.AlignmentFlag.AlignRight)
        cost = QLabel("COST")
        cost.setObjectName("tableHeader")
        cost.setFixedWidth(48)
        cost.setAlignment(Qt.AlignmentFlag.AlignRight)
        row.addWidget(name, 1)
        row.addWidget(tokens)
        row.addWidget(cost)


class ProviderCard(QFrame):
    def __init__(self, usage: ProviderUsage, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("providerSection")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 5, 2, 2)
        layout.setSpacing(5)
        header = QHBoxLayout()
        header.setContentsMargins(2, 0, 2, 0)
        identity = QVBoxLayout()
        identity.setSpacing(0)
        title = QLabel(usage.name)
        title.setObjectName("provider")
        self.has_error = bool(usage.error)
        self.status = QLabel("●  Unavailable" if usage.error else "●  Updated just now")
        self.status.setObjectName("statusError" if usage.error else "statusGood")
        identity.addWidget(title)
        identity.addWidget(self.status)
        header.addLayout(identity)
        header.addStretch()
        if usage.plan:
            plan = QLabel(usage.plan)
            plan.setObjectName("plan")
            header.addWidget(plan)
        layout.addLayout(header)
        layout.addWidget(self._divider())
        self.lanes: list[UsageLane] = []
        if usage.error:
            state = QFrame()
            state.setObjectName("statePanel")
            state_layout = QVBoxLayout(state)
            state_layout.setContentsMargins(11, 10, 11, 10)
            error = QLabel(usage.error)
            error.setObjectName("stateText")
            error.setWordWrap(True)
            state_layout.addWidget(error)
            layout.addWidget(state)
        elif not usage.windows:
            state = QFrame()
            state.setObjectName("statePanel")
            state_layout = QVBoxLayout(state)
            state_layout.setContentsMargins(11, 10, 11, 10)
            empty = QLabel("No active quota windows reported")
            empty.setObjectName("stateText")
            state_layout.addWidget(empty)
            layout.addWidget(state)
        else:
            usage_group = QFrame()
            usage_group.setObjectName("usageGroup")
            usage_layout = QVBoxLayout(usage_group)
            usage_layout.setContentsMargins(0, 0, 0, 0)
            usage_layout.setSpacing(0)
            for quota in usage.windows:
                lane = UsageLane(quota)
                self.lanes.append(lane)
                if self.lanes[:-1]:
                    lane_divider = QFrame()
                    lane_divider.setObjectName("usageDivider")
                    lane_divider.setFixedHeight(1)
                    usage_layout.addWidget(lane_divider)
                usage_layout.addWidget(lane)
            layout.addWidget(usage_group)
        if usage.reset_credits is not None:
            layout.addWidget(self._divider())
            reset_row = QHBoxLayout()
            reset_copy = QVBoxLayout()
            reset_copy.setSpacing(0)
            reset_title = QLabel("Limit reset credits")
            reset_title.setObjectName("sectionTitle")
            reset_hint = QLabel("Full quota resets")
            reset_hint.setObjectName("muted")
            reset_copy.addWidget(reset_title)
            reset_copy.addWidget(reset_hint)
            reset_count = QLabel(f"{usage.reset_credits} available")
            reset_count.setObjectName("resetCount")
            reset_row.addLayout(reset_copy)
            reset_row.addStretch()
            reset_row.addWidget(reset_count)
            layout.addLayout(reset_row)
        if usage.local_usage:
            layout.addWidget(self._divider())
            if usage.local_usage.daily:
                activity_row = QHBoxLayout()
                activity_copy = QVBoxLayout()
                activity_copy.setSpacing(0)
                activity_title = QLabel("Activity")
                activity_title.setObjectName("sectionTitle")
                activity_hint = QLabel(f"Last {usage.local_usage.history_days} days · local history")
                activity_hint.setObjectName("muted")
                activity_copy.addWidget(activity_title)
                activity_copy.addWidget(activity_hint)
                self.heatmap = ActivityHeatmap(usage.local_usage.daily)
                self.heatmap.day_selected.connect(self._select_heatmap_day)
                activity_row.addLayout(activity_copy)
                activity_row.addStretch()
                activity_row.addWidget(self.heatmap)
                layout.addLayout(activity_row)
            cost_header = QHBoxLayout()
            cost_title = QLabel("Cost")
            cost_title.setObjectName("sectionTitle")
            cost_header.addWidget(cost_title)
            cost_header.addStretch()
            self.day_picker: QComboBox | None = None
            self.day_entries = list(reversed(usage.local_usage.daily))
            if self.day_entries:
                self.day_picker = QComboBox()
                self.day_picker.setObjectName("dayPicker")
                for day in self.day_entries:
                    self.day_picker.addItem(daily_label(day))
                self.day_picker.setToolTip("Choose a day from local usage history")
                self.day_picker.currentIndexChanged.connect(self._set_selected_day)
                cost_header.addWidget(self.day_picker)
            layout.addLayout(cost_header)
            self.selected_day_line = QLabel()
            self.selected_day_line.setObjectName("costLine")
            period_label = (
                f"Last {usage.local_usage.history_days} days" if usage.local_usage.history_days else "All time"
            )
            total = QLabel(
                f"{period_label}: ${usage.local_usage.cost:,.2f} · "
                f"{compact_tokens(usage.local_usage.tokens)} tokens"
            )
            total.setObjectName("costLine")
            layout.addWidget(self.selected_day_line)
            layout.addWidget(total)
            layout.addWidget(ModelHeader())
            self.model_rows = [ModelRow() for _ in range(5)]
            for row in self.model_rows:
                layout.addWidget(row)
            if self.day_entries:
                self._set_selected_day(0)
            else:
                self.selected_day_line.setText(
                    f"Today: ${usage.local_usage.today_cost:,.2f} · "
                    f"{compact_tokens(usage.local_usage.today_tokens)} tokens"
                )
                self._set_models(usage.local_usage.models)
        layout.addStretch(1)

    def _set_selected_day(self, index: int) -> None:
        if not (0 <= index < len(self.day_entries)):
            return
        day = self.day_entries[index]
        self.selected_day_line.setText(
            f"{daily_label(day)}: ${day.cost:,.2f} · {compact_tokens(day.tokens)} tokens"
        )
        if hasattr(self, "heatmap"):
            self.heatmap.set_selected_date(day.date)
        self._set_models(day.models)

    def _select_heatmap_day(self, date: str) -> None:
        for index, day in enumerate(self.day_entries):
            if day.date == date and self.day_picker is not None:
                self.day_picker.setCurrentIndex(index)
                return

    def _set_models(self, models: list[ModelBreakdown]) -> None:
        for row, model in zip(self.model_rows, models[: len(self.model_rows)]):
            row.set_model(model)
        for row in self.model_rows[len(models) :]:
            row.set_model(None)

    def set_updated(self, text: str) -> None:
        if not self.has_error:
            self.status.setText(f"●  {text}")

    @staticmethod
    def _divider() -> QFrame:
        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFrameShape(QFrame.Shape.HLine)
        return divider


def provider_icon(provider: str) -> QIcon:
    spec = PROVIDER_MAP.get(provider)
    if spec is None:
        return QIcon.fromTheme("applications-development")
    source = QPixmap(str(ASSET_DIR / spec.icon_asset))
    if source.isNull():
        return QIcon.fromTheme("applications-development")

    def tinted(color_role: QPalette.ColorRole) -> QPixmap:
        pixmap = source.scaled(
            64,
            64,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        painter = QPainter(pixmap)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(pixmap.rect(), QApplication.palette().color(color_role))
        painter.end()
        return pixmap

    icon = QIcon()
    icon.addPixmap(tinted(QApplication.palette().ColorRole.ButtonText), QIcon.Mode.Normal, QIcon.State.Off)
    icon.addPixmap(tinted(QApplication.palette().ColorRole.HighlightedText), QIcon.Mode.Normal, QIcon.State.On)
    return icon


class UsagePopup(QWidget):
    refresh_requested = pyqtSignal()

    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setObjectName("popup")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(402)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        self.panel = QFrame()
        self.panel.setObjectName("panel")
        outer.addWidget(self.panel)
        self.content_layout = QVBoxLayout(self.panel)
        self.content_layout.setContentsMargins(12, 10, 12, 9)
        self.content_layout.setSpacing(7)
        nav = QFrame()
        nav.setObjectName("nav")
        nav_layout = QHBoxLayout(nav)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(5)
        self.tabs = QButtonGroup(self)
        self.tabs.setExclusive(True)
        self.tab_buttons: dict[str, QToolButton] = {}
        self.tab_bars: dict[str, QProgressBar] = {}
        default_key = PROVIDERS[0].key
        for spec in PROVIDERS:
            tab = QWidget()
            tab_layout = QVBoxLayout(tab)
            tab_layout.setContentsMargins(0, 0, 0, 0)
            tab_layout.setSpacing(2)
            button = QToolButton()
            button.setObjectName("providerTab")
            button.setText(spec.label)
            button.setIcon(provider_icon(spec.key))
            button.setIconSize(QSize(21, 21))
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            button.setCheckable(True)
            button.setFixedHeight(52)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.clicked.connect(lambda _checked, selected=spec.key: self.set_filter(selected))
            self.tabs.addButton(button)
            self.tab_buttons[spec.key] = button
            meter = QProgressBar()
            meter.setObjectName("tabMeter")
            meter.setRange(0, 100)
            meter.setTextVisible(False)
            meter.setFixedHeight(2)
            self.tab_bars[spec.key] = meter
            tab_layout.addWidget(button)
            tab_layout.addWidget(meter)
            nav_layout.addWidget(tab)
            if spec.key == default_key:
                button.setChecked(True)
        self.content_layout.addWidget(nav)
        self.content_layout.addWidget(ProviderCard._divider())
        self.loading = QLabel("Refreshing usage…")
        self.loading.setObjectName("loadingText")
        self.loading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading.setMinimumHeight(112)
        self.content_layout.addWidget(self.loading)
        self.cards: list[ProviderCard] = []
        self.updated_at: datetime | None = None
        self.current_filter = default_key
        self.footer = QFrame()
        self.footer.setObjectName("footerFrame")
        footer_layout = QHBoxLayout(self.footer)
        footer_layout.setContentsMargins(2, 1, 0, 0)
        footer_text = QLabel("Local data · refreshes every 2 min")
        footer_text.setObjectName("footer")
        self.refresh = QPushButton()
        self.refresh.setObjectName("iconButton")
        self.refresh.setIcon(QIcon.fromTheme("view-refresh-symbolic", QIcon.fromTheme("view-refresh")))
        self.refresh.setIconSize(QSize(14, 14))
        self.refresh.setToolTip("Refresh now")
        self.refresh.setFixedSize(26, 26)
        self.refresh.clicked.connect(self.refresh_requested)
        footer_layout.addWidget(footer_text)
        footer_layout.addStretch()
        footer_layout.addWidget(self.refresh)
        self.footer.hide()
        self.content_layout.addWidget(self.footer)

    def event(self, a0: QEvent | None) -> bool:
        if a0 is not None and a0.type() == QEvent.Type.WindowDeactivate and self.isVisible():
            QTimer.singleShot(0, self.hide)
        return super().event(a0)

    def set_loading(self, loading: bool) -> None:
        self.refresh.setEnabled(not loading)
        if loading and not self.cards:
            self.loading.show()
        elif self.cards:
            self.loading.hide()

    def set_data(self, providers: list[ProviderUsage]) -> None:
        self.updated_at = datetime.now(timezone.utc)
        self.setMinimumHeight(0)
        self.setMaximumHeight(16_777_215)
        for card in self.cards:
            self.content_layout.removeWidget(card)
            card.deleteLater()
        self.cards = []
        for provider in providers:
            card = ProviderCard(provider)
            provider_key = provider.name.lower()
            card.setProperty("providerName", provider_key)
            meter = self.tab_bars.get(provider_key)
            if meter is not None:
                meter.setValue(round(max((quota.used for quota in provider.windows), default=0)))
            self.cards.append(card)
        for card in self.cards:
            self.content_layout.insertWidget(self.content_layout.count() - 1, card)
        stable_card_height = max((card.sizeHint().height() for card in self.cards), default=0)
        for card in self.cards:
            card.setFixedHeight(stable_card_height)
        self.loading.hide()
        self.footer.show()
        self._apply_updated()
        self.set_filter(self.current_filter)
        self.adjustSize()
        self.setFixedHeight(self.sizeHint().height())

    def set_filter(self, selected: str) -> None:
        self.current_filter = selected
        button = self.tab_buttons.get(selected)
        if button is not None:
            button.setChecked(True)
        for card in self.cards:
            card.setVisible(card.property("providerName") == selected)

    def _apply_updated(self) -> None:
        text = updated_text(self.updated_at)
        for card in self.cards:
            card.set_updated(text)

    def update_times(self) -> None:
        self._apply_updated()
        for card in self.cards:
            for lane in card.lanes:
                lane.update_time()


class TrayApp(QObject):
    def __init__(self, app: QApplication):
        super().__init__()
        self.app = app
        self.client = UsageClient()
        global_pool = QThreadPool.globalInstance()
        self.pool = global_pool if global_pool is not None else QThreadPool(self)
        self.providers: list[ProviderUsage] = []
        self.fetching = False
        self.popup = UsagePopup()
        self.popup.refresh_requested.connect(self.refresh)
        self.tray = QSystemTrayIcon(self.make_icon([]), self)
        self.tray.setToolTip(APP_NAME + "\nChecking usage…")
        self.tray.activated.connect(self.activated)
        self.tray.setContextMenu(self.make_menu())
        self.tray.show()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(REFRESH_MS)
        self.refresh_timer.timeout.connect(self.refresh)
        self.refresh_timer.start()
        self.clock_timer = QTimer(self)
        self.clock_timer.setInterval(30_000)
        self.clock_timer.timeout.connect(self.popup.update_times)
        self.clock_timer.start()
        QTimer.singleShot(0, self.refresh)

    def make_menu(self) -> QMenu:
        menu = QMenu()
        open_action = QAction(QIcon.fromTheme("office-chart-bar"), "Show usage", menu)
        open_action.triggered.connect(self.show_popup)
        refresh_action = QAction(QIcon.fromTheme("view-refresh"), "Refresh", menu)
        refresh_action.triggered.connect(self.refresh)
        quit_action = QAction(QIcon.fromTheme("application-exit"), "Quit", menu)
        quit_action.triggered.connect(self.app.quit)
        menu.addAction(open_action)
        menu.addAction(refresh_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        return menu

    def activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_popup()

    def show_popup(self) -> None:
        if self.popup.isVisible():
            self.popup.hide()
            return
        self.popup.adjustSize()
        cursor = QCursor.pos()
        screen = self.app.screenAt(cursor) or self.app.primaryScreen()
        if screen is None:
            self.popup.show()
            return
        bounds = screen.availableGeometry()
        tray_geo = self.tray.geometry()
        anchor = tray_geo.center() if tray_geo.isValid() else cursor
        x = max(bounds.left() + 8, min(anchor.x() - self.popup.width() // 2, bounds.right() - self.popup.width() - 8))
        if anchor.y() < bounds.center().y():
            y = min(bounds.bottom() - self.popup.height() - 8, tray_geo.bottom() + 8)
        else:
            y = max(bounds.top() + 8, tray_geo.top() - self.popup.height() - 8)
        self.popup.move(QPoint(x, y))
        self.popup.show()
        self.popup.raise_()

    def refresh(self) -> None:
        if self.fetching:
            return
        self.fetching = True
        self.popup.set_loading(True)
        worker = FetchWorker(self.client)
        worker.signals.done.connect(self.refreshed)
        self.pool.start(worker)

    def refreshed(self, providers: list[ProviderUsage]) -> None:
        self.fetching = False
        self.providers = providers
        self.popup.set_loading(False)
        self.popup.set_data(providers)
        self.tray.setIcon(self.make_icon(providers))
        lines = [APP_NAME]
        for provider in providers:
            if provider.error:
                lines.append(f"{provider.name}: {provider.error}")
            elif provider.windows:
                lanes = ", ".join(f"{window.label} {window.used:.0f}%" for window in provider.windows)
                lines.append(f"{provider.name}: {lanes}")
        self.tray.setToolTip("\n".join(lines))

    @staticmethod
    def make_icon(providers: list[ProviderUsage]) -> QIcon:
        size = 44
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        foreground = QApplication.palette().color(QApplication.palette().ColorRole.WindowText)
        muted = QColor(foreground)
        muted.setAlpha(65)
        if providers:
            values = [
                max((window.used for window in provider.windows), default=0.0) if not provider.error else -1.0
                for provider in providers
            ]
        else:
            values = [0.0] * len(PROVIDERS)
        count = len(values)
        if count == 1:
            bar_height, gap = 12, 0
        elif count == 2:
            bar_height, gap = 10, 6
        elif count == 3:
            bar_height, gap = 8, 4
        else:
            gap = 3
            bar_height = max(4, (size - 12 - gap * (count - 1)) // count)
        total_height = count * bar_height + max(0, count - 1) * gap
        top = (size - total_height) // 2
        radius = bar_height / 2
        painter.setPen(Qt.PenStyle.NoPen)
        for index, value in enumerate(values):
            rect = QRect(5, top + index * (bar_height + gap), 34, bar_height)
            painter.setBrush(muted)
            painter.drawRoundedRect(rect, radius, radius)
            fill = QRect(rect)
            fill.setWidth(max(4, round(rect.width() * max(0.0, min(100.0, value)) / 100.0)))
            color = QColor("#da4453") if value < 0 or value >= 90 else QColor("#f67400") if value >= 70 else foreground
            painter.setBrush(color)
            painter.drawRoundedRect(fill, radius, radius)
        painter.end()
        return QIcon(pixmap)


STYLESHEET = """
QWidget { font-family: "Noto Sans"; font-size: 13px; }
QFrame#panel { background: palette(window); border: 1px solid palette(mid); border-radius: 12px; }
QLabel#provider { font-size: 16px; font-weight: 650; padding: 1px 0; }
QLabel#plan { color: palette(placeholder-text); font-size: 11px; padding-right: 2px; }
QLabel#muted { color: palette(placeholder-text); font-size: 10px; }
QLabel#statusGood { color: #27ae60; font-size: 9px; }
QLabel#statusError { color: #da4453; font-size: 9px; }
QLabel#loadingText { color: palette(placeholder-text); font-size: 11px; }
QLabel#stateText { color: palette(placeholder-text); font-size: 11px; }
QLabel#sectionTitle { font-size: 13px; font-weight: 600; padding-top: 4px; }
QLabel#resetCount { font-size: 11px; font-weight: 600; }
QLabel#costLine { font-size: 11px; padding: 1px 2px; }
QLabel#tableHeader { color: palette(placeholder-text); font-size: 8px; font-weight: 700; letter-spacing: .4px; }
QLabel#modelName { font-size: 10px; }
QLabel#modelTokens { color: palette(placeholder-text); font-size: 9px; }
QLabel#modelCost { font-family: "Noto Sans Mono"; font-size: 10px; font-weight: 600; min-width: 48px; }
QLabel#footer { color: palette(placeholder-text); font-size: 8px; padding-top: 3px; }
QComboBox#dayPicker { background: palette(alternate-base); border: 1px solid palette(midlight); border-radius: 5px; padding: 2px 6px; font-size: 10px; min-width: 96px; }
QComboBox#dayPicker:hover { border-color: palette(highlight); }
QComboBox#dayPicker::drop-down { border: none; width: 18px; }
QComboBox#dayPicker QAbstractItemView { background: palette(base); border: 1px solid palette(mid); selection-background-color: palette(highlight); selection-color: palette(highlighted-text); }
QFrame#providerSection { background: transparent; border: none; }
QFrame#footerFrame { background: transparent; border: none; }
QFrame#divider { color: palette(midlight); max-height: 1px; border: none; background: palette(midlight); }
QFrame#nav { background: transparent; border: none; }
QFrame#usageGroup { background: palette(alternate-base); border: 1px solid palette(midlight); border-radius: 7px; }
QFrame#usageDivider { background: rgba(127, 127, 127, 45); border: none; margin-left: 10px; margin-right: 10px; }
QFrame#statePanel { background: palette(alternate-base); border: 1px solid palette(midlight); border-radius: 7px; }
QWidget#usageLane { background: transparent; }
QLabel#usageLaneName { color: palette(text); font-size: 13px; font-weight: 650; }
QLabel#usageMeta { color: palette(placeholder-text); font-size: 10px; }
QToolButton#providerTab { background: transparent; border: none; border-radius: 7px; padding: 3px 10px; color: palette(placeholder-text); font-size: 10px; }
QToolButton#providerTab:hover { color: palette(text); background: palette(alternate-base); }
QToolButton#providerTab:checked { background: palette(highlight); color: palette(highlighted-text); font-weight: 650; }
QProgressBar { background: palette(midlight); border: none; border-radius: 3px; }
QProgressBar::chunk { background: palette(highlight); border-radius: 3px; }
QProgressBar#quotaBar { background: rgba(127, 127, 127, 55); border: none; border-radius: 2px; }
QProgressBar#quotaBar::chunk { background: #27ae60; border-radius: 2px; }
QProgressBar#quotaBar[level="warn"]::chunk { background: #e8a33d; }
QProgressBar#quotaBar[level="critical"]::chunk { background: #e0455e; }
QProgressBar#tabMeter { background: palette(midlight); border-radius: 1px; }
QProgressBar#tabMeter::chunk { background: palette(highlight); border-radius: 1px; }
QPushButton#iconButton { border: none; border-radius: 7px; background: transparent; }
QPushButton#iconButton:hover { background: palette(alternate-base); }
QPushButton#iconButton:disabled { opacity: .45; }
"""


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORMTHEME", "kde")
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setDesktopFileName(APP_ID)
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(STYLESHEET)
    runtime_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.RuntimeLocation) or "/tmp"
    lock = QLockFile(str(Path(runtime_dir) / f"{APP_ID}.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        if not lock.removeStaleLockFile() or not lock.tryLock(100):
            return 0
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("AI Usage Bar: no system tray is available", file=sys.stderr)
        return 1
    tray_app = TrayApp(app)
    exit_code = app.exec()
    del tray_app
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
