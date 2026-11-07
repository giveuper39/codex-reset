"""One-use HTTPS setup form for an owner who is using a phone."""
import argparse
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import threading
from urllib.parse import parse_qs

from codex_reset import save
from telegram_api import Telegram, TelegramError


PAGE = """<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codex Reset · Настройка бота</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#101815;color:#ecf2ed;font:16px/1.6 system-ui,sans-serif;min-height:100vh;display:grid;place-items:center;padding:24px}
main{max-width:480px;width:100%;padding:32px;border:1px solid #33453a;border-radius:24px;background:#17221b}
.tag{color:#9dd7ab;font-size:12px;letter-spacing:.12em;text-transform:uppercase}h1{font-size:28px;line-height:1.2;margin:16px 0}p{color:#becdc1}
input,button{width:100%;padding:15px;border-radius:12px;font:inherit}input{background:#101815;color:#fff;border:1px solid #617867;margin:10px 0 16px}button{border:0;background:#afe5bb;color:#122016;font-weight:700;cursor:pointer}
a{color:#afe5bb}.small{font-size:13px}label{display:block;font-weight:600}.error{color:#ffb7a8}
</style><main><div class="tag">Codex Reset · Private setup</div>
<h1>Подключить Telegram-бота</h1><p>Вставь токен, полученный у BotFather. Он сохранится на этом сервере с закрытым доступом.</p>
{error}<form method="post" autocomplete="off"><input type="hidden" name="csrf" value="{csrf}">
<label for="token">Токен бота</label><input id="token" name="token" type="password" autocomplete="off" required maxlength="180" placeholder="123456789:…">
<button type="submit">Сохранить токен</button></form>
<p class="small">Ссылка одноразовая. После успешной настройки эта страница выключится.</p></main></html>"""


def create_config(token, codex, state_dir, reset_dir):
    profile = Telegram(token).call("getMe")
    username = profile.get("username")
    if not profile.get("is_bot") or not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_]+", username):
        raise ValueError("Unexpected bot profile")
    return {"bot_token": token, "bot_username": username,
            "pairing_secret": secrets.token_urlsafe(24), "owner_id": None,
            "codex": codex, "state_dir": state_dir, "reset_dir": reset_dir,
            "poll_seconds": 60, "before_minutes": 15}


class SetupHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(8)
        if hasattr(self.server, 'tls_context'):
            self.request = self.server.tls_context.wrap_socket(self.request, server_side=True)
        super().setup()

    def log_message(self, *args):
        pass  # POST bodies and one-time URLs must not reach access logs.

    def respond(self, code, page):
        body = page.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def allowed(self):
        return secrets.compare_digest(self.path, self.server.secret_path) and not self.server.used

    def do_GET(self):
        if not self.allowed():
            return self.respond(404, "Not found")
        self.respond(200, PAGE.replace("{error}", "").replace("{csrf}", self.server.csrf))

    def do_POST(self):
        with self.server.setup_lock:
            self.accept_token()

    def accept_token(self):
        if not self.allowed():
            return self.respond(404, "Not found")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("Invalid body size")
            values = parse_qs(self.rfile.read(length).decode("utf-8"), strict_parsing=True)
            if not secrets.compare_digest(values.get("csrf", [""])[0], self.server.csrf):
                return self.respond(403, "Forbidden")
            token = values.get("token", [""])[0].strip()
            if not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,150}", token):
                raise ValueError("Invalid token format")
            config = create_config(token, **self.server.config_options)
            self.server.config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.server.config_path.exists():
                return self.respond(409, "Already configured")
            save(self.server.config_path, config)
            os.chmod(self.server.config_path, 0o600)
            self.server.used = True
            link = f"https://t.me/{config['bot_username']}?start={config['pairing_secret']}"
            page = ("<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'>"
                    "<body style='background:#101815;color:#ecf2ed;font:18px/1.6 system-ui;padding:32px;max-width:560px;margin:auto'>"
                    "<h1>Токен сохранён ✓</h1><p>Следующий шаг — привязать себя как владельца бота. "
                    "Нажми ссылку и затем Start в Telegram. Если бот ещё запускается, сообщение дождётся его.</p>"
                    f"<a style='color:#afe5bb' href='{escape(link)}'>Открыть @{escape(config['bot_username'])}</a>"
                    "<p>Одноразовая страница настройки закрывается.</p></body>")
            self.respond(200, page)
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        except (ValueError, UnicodeError, TelegramError):
            error = '<p class="error">Не удалось проверить токен. Проверь его и попробуй ещё раз.</p>'
            self.respond(400, PAGE.replace("{error}", error).replace("{csrf}", self.server.csrf))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--port", type=int, default=9443)
    parser.add_argument("--codex", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--reset-dir", required=True)
    parser.add_argument("--uid", type=int)
    parser.add_argument("--gid", type=int)
    args = parser.parse_args()
    if args.config.exists():
        raise SystemExit("Already configured; refusing to overwrite credentials")
    os.umask(0o077)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(args.cert, args.key)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), SetupHandler)
    server.tls_context = context
    server.setup_lock = threading.Lock()
    server.secret_path, server.csrf, server.used = args.path, secrets.token_urlsafe(32), False
    server.config_path = args.config
    server.config_options = {"codex": args.codex, "state_dir": args.state_dir, "reset_dir": args.reset_dir}
    if args.gid is not None:
        os.setgroups([])
        os.setgid(args.gid)
    if args.uid is not None:
        os.setuid(args.uid)
    timer = threading.Timer(1200, server.shutdown)
    timer.daemon = True
    timer.start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        timer.cancel()
        server.server_close()


if __name__ == "__main__":
    main()
