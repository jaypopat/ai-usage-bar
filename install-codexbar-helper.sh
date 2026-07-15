#!/usr/bin/env bash
set -euo pipefail

version="0.43.0"
asset="CodexBarCLI-v${version}-linux-x86_64.tar.gz"
url="https://github.com/steipete/CodexBar/releases/download/v${version}/${asset}"
checksum_url="${url}.sha256"
app_dir="${XDG_DATA_HOME:-$HOME/.local/share}/ai-usage-bar"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

curl --fail --location --progress-bar "$url" --output "$tmp_dir/$asset"
curl --fail --location --silent --show-error "$checksum_url" --output "$tmp_dir/$asset.sha256"
(cd "$tmp_dir" && sha256sum --check "$asset.sha256")
tar -xzf "$tmp_dir/$asset" -C "$tmp_dir" CodexBarCLI
mkdir -p "$app_dir/bin"
install -m 755 "$tmp_dir/CodexBarCLI" "$app_dir/bin/CodexBarCLI"
ln -sfn CodexBarCLI "$app_dir/bin/codexbar"
echo "Installed CodexBar cost helper ${version}."
