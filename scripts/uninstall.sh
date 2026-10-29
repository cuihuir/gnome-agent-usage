#!/usr/bin/env bash
# Purpose: remove the Agent Usage GNOME extension for this user.
# Dependency: gnome-extensions.
# Effect: disables and deletes the user extension. Leaves the usage cache in place.
# Rollback: scripts/install.sh
set -euo pipefail

uuid=agent-usage@fedora-enhance
gnome-extensions disable "$uuid" 2>/dev/null || true

python3 - "$uuid" <<'PY'
import ast
import subprocess
import sys

uuid = sys.argv[1]


def read(key):
    raw = subprocess.check_output(
        ["gsettings", "get", "org.gnome.shell", key], text=True
    ).strip()
    value = ast.literal_eval(raw)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SystemExit(f"无法解析 org.gnome.shell {key}")
    return value


def write(key, items):
    formatted = "[" + ", ".join(repr(item) for item in items) + "]"
    subprocess.check_call(["gsettings", "set", "org.gnome.shell", key, formatted])


for key in ("enabled-extensions", "disabled-extensions"):
    items = [item for item in read(key) if item != uuid]
    if len(items) != len(read(key)):
        write(key, items)
PY

gnome-extensions uninstall "$uuid" 2>/dev/null || true
rm -rf "${HOME}/.local/share/gnome-shell/extensions/${uuid}"
echo "已移除智能体用量。用量缓存仍在 ~/.local/state/gnome-agent-usage 和 ~/.cache/gnome-agent-usage。"
