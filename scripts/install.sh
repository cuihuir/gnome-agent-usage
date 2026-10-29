#!/usr/bin/env bash
# Purpose: install the Agent Usage extension for this user.
# Dependency: GNOME Shell 50, python3, zip, gsettings.
# Effect: copies the extension and adds it to enabled-extensions.
#         Shell 50 scans extension directories only at startup, so a new
#         copy stays invisible until the next login. Does not change system files.
# Rollback: scripts/uninstall.sh
set -euo pipefail

uuid=agent-usage@fedora-enhance
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
version=$(gnome-shell --version | awk '{print $3}')
case "$version" in
  50.*) ;;
  *)
    echo "需要 GNOME Shell 50，当前是 ${version}" >&2
    exit 1
    ;;
esac

workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT
mkdir -p "$workdir/collector" "$workdir/locale/zh_CN/LC_MESSAGES"
cp "$root/metadata.json" "$root/extension.js" "$root/stylesheet.css" "$root/LICENSE" "$workdir/"
cp "$root/collector/agent_usage.py" "$workdir/collector/"
chmod 755 "$workdir/collector/agent_usage.py"
msgfmt -o "$workdir/locale/zh_CN/LC_MESSAGES/agent-usage@fedora-enhance.mo" "$root/po/zh_CN.po"
# gnome-extensions reads metadata.json at the root of the zip.
( cd "$workdir" && zip -qr extension.zip metadata.json extension.js stylesheet.css LICENSE locale collector )

gnome-extensions install --force "$workdir/extension.zip"

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


disabled = [item for item in read("disabled-extensions") if item != uuid]
if len(disabled) != len(read("disabled-extensions")):
    write("disabled-extensions", disabled)
enabled = read("enabled-extensions")
if uuid not in enabled:
    enabled.append(uuid)
    write("enabled-extensions", enabled)
PY

if gnome-extensions info "$uuid" 2>/dev/null | grep -q 'State:'; then
  echo "智能体用量已安装。这次登录仍用启动时载入的面板。"
  echo "注销并重新登录后，顶栏会换成新的一列布局。"
else
  echo "智能体用量已安装，并写入了下次登录的启用列表。"
  echo "GNOME Shell 50 只在启动时扫描扩展目录，这次登录的顶栏还不会出现它。"
  echo "注销并重新登录后，顶栏右侧会出现用量文字，点击可打开面板。"
fi
