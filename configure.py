"""Interactive local/server setup. The bot token is read without echo."""
import argparse
from getpass import getpass
import os
from pathlib import Path
import json

from codex_reset import save, resolve_codex
from setup_web import create_config
from telegram_api import TelegramError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path.home() / '.config/codex-reset-bot/config.json')
    parser.add_argument('--codex', default='codex')
    parser.add_argument('--state-dir', type=Path, default=Path.home() / '.local/state/codex-reset-bot')
    parser.add_argument('--reset-dir', type=Path, default=Path.home() / '.local/state/codex-reset-bot/redemptions')
    parser.add_argument('--show-owner-link', action='store_true')
    args = parser.parse_args()
    if args.show_owner_link:
        config = json.loads(args.config.read_text('utf-8'))
        print(f"https://t.me/{config['bot_username']}?start={config['pairing_secret']}")
        return
    if args.config.exists():
        raise SystemExit('Configuration already exists; refusing to overwrite the bot token')
    executable = resolve_codex(args.codex)
    if not executable:
        raise SystemExit('Install Codex CLI first or pass --codex /absolute/path/to/codex')
    os.umask(0o077)
    token = getpass('Telegram bot token (hidden): ').strip()
    try:
        config = create_config(token, executable, str(args.state_dir), str(args.reset_dir))
    except (TelegramError, ValueError):
        raise SystemExit('Could not verify this bot token. Nothing was saved.') from None
    args.config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    save(args.config, config)
    os.chmod(args.config, 0o600)
    print('Configuration saved. Start codex_bot.py, then open this one-time owner link:')
    print(f"https://t.me/{config['bot_username']}?start={config['pairing_secret']}")


if __name__ == '__main__':
    main()
