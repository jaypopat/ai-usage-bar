#!/usr/bin/env bash
set -euo pipefail
rm -f "$HOME/.local/bin/ai-usage-bar"
rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/ai-usage-bar"
rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/applications/io.github.aiusagebar.desktop"
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/io.github.aiusagebar.desktop"
echo "AI Usage Bar removed."
