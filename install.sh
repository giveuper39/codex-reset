#!/usr/bin/env bash
set -euo pipefail

if [[ ${1:-} == --help ]]; then
  printf '%s\n' 'Usage: sudo bash install.sh' \
    'Installs Codex Reset; prompts for the Telegram bot token without echo.'
  exit 0
fi
[[ $EUID -eq 0 ]] || { printf '%s\n' 'Run this installer with sudo.' >&2; exit 1; }
[[ $# -eq 0 ]] || { printf '%s\n' 'Unknown argument.' >&2; exit 1; }
for command in python3 curl systemctl runuser useradd install; do
  command -v "$command" >/dev/null || { printf 'Missing command: %s\n' "$command" >&2; exit 1; }
done
python3 -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ is required"'
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
service_home=/var/lib/codex-reset
if ! id codex-reset >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$service_home" --shell /usr/sbin/nologin codex-reset
fi
[[ $(getent passwd codex-reset | cut -d: -f6) == "$service_home" && $(id -u codex-reset) != 0 ]] || {
  printf '%s\n' 'Existing codex-reset user has unexpected settings; aborting.' >&2; exit 1;
}
install -d -m 755 /opt/codex-reset
install -m 644 "$source_dir"/codex_reset.py "$source_dir"/codex_bot.py \
  "$source_dir"/telegram_api.py "$source_dir"/setup_web.py "$source_dir"/configure.py /opt/codex-reset/
install -m 644 "$source_dir"/systemd/codex-reset-bot.service /etc/systemd/system/
install -d -o codex-reset -g codex-reset -m 700 "$service_home/.config/codex-reset-bot"
cd /opt/codex-reset
if [[ ! -x $service_home/.local/bin/codex ]]; then
  runuser -u codex-reset -- env HOME="$service_home" CODEX_NON_INTERACTIVE=1 \
    bash -o pipefail -c 'curl -fsSL https://chatgpt.com/codex/install.sh | sh'
fi
runuser -u codex-reset -- env HOME="$service_home" python3 -c \
  'import codex_reset; codex_reset.check_protocol("/var/lib/codex-reset/.local/bin/codex")'
config="$service_home/.config/codex-reset-bot/config.json"
if [[ ! -f $config ]]; then
  runuser -u codex-reset -- env HOME="$service_home" python3 configure.py \
    --config "$config" --codex "$service_home/.local/bin/codex"
fi
runuser -u codex-reset -- env HOME="$service_home" python3 - "$config" <<'PY'
import json, sys
from pathlib import Path
from telegram_api import Telegram
c = json.loads(Path(sys.argv[1]).read_text())
t = Telegram(c['bot_token'])
assert t.call('getMe').get('username') == c['bot_username'], 'Unexpected bot identity'
assert not t.call('getWebhookInfo').get('url'), 'This bot has an active webhook'
PY
systemctl daemon-reload
systemctl enable --now codex-reset-bot.service
sleep 2
systemctl is-active --quiet codex-reset-bot.service || { printf '%s\n' 'Bot startup failed. Check journalctl -u codex-reset-bot.' >&2; exit 1; }
printf '%s\n' 'Bot installed. Use the private owner link printed during configuration.' \
  'New installations start paused; approve exact credit IDs and enable auto-reset from Telegram.'
runuser -u codex-reset -- env HOME="$service_home" python3 configure.py --config "$config" --show-owner-link
