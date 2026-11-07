"""Small Telegram Bot API client; never include token-bearing URLs in errors."""
import json
from http.client import HTTPSConnection
import socket
from urllib.error import HTTPError, URLError
from urllib.request import Request, HTTPSHandler, build_opener


def ipv4_first_connection(address, timeout=15, source_address=None):
    # An advertised but unreachable IPv6 route caused a 15s stall on EVERY request.
    addresses = socket.getaddrinfo(*address, type=socket.SOCK_STREAM)
    addresses.sort(key=lambda item: item[0] != socket.AF_INET)
    error = OSError('No Telegram address resolved')
    for family, kind, protocol, _, target in addresses:
        connection = socket.socket(family, kind, protocol)
        try:
            connection.settimeout(min(timeout, 5))
            if source_address:
                connection.bind(source_address)
            connection.connect(target)
            connection.settimeout(timeout)
            return connection
        except OSError as exc:
            error = exc
            connection.close()
    raise error


class TelegramConnection(HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = ipv4_first_connection


class TelegramHTTPSHandler(HTTPSHandler):
    def https_open(self, request):
        return self.do_open(TelegramConnection, request, context=self._context)


urlopen = build_opener(TelegramHTTPSHandler()).open


class TelegramError(RuntimeError):
    def __init__(self, method, code=0, retry_after=5):
        super().__init__(f"Telegram {method}: error {code}")
        self.code = code
        self.retry_after = max(1, min(300, retry_after))


class Telegram:
    def __init__(self, token):
        self.token = token

    def call(self, method, **params):
        request = Request(f"https://api.telegram.org/bot{self.token}/{method}",
                          data=json.dumps(params).encode(),
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=int(params.get("timeout", 0)) + 15) as response:
                result = json.load(response)
        except HTTPError as exc:
            delay = 5
            try:
                data = json.load(exc)
                delay = int(data.get("parameters", {}).get("retry_after", 5))
            except (ValueError, TypeError):
                pass
            raise TelegramError(method, exc.code, delay) from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise TelegramError(method) from None
        if not isinstance(result, dict) or not result.get("ok"):
            raise TelegramError(method, result.get("error_code", 0) if isinstance(result, dict) else 0)
        return result["result"]

    def send(self, chat_id, text, keyboard=None):
        params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "link_preview_options": {"is_disabled": True}}
        if keyboard is not None:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        return self.call("sendMessage", **params)
