# AI Usage Bar

A tiny KDE Plasma tray utility for Claude Code and OpenAI Codex quota windows. It reads the sessions
already maintained by the two CLIs, fetches their usage endpoints every two minutes, and keeps all data
in memory. Five-hour, daily, weekly, and model-specific Codex lanes are supported whenever the provider
reports them. There is no account setup, database, analytics, helper daemon, or browser-cookie access.

## Run

Requirements: Linux, Python 3.11+, PyQt6, and KDE Plasma (tested on Plasma 6 / Wayland).

```bash
./ai_usage_bar.py
```

Click the tray meter for the compact details popup. The fixed-size provider switcher opens on Claude
and switches to Codex without moving or resizing the popup; right-click the tray icon for refresh and
quit. A runtime lock ensures only one copy can run per desktop session.

## Install and start at login

```bash
./install.sh
./install-codexbar-helper.sh  # optional exact local cost/model breakdowns
ai-usage-bar
```

The installer copies the one-file application under `~/.local/share`, adds a launcher in
`~/.local/bin`, and enables a standard XDG autostart entry. Run `./uninstall.sh` to remove it.
The optional official CodexBar CLI helper is invoked only for local cost scans and exits afterward;
it is not another resident service. Cost results are cached for ten minutes.

## Privacy and credentials

The app reads `~/.claude/.credentials.json` and `~/.codex/auth.json` without modifying them. Tokens are
used only as authorization headers for `api.anthropic.com/api/oauth/usage` and
`chatgpt.com/backend-api/wham/usage`. They are never logged or stored by this app. If a session expires,
sign in again with `claude` or `codex login`; the next refresh picks up the new credentials.

## Test

```bash
python3 -m unittest discover -v
```
