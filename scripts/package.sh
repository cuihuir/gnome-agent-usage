#!/usr/bin/env bash
# Purpose: build a flat zip for extensions.gnome.org.
# Dependency: zip, msgfmt (gettext).
# Effect: writes dist/agent-usage@fedora-enhance.shell-extension.zip. Does not install it.
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT
mkdir -p "$workdir/collector" "$workdir/locale/zh_CN/LC_MESSAGES" "$root/dist"
cp "$root/metadata.json" "$root/extension.js" "$root/stylesheet.css" "$root/LICENSE" "$workdir/"
cp "$root/collector/agent_usage.py" "$workdir/collector/"
chmod 755 "$workdir/collector/agent_usage.py"
msgfmt -o "$workdir/locale/zh_CN/LC_MESSAGES/agent-usage@fedora-enhance.mo" "$root/po/zh_CN.po"
# The zip root is the extension itself. A nested UUID directory is rejected.
(
  cd "$workdir"
  zip -qr "$root/dist/agent-usage@fedora-enhance.shell-extension.zip" \
    metadata.json extension.js stylesheet.css LICENSE locale collector
)
echo "$root/dist/agent-usage@fedora-enhance.shell-extension.zip"
