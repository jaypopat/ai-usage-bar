#!/usr/bin/env python3
"""A small, local-first Claude Code and Codex tray meter for KDE Plasma."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSystemTrayIcon,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "AI Usage Bar"
APP_ID = "io.github.aiusagebar"
VERSION = "0.3.0"
REFRESH_MS = 120_000
ASSET_DIR = Path(__file__).resolve().parent / "assets"


@dataclass
class UsageWindow:
    label: str
    used: float
    resets_at: datetime | None
    duration_seconds: int | None = None


@dataclass
class ModelBreakdown:
    name: str
    tokens: int
    cost: float


@dataclass
class LocalUsage:
    cost: float
    tokens: int
    models: list[ModelBreakdown] = field(default_factory=list)
    history_days: int = 30
    today_cost: float = 0.0
    today_tokens: int = 0


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


class UsageClient:
    """Reads existing CLI credentials and fetches only provider quota endpoints."""

    def __init__(self, home: Path | None = None, timeout: int = 15):
        self.home = home or Path.home()
        self.timeout = timeout
        self._cost_cache: dict[str, LocalUsage] = {}
        self._cost_cached_at = 0.0

    def _json_request(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"Accept": "application/json", **headers})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.load(response)

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

    def fetch(self) -> list[ProviderUsage]:
        providers = [self.claude(), self.codex()]
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
                for day in item.get("daily") or []:
                    for model in day.get("modelBreakdowns") or []:
                        name = str(model.get("modelName") or "Unknown")
                        current = models.get(name, ModelBreakdown(name, 0, 0.0))
                        current.tokens += int(model.get("totalTokens") or 0)
                        current.cost += float(model.get("cost") or 0)
                        models[name] = current
                parsed[provider] = LocalUsage(
                    cost=float(totals.get("totalCost") or item.get("last30DaysCostUSD") or 0),
                    tokens=int(totals.get("totalTokens") or item.get("last30DaysTokens") or 0),
                    models=sorted(models.values(), key=lambda model: model.cost, reverse=True),
                    history_days=int(item.get("historyDays") or 30),
                    today_cost=float((item.get("daily") or [{}])[-1].get("totalCost") or 0),
                    today_tokens=int((item.get("daily") or [{}])[-1].get("totalTokens") or 0),
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
        self.signals.done.emit(self.client.fetch())


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


class ModelRow(QWidget):
    def __init__(self, model: ModelBreakdown, parent: QWidget | None = None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 4, 0, 4)
        row.setSpacing(10)
        name = QLabel(model.name)
        name.setObjectName("modelName")
        tokens = QLabel(f"{compact_tokens(model.tokens)} tokens")
        tokens.setObjectName("modelTokens")
        tokens.setFixedWidth(92)
        tokens.setAlignment(Qt.AlignmentFlag.AlignRight)
        cost = QLabel(f"${model.cost:,.2f}")
        cost.setObjectName("modelCost")
        cost.setAlignment(Qt.AlignmentFlag.AlignRight)
        row.addWidget(name, 1)
        row.addWidget(tokens)
        row.addWidget(cost)


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
            cost_header = QHBoxLayout()
            cost_title = QLabel("Cost")
            cost_title.setObjectName("sectionTitle")
            cost_header.addWidget(cost_title)
            cost_header.addStretch()
            layout.addLayout(cost_header)
            today = QLabel(
                f"Today: ${usage.local_usage.today_cost:,.2f} · "
                f"{compact_tokens(usage.local_usage.today_tokens)} tokens"
            )
            today.setObjectName("costLine")
            period_label = (
                f"Last {usage.local_usage.history_days} days" if usage.local_usage.history_days else "All time"
            )
            total = QLabel(
                f"{period_label}: ${usage.local_usage.cost:,.2f} · "
                f"{compact_tokens(usage.local_usage.tokens)} tokens"
            )
            total.setObjectName("costLine")
            layout.addWidget(today)
            layout.addWidget(total)
            layout.addWidget(ModelHeader())
            for model in usage.local_usage.models[:5]:
                layout.addWidget(ModelRow(model))

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
    asset_name = "claude.svg" if provider == "claude" else "openai.svg"
    source = QPixmap(str(ASSET_DIR / asset_name))
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
        for key, text in (("claude", "Claude"), ("codex", "Codex")):
            tab = QWidget()
            tab_layout = QVBoxLayout(tab)
            tab_layout.setContentsMargins(0, 0, 0, 0)
            tab_layout.setSpacing(2)
            button = QToolButton()
            button.setObjectName("providerTab")
            button.setText(text)
            button.setIcon(provider_icon(key))
            button.setIconSize(QSize(21, 21))
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            button.setCheckable(True)
            button.setFixedHeight(52)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.clicked.connect(lambda _checked, selected=key: self.set_filter(selected))
            self.tabs.addButton(button)
            self.tab_buttons[key] = button
            meter = QProgressBar()
            meter.setObjectName("tabMeter")
            meter.setRange(0, 100)
            meter.setTextVisible(False)
            meter.setFixedHeight(2)
            self.tab_bars[key] = meter
            tab_layout.addWidget(button)
            tab_layout.addWidget(meter)
            nav_layout.addWidget(tab)
            if key == "claude":
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
        self.current_filter = "claude"
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
        values = []
        for provider in providers[:2]:
            values.append(max((window.used for window in provider.windows), default=0) if not provider.error else -1)
        while len(values) < 2:
            values.append(0)
        painter.setPen(Qt.PenStyle.NoPen)
        for index, value in enumerate(values):
            rect = QRect(5, 8 + index * 16, 34, 10)
            painter.setBrush(muted)
            painter.drawRoundedRect(rect, 5, 5)
            fill = QRect(rect)
            fill.setWidth(max(4, round(rect.width() * max(0, value) / 100)))
            color = QColor("#da4453") if value < 0 or value >= 90 else QColor("#f67400") if value >= 70 else foreground
            painter.setBrush(color)
            painter.drawRoundedRect(fill, 5, 5)
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
