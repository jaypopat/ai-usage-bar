#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
app_dir="${XDG_DATA_HOME:-$HOME/.local/share}/ai-usage-bar"
bin_dir="$HOME/.local/bin"
applications_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
autostart_dir="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"

mkdir -p "$app_dir/assets" "$bin_dir" "$applications_dir" "$autostart_dir"
install -m 755 "$root/ai_usage_bar.py" "$app_dir/ai_usage_bar.py"
install -m 644 "$root/assets/claude.svg" "$root/assets/openai.svg" "$app_dir/assets/"
ln -sfn "$app_dir/ai_usage_bar.py" "$bin_dir/ai-usage-bar"

desktop="[Desktop Entry]
Type=Application
Name=AI Usage Bar
Comment=Claude Code and Codex quota meter
Exec=$bin_dir/ai-usage-bar
Icon=office-chart-bar
Terminal=false
Categories=System;
StartupNotify=false
X-KDE-autostart-after=panel
X-GNOME-Autostart-enabled=true"

printf '%s\n' "$desktop" > "$applications_dir/io.github.aiusagebar.desktop"
printf '%s\n' "$desktop" > "$autostart_dir/io.github.aiusagebar.desktop"
chmod 644 "$applications_dir/io.github.aiusagebar.desktop" "$autostart_dir/io.github.aiusagebar.desktop"
update-desktop-database "$applications_dir" 2>/dev/null || true
echo "Installed AI Usage Bar. Run: ai-usage-bar"
