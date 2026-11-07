"""Self-hosted, single-owner Telegram control for Codex banked resets."""
import argparse
from datetime import datetime, timezone
from html import escape
import hashlib
import json
import os
from pathlib import Path
import queue
import secrets
import signal
import time

import codex_reset as core
from telegram_api import Telegram, TelegramError


class UserError(RuntimeError):
    pass


def stamp(value):
    return (datetime.fromtimestamp(value, timezone.utc).strftime('%d.%m.%Y %H:%M UTC')
            if type(value) in (int, float) else 'без срока')


def credit_key(credit_id):
    return hashlib.sha256(credit_id.encode()).hexdigest()[:16]


def main_bucket(snapshot):
    buckets = snapshot.get('rateLimitsByLimitId')
    return (buckets.get('codex') or {}) if isinstance(buckets, dict) else snapshot.get('rateLimits', {})


def windows(snapshot):
    result = {}
    for key in ('primary', 'secondary'):
        value = main_bucket(snapshot).get(key)
        if isinstance(value, dict) and value.get('windowDurationMins') in (300, 10080):
            result[str(value['windowDurationMins'])] = value
    return result


def snapshot_baseline(account, snapshot):
    rows = core.available_credits(snapshot)
    return {'identity': account.get('email'),
            'credits': {row['id']: row['expiresAt'] for row in rows},
            'windows': windows(snapshot)}


def events_between(previous, current, now):
    if not previous or previous.get('identity') != current.get('identity'):
        return []
    events = []
    for credit_id, expiry in current['credits'].items():
        if credit_id not in previous['credits']:
            events.append(('credit_added', credit_id, expiry))
    for credit_id, expiry in previous['credits'].items():
        if credit_id not in current['credits']:
            kind = 'credit_expired' if expiry is not None and expiry <= now else 'credit_removed'
            events.append((kind, credit_id, expiry))
    for key, new in current['windows'].items():
        old = previous['windows'].get(key)
        if not old:
            continue
        old_reset, new_reset = old.get('resetsAt'), new.get('resetsAt')
        old_used, new_used = old.get('usedPercent'), new.get('usedPercent')
        if type(old_used) is not int or type(new_used) is not int:
            continue
        if (type(old_reset) is int and type(new_reset) is int
                and old_reset <= now and new_reset > old_reset):
            events.append(('window_renewed', key, new_reset))
        elif new_used < old_used:
            events.append(('usage_decreased', key, new_used))
    return events


class Backend:
    def __init__(self, executable, reset_dir):
        self.executable = executable
        self.reset_dir = Path(reset_dir)
        self.server = None
        self.login_id = None

    def client(self):
        if self.server is None:
            self.server = core.AppServer(self.executable).__enter__()
        return self.server

    def close(self):
        if self.server is not None:
            self.server.__exit__(None, None, None)
            self.server = None
        self.login_id = None

    def snapshot(self):
        account = self.client().call('account/read').get('account')
        if not isinstance(account, dict) or account.get('type') != 'chatgpt':
            raise UserError('Сначала подключи аккаунт Codex кнопкой «Подключить Codex».')
        return account, self.client().call(core.READ)

    def start_login(self):
        if self.login_id:
            self.client().call('account/login/cancel', {'loginId': self.login_id})
        result = self.client().call('account/login/start', {'type': 'chatgptDeviceCode'})
        if (result.get('type') != 'chatgptDeviceCode'
                or result.get('verificationUrl') != 'https://auth.openai.com/codex/device'
                or not isinstance(result.get('userCode'), str) or not result.get('loginId')):
            raise UserError('CLI вернул неожиданный способ входа. Обнови Codex и попробуй снова.')
        self.login_id = result['loginId']
        return result

    def login_result(self):
        if self.server is None:
            return None
        while True:
            try:
                message = self.server.notifications.get_nowait()
            except queue.Empty:
                return None
            params = message.get('params', {})
            if message.get('method') == 'account/login/completed' and params.get('loginId') == self.login_id:
                self.login_id = None
                return bool(params.get('success'))

    def apply(self, credit_id):
        core.check_protocol(self.executable)
        with core.locked(self.reset_dir):
            path = self.reset_dir / (hashlib.sha256(credit_id.encode()).hexdigest() + '.json')
            return core.apply_reset(self.client(), credit_id, path, self.before_minutes)

    def redemption_status(self, credit_id):
        path = self.reset_dir / (hashlib.sha256(credit_id.encode()).hexdigest() + '.json')
        if not path.exists():
            return None
        state = json.loads(path.read_text('utf-8'))
        if state.get('creditId') == credit_id and state.get('outcome') in ('reset', 'alreadyRedeemed'):
            return 'verified' if state.get('verified') else 'confirmed'
        return None


class Bot:
    def __init__(self, config, telegram, backend):
        self.config, self.telegram, self.backend = config, telegram, backend
        self.directory = Path(config['state_dir'])
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / 'bot.json'
        self.state = json.loads(self.path.read_text('utf-8')) if self.path.exists() else {}
        defaults = {'owner_id': config.get('owner_id'), 'offset': 0, 'auto_enabled': False,
                    'notifications': True, 'approved': [], 'outbox': [], 'baseline': None,
                    'warned': [], 'last_error': None, 'confirmation': None}
        for key, value in defaults.items():
            self.state.setdefault(key, value)
        self.backend.before_minutes = config.get('before_minutes', 15)
        self.login_deadline = None
        self.next_send_at = 0
        self.save()

    def save(self):
        core.save(self.path, self.state)

    def enqueue(self, text, keyboard=None, optional=False):
        if not self.state['owner_id'] or (optional and not self.state['notifications']):
            return
        if len(self.state['outbox']) >= 500:
            raise RuntimeError('Notification queue is full')
        item = {'text': text, 'keyboard': keyboard, 'optional': optional}
        if optional:
            self.state['outbox'].append(item)
        else:
            index = next((i for i, value in enumerate(self.state['outbox']) if value.get('optional')), len(self.state['outbox']))
            self.state['outbox'].insert(index, item)

    def flush_one(self):
        if self.state['owner_id'] and self.state['outbox'] and time.monotonic() >= self.next_send_at:
            item = self.state['outbox'][0]
            started = time.monotonic()
            self.telegram.send(self.state['owner_id'], item['text'], item.get('keyboard'))
            print(f'Telegram reply API: {time.monotonic() - started:.2f}s', flush=True)
            self.next_send_at = time.monotonic() + 1
            self.state['outbox'].pop(0)
            self.save()

    def keyboard(self):
        enabled = self.state['auto_enabled']
        return [[{'text': '📊 Статус', 'callback_data': 'status'},
                 {'text': '🎁 Banked resets', 'callback_data': 'credits'}],
                [{'text': '⏸ Приостановить авторесет' if enabled else '▶️ Включить авторесет',
                  'callback_data': 'auto:off' if enabled else 'auto:offer'}],
                [{'text': '🔕 Выключить уведомления' if self.state['notifications'] else '🔔 Включить уведомления',
                  'callback_data': 'notify:off' if self.state['notifications'] else 'notify:on'}],
                [{'text': '🔗 Подключить Codex', 'callback_data': 'connect'}]]

    def status(self):
        lines = ['<b>Codex Reset</b>',
                 'Авторесет: ' + ('<b>включён</b>' if self.state['auto_enabled'] else 'приостановлен'),
                 'Уведомления: ' + ('включены' if self.state['notifications'] else 'выключены')]
        if self.login_deadline:
            lines.append('\nОжидается подтверждение входа в браузере.')
            return '\n'.join(lines)
        try:
            account, snapshot = self.backend.snapshot()
        except UserError as exc:
            lines.append('\n' + str(exc))
            return '\n'.join(lines)
        lines.extend(['', 'Аккаунт: ' + escape(account.get('email') or 'ChatGPT')])
        for duration, value in windows(snapshot).items():
            used = value.get('usedPercent')
            remaining = max(0, min(100, 100 - used)) if type(used) is int else '?'
            lines.append(('Неделя' if duration == '10080' else '5 часов') + f': осталось <b>{remaining}%</b>')
            lines.append('Обновление: ' + stamp(value.get('resetsAt')))
        try:
            rows = core.available_credits(snapshot)
            selected = core.earliest(snapshot, time.time())
            lines.append(f'\nBanked resets: <b>{len(rows)}</b> · разрешено: {len(self.state["approved"])}')
            if selected:
                lines.extend(['Ближайшее сгорание: ' + stamp(selected['expiresAt']),
                              '<code>' + escape(selected['id']) + '</code>'])
        except ValueError:
            lines.append('\nПолный список banked reset недоступен. Автоприменение заблокировано.')
        return '\n'.join(lines)

    def show_credits(self):
        _, snapshot = self.backend.snapshot()
        rows = sorted(core.available_credits(snapshot), key=lambda row: (
            row['expiresAt'] is None, row['expiresAt'] or 0, row['id']))
        if not rows:
            self.enqueue('Доступных banked reset пока нет.', self.keyboard())
        for row in rows:
            approved = row['id'] in self.state['approved']
            action = 'deny:' if approved else 'allow:'
            button = 'Запретить автоприменение' if approved else 'Разрешить автоприменение'
            self.enqueue('<b>Banked reset</b>\n<code>' + escape(row['id']) + '</code>\nСгорание: '
                         + stamp(row['expiresAt']) + '\nРазрешён: ' + ('да' if approved else 'нет'),
                         [[{'text': button, 'callback_data': action + credit_key(row['id'])}]])

    def change_credit(self, action, short_id):
        _, snapshot = self.backend.snapshot()
        rows = core.available_credits(snapshot)
        matches = [row for row in rows if credit_key(row['id']) == short_id]
        if len(matches) != 1:
            raise UserError('Ресет уже недоступен или список изменился. Открой Banked resets заново.')
        row = matches[0]
        if row['expiresAt'] is None or row['expiresAt'] <= time.time():
            raise UserError('У этого ресета нет действующего срока сгорания.')
        approved = set(self.state['approved'])
        if action == 'allow':
            approved.add(row['id'])
        else:
            approved.discard(row['id'])
        self.state['approved'] = sorted(approved)
        self.state['confirmation'] = None
        self.save()
        self.enqueue(('Разрешено' if action == 'allow' else 'Запрещено') + ' автоприменение:\n<code>'
                     + escape(row['id']) + '</code>\nСгорание: ' + stamp(row['expiresAt']), self.keyboard())

    def offer_auto(self):
        account, snapshot = self.backend.snapshot()
        if not account.get('email'):
            raise UserError('Codex не подтвердил идентичность аккаунта. Автоприменение заблокировано.')
        live = {row['id'] for row in core.available_credits(snapshot)
                if row['expiresAt'] is not None and row['expiresAt'] > time.time()}
        approved = sorted(live.intersection(self.state['approved']))
        if not approved:
            raise UserError('Сначала открой Banked resets и разреши нужный ресет по его ID.')
        nonce = secrets.token_urlsafe(12)
        self.state['confirmation'] = {'nonce': nonce, 'until': time.time() + 300,
                                      'approved': approved, 'identity': account['email']}
        self.save()
        ids = '\n'.join('<code>' + escape(value) + '</code>' for value in approved)
        self.enqueue('<b>Включить авторесет?</b>\n\n' + ids + '\n\nТолько ближайший к сгоранию ID, '
                     f'в последние {self.config.get("before_minutes", 15)} минут и при остатке лимита ≤10%.',
                     [[{'text': 'Подтверждаю', 'callback_data': 'auto:on:' + nonce},
                       {'text': 'Отмена', 'callback_data': 'auto:off'}]])

    def confirm_auto(self, nonce):
        confirmation = self.state.get('confirmation')
        if (not confirmation or confirmation['until'] < time.time()
                or not secrets.compare_digest(confirmation['nonce'], nonce)):
            raise UserError('Подтверждение устарело. Нажми «Включить авторесет» заново.')
        account, snapshot = self.backend.snapshot()
        live = {row['id'] for row in core.available_credits(snapshot)
                if row['expiresAt'] is not None and row['expiresAt'] > time.time()}
        if (account.get('email') != confirmation['identity']
                or set(confirmation['approved']) != set(self.state['approved']).intersection(live)):
            raise UserError('Аккаунт, список разрешений или таймеры изменились. Подтверди заново.')
        self.state['auto_enabled'] = True
        self.state['confirmation'] = None
        self.save()
        self.enqueue('Авторесет включён для подтверждённых ID.', self.keyboard())

    def command(self, action):
        if action == 'status':
            self.enqueue(self.status(), self.keyboard())
        elif action == 'credits':
            self.show_credits()
        elif action.startswith(('allow:', 'deny:')):
            self.change_credit(*action.split(':', 1))
        elif action == 'auto:offer':
            self.offer_auto()
        elif action.startswith('auto:on:'):
            self.confirm_auto(action.removeprefix('auto:on:'))
        elif action == 'auto:off':
            self.state['auto_enabled'] = False
            self.state['confirmation'] = None
            self.save()
            self.enqueue('Авторесет приостановлен. Мониторинг продолжает работать.', self.keyboard())
        elif action in ('notify:on', 'notify:off'):
            self.state['notifications'] = action == 'notify:on'
            if not self.state['notifications']:
                self.state['outbox'] = [item for item in self.state['outbox'] if not item.get('optional')]
            self.save()
            self.enqueue('Уведомления ' + ('включены.' if self.state['notifications'] else 'выключены.'), self.keyboard())
        elif action == 'connect':
            self.state.update(auto_enabled=False, approved=[], baseline=None, confirmation=None)
            self.save()
            result = self.backend.start_login()
            self.login_deadline = time.time() + 900
            self.enqueue('<b>Подключение Codex</b>\n\n1. Открой официальную страницу:\n'
                         + escape(result['verificationUrl']) + '\n2. Войди в нужный аккаунт и введи код:\n<code>'
                         + escape(result['userCode']) + '</code>\n\nБот сообщит, когда вход завершится. '
                         'Пароль и токены Codex сюда отправлять не нужно.')
        else:
            self.enqueue('Используй кнопки ниже или /status, /resets, /connect.', self.keyboard())

    def handle(self, update):
        callback = update.get('callback_query')
        message = callback.get('message', {}) if isinstance(callback, dict) else update.get('message', {})
        sender = callback.get('from', {}) if isinstance(callback, dict) else message.get('from', {})
        chat = message.get('chat', {})
        user_id = sender.get('id')
        if type(user_id) is not int or chat.get('type') != 'private' or chat.get('id') != user_id:
            return
        text = message.get('text', '') if not callback else ''
        if not self.state['owner_id']:
            parts = text.split()
            provided = parts[1] if len(parts) == 2 and parts[0] == '/start' else ''
            expected = self.config.get('pairing_secret', '')
            if not expected or not secrets.compare_digest(expected, provided):
                return
            self.state['owner_id'] = user_id
            self.save()
            self.enqueue('Владелец привязан ✓\nУправление доступно только тебе. Все даты показаны в UTC.')
        if user_id != self.state['owner_id']:
            return
        if callback:
            try:
                self.telegram.call('answerCallbackQuery', callback_query_id=callback['id'])
            except TelegramError as exc:
                if exc.code != 400:  # Expired callback acknowledgments must not block the update queue.
                    raise
            action = callback.get('data', '')
        else:
            command = text.split()[0].split('@')[0] if text.strip() else ''
            action = {'/start': 'status', '/status': 'status', '/resets': 'credits',
                      '/connect': 'connect', '/pause': 'auto:off'}.get(command, 'help')
        try:
            self.command(action)
        except (UserError, ValueError) as exc:
            self.enqueue(escape(str(exc)), self.keyboard())
        self.save()

    def check_login(self):
        result = self.backend.login_result()
        if result is not None:
            self.login_deadline = None
            self.state['baseline'] = None
            self.enqueue('Codex подключён ✓' if result else 'Вход не завершён. Попробуй подключить Codex снова.', self.keyboard())
            self.save()
        elif self.login_deadline and time.time() > self.login_deadline:
            self.backend.close()
            self.login_deadline = None
            self.enqueue('Время на вход истекло. Нажми «Подключить Codex» снова.', self.keyboard())
            self.save()

    def monitor(self):
        if self.login_deadline:
            return
        now = time.time()
        account, snapshot = self.backend.snapshot()
        current = snapshot_baseline(account, snapshot)
        previous = self.state['baseline']
        if previous and previous.get('identity') != current['identity']:
            self.state.update(auto_enabled=False, approved=[], confirmation=None)
            self.enqueue('Аккаунт Codex изменился. Авторесет приостановлен; разрешения сброшены.')
            previous = None
        for kind, value, extra in events_between(previous, current, now):
            if kind == 'credit_added':
                self.enqueue('🎁 <b>Пришёл новый banked reset</b>\n<code>' + escape(value)
                             + '</code>\nСгорание: ' + stamp(extra),
                             [[{'text': 'Разрешить автоприменение', 'callback_data': 'allow:' + credit_key(value)}]], optional=True)
            elif kind in ('credit_expired', 'credit_removed'):
                redemption = self.backend.redemption_status(value)
                if redemption == 'verified':
                    text = '✅ Сервис подтвердил применение banked reset; постпроверка выполнена'
                elif redemption == 'confirmed':
                    text = '⚠️ Сервис подтвердил применение banked reset, но постпроверка не завершена. Авторесет приостановлен; нужна ручная проверка'
                    self.state['auto_enabled'] = False
                else:
                    text = 'Истёк срок banked reset' if kind == 'credit_expired' else 'Banked reset больше недоступен; причина API не указана'
                self.enqueue(text + '\n<code>' + escape(value) + '</code>', optional=redemption not in ('verified', 'confirmed'))
                self.state['approved'] = [item for item in self.state['approved'] if item != value]
            elif kind == 'window_renewed':
                self.enqueue('🔄 Обновилось ' + ('недельное' if value == '10080' else 'пятичасовое')
                             + ' окно лимита. Следующее обновление: ' + stamp(extra), optional=True)
            elif kind == 'usage_decreased':
                self.enqueue('🔄 Расход ' + ('недельного' if value == '10080' else 'пятичасового')
                             + f' лимита уменьшился до {extra}%. Причину изменения API не сообщает.', optional=True)
        for credit_id, expiry in current['credits'].items():
            if expiry is not None and 0 < expiry - now <= 3600 and credit_id not in self.state['warned']:
                self.state['warned'].append(credit_id)
                self.enqueue('⏳ Banked reset сгорает в течение часа\n<code>' + escape(credit_id)
                             + '</code>\nСгорание: ' + stamp(expiry), optional=True)
        self.state['baseline'] = current
        self.state['warned'] = [key for key in self.state['warned'] if key in current['credits']]
        if self.state['last_error']:
            self.enqueue('Связь с Codex восстановлена.', optional=True)
            self.state['last_error'] = None
        self.save()  # Baseline and pending notifications advance together.
        if not self.state['auto_enabled']:
            return
        if not current['identity']:
            self.state['auto_enabled'] = False
            self.enqueue('Авторесет приостановлен: не подтверждён аккаунт.')
            self.save()
            return
        selected = core.earliest(snapshot, now)
        if (selected and selected['id'] in self.state['approved'] and core.eligible(snapshot, now)
                and selected['expiresAt'] - now <= self.config.get('before_minutes', 15) * 60):
            outcome = self.backend.apply(selected['id'])
            if outcome in ('reset', 'alreadyRedeemed', 'alreadyCompleted'):
                self.state['approved'] = [key for key in self.state['approved'] if key != selected['id']]
                account, after = self.backend.snapshot()
                self.state['baseline'] = snapshot_baseline(account, after)
                self.enqueue('✅ Сервис подтвердил применение выбранного reset\n<code>'
                             + escape(selected['id']) + '</code>\nПроверка остальных ID выполнена.')
                self.save()

    def monitor_error(self, exc):
        label = type(exc).__name__
        if isinstance(exc, UserError):
            label = 'not_connected'
        if self.state['last_error'] != label:
            self.state['last_error'] = label
            if label != 'not_connected':
                self.enqueue('⚠️ Не удалось проверить Codex. Новое применение не выполняется; '
                             'бот повторит проверку. При необходимости подключи аккаунт заново.')
            self.save()
        self.backend.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path.home() / '.config/codex-reset-bot/config.json')
    args = parser.parse_args()
    os.umask(0o077)
    config = json.loads(args.config.read_text('utf-8'))
    executable = core.resolve_codex(config.get('codex', 'codex'))
    if not executable:
        raise SystemExit('Codex executable not found')
    interval = config.get('poll_seconds', 60)
    if type(interval) is not int or not 15 <= interval <= 3600:
        raise SystemExit('poll_seconds must be 15..3600')
    telegram = Telegram(config['bot_token'])
    profile = telegram.call('getMe')
    if profile.get('username') != config.get('bot_username'):
        raise SystemExit('Bot token belongs to a different bot')
    if telegram.call('getWebhookInfo').get('url'):
        raise SystemExit('This bot has a webhook; remove it before using this independent polling instance')
    backend = Backend(executable, config['reset_dir'])
    stop = False

    def shutdown(*unused):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    with core.locked(Path(config['state_dir']) / 'process'):
        bot = Bot(config, telegram, backend)
        next_check = 0
        try:
            while not stop:
                try:
                    bot.check_login()
                    if time.monotonic() >= next_check:
                        try:
                            bot.monitor()
                        except Exception as exc:
                            bot.monitor_error(exc)
                        next_check = time.monotonic() + interval
                    bot.flush_one()
                    updates = telegram.call('getUpdates', offset=bot.state['offset'],
                                            timeout=1 if bot.state['outbox'] else 10,
                                            allowed_updates=['message', 'callback_query'])
                    for update in updates:
                        if stop:
                            break
                        try:
                            started = time.monotonic()
                            bot.handle(update)
                            print(f'Telegram command handled: {time.monotonic() - started:.2f}s', flush=True)
                        except TelegramError:
                            raise
                        except Exception as exc:
                            bot.monitor_error(exc)
                            bot.enqueue('Операция не завершена. Попробуй ещё раз.', bot.keyboard())
                        bot.state['offset'] = update['update_id'] + 1
                        bot.save()
                        bot.flush_one()  # Reply before beginning another long-poll wait.
                except TelegramError as exc:
                    print(str(exc), flush=True)  # The API client redacts token-bearing URLs.
                    time.sleep(exc.retry_after)
        finally:
            backend.close()


if __name__ == '__main__':
    main()
