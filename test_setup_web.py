import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from http.server import HTTPServer

from setup_web import SetupHandler


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.server = HTTPServer(('127.0.0.1', 0), SetupHandler)
        self.server.secret_path, self.server.csrf, self.server.used = '/secret', 'csrf', False
        self.server.setup_lock = threading.Lock()
        self.server.config_path = Path(self.directory.name) / 'config.json'
        self.server.config_options = dict(codex='codex', state_dir='state', reset_dir='reset')
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def test_unknown_path_and_csrf_are_rejected(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(self.url + '/wrong')
        self.assertEqual(error.exception.code, 404)
        error.exception.close()
        request = Request(self.url + '/secret', data=urlencode({'csrf': 'bad', 'token': 'bad'}).encode())
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        self.assertFalse(self.server.config_path.exists())

    def test_token_saved_but_never_reflected(self):
        token = '123456789:' + 'x' * 35
        with patch('setup_web.Telegram.call', return_value={'is_bot': True, 'username': 'example_bot'}):
            request = Request(self.url + '/secret', data=urlencode({'csrf': 'csrf', 'token': token}).encode())
            with urlopen(request) as response:
                page = response.read().decode()
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertNotIn(token, page)
        self.assertIn('https://t.me/example_bot?start=', page)
        self.assertEqual(json.loads(self.server.config_path.read_text())['bot_token'], token)
        self.assertTrue(self.server.used)
