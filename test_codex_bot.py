from copy import deepcopy
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

import codex_bot as app
from telegram_api import Telegram, TelegramError, ipv4_first_connection
import socket

NOW = 2000000000
ACCOUNT = {'type': 'chatgpt', 'email': 'owner@example.test'}


def credit(value='first', expiry=NOW + 600):
    return {'id': value, 'expiresAt': expiry, 'status': 'available', 'resetType': 'codexRateLimits'}


def snapshot(rows=None, used=95, resets=NOW + 3600):
    rows = [credit()] if rows is None else rows
    return {'rateLimitResetCredits': {'availableCount': len(rows), 'credits': rows},
            'rateLimitsByLimitId': {'codex': {'primary': {'windowDurationMins': 10080,
                  'usedPercent': used, 'resetsAt': resets}}}}


def message(user=42, text='/status', chat_type='private'):
    return {'update_id': 1, 'message': {'from': {'id': user},
            'chat': {'id': user, 'type': chat_type}, 'text': text}}


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {'state_dir': self.temp.name, 'owner_id': 42,
                       'pairing_secret': 'one-time-pairing-secret', 'before_minutes': 15}
        self.telegram = Mock()
        self.backend = Mock()
        self.backend.snapshot.return_value = (ACCOUNT, snapshot())
        self.backend.login_result.return_value = None
        self.bot = app.Bot(self.config, self.telegram, self.backend)
        timer = patch('codex_bot.time.time', return_value=NOW)
        timer.start()
        self.addCleanup(timer.stop)

    def texts(self):
        return '\n'.join(item['text'] for item in self.bot.state['outbox'])

    def test_other_users_and_groups_cannot_control_bot(self):
        self.bot.handle(message(user=99, text='/connect'))
        self.bot.handle(message(text='/connect', chat_type='group'))
        self.backend.start_login.assert_not_called()
        self.backend.snapshot.assert_not_called()
        self.assertEqual(self.bot.state['outbox'], [])

    def test_pairing_requires_secret_and_binds_once(self):
        self.bot.state['owner_id'] = None
        self.bot.handle(message(user=99, text='/start wrong'))
        self.assertIsNone(self.bot.state['owner_id'])
        self.bot.handle(message(text='/start one-time-pairing-secret'))
        self.assertEqual(self.bot.state['owner_id'], 42)
        self.bot.handle(message(user=99, text='/start one-time-pairing-secret'))
        self.assertEqual(self.bot.state['owner_id'], 42)

    def test_approval_targets_exact_full_credit_id(self):
        self.bot.change_credit('allow', app.credit_key('first'))
        self.assertEqual(self.bot.state['approved'], ['first'])
        with self.assertRaises(app.UserError):
            self.bot.change_credit('allow', app.credit_key('unknown'))

    def test_ambiguous_short_ids_cannot_approve(self):
        self.backend.snapshot.return_value = (ACCOUNT, snapshot([credit('one'), credit('two')]))
        with patch('codex_bot.credit_key', return_value='collision'):
            with self.assertRaises(app.UserError):
                self.bot.change_credit('allow', 'collision')
        self.assertEqual(self.bot.state['approved'], [])

    def test_auto_requires_per_credit_approval_and_confirmation(self):
        with self.assertRaises(app.UserError):
            self.bot.offer_auto()
        self.bot.state['approved'] = ['first']
        self.bot.offer_auto()
        nonce = self.bot.state['confirmation']['nonce']
        self.assertFalse(self.bot.state['auto_enabled'])
        self.bot.confirm_auto(nonce)
        self.assertTrue(self.bot.state['auto_enabled'])
        with self.assertRaises(app.UserError):
            self.bot.confirm_auto(nonce)

    def test_confirmation_fails_if_account_changes(self):
        self.bot.state['approved'] = ['first']
        self.bot.offer_auto()
        nonce = self.bot.state['confirmation']['nonce']
        self.backend.snapshot.return_value = (dict(ACCOUNT, email='another@example.test'), snapshot())
        with self.assertRaises(app.UserError):
            self.bot.confirm_auto(nonce)
        self.assertFalse(self.bot.state['auto_enabled'])

    def test_pause_preserves_monitoring_and_permissions(self):
        self.bot.state.update(auto_enabled=True, approved=['first'])
        self.bot.command('auto:off')
        self.bot.monitor()
        self.assertEqual(self.bot.state['approved'], ['first'])
        self.assertIsNotNone(self.bot.state['baseline'])
        self.backend.apply.assert_not_called()

    def test_responses_flush_immediately_then_respect_per_chat_rate(self):
        self.bot.enqueue('one')
        self.bot.enqueue('two')
        with patch('codex_bot.time.monotonic', return_value=10):
            self.bot.flush_one()
            self.bot.flush_one()
        self.assertEqual(self.telegram.send.call_count, 1)
        with patch('codex_bot.time.monotonic', return_value=11):
            self.bot.flush_one()
        self.assertEqual(self.telegram.send.call_count, 2)
        self.assertEqual(self.bot.state['outbox'], [])

    def test_menu_replies_take_priority_over_passive_notifications(self):
        self.bot.enqueue('passive event', optional=True)
        self.bot.enqueue('menu reply')
        self.assertEqual(self.bot.state['outbox'][0]['text'], 'menu reply')

    def test_new_credit_notification_is_deduplicated_across_restart(self):
        self.bot.monitor()
        self.bot.state['outbox'] = []
        self.backend.snapshot.return_value = (ACCOUNT, snapshot([credit(), credit('new', NOW + 86400)]))
        self.bot.monitor()
        self.assertEqual(self.texts().count('Пришёл новый banked reset'), 1)
        self.bot = app.Bot(self.config, self.telegram, self.backend)
        self.bot.monitor()
        self.assertEqual(self.texts().count('Пришёл новый banked reset'), 1)

    def test_muted_notifications_advance_baseline_without_replay(self):
        self.bot.monitor()
        self.bot.command('notify:off')
        self.backend.snapshot.return_value = (ACCOUNT, snapshot([credit(), credit('new')]))
        self.bot.monitor()
        self.bot.command('notify:on')
        self.bot.monitor()
        self.assertNotIn('Пришёл новый', self.texts())

    def test_auto_will_not_apply_later_approved_credit(self):
        self.bot.state.update(auto_enabled=True, approved=['later'])
        self.backend.snapshot.return_value = (ACCOUNT, snapshot([credit(), credit('later', NOW + 700)]))
        self.bot.monitor()
        self.backend.apply.assert_not_called()

    def test_exact_success_replaces_baseline_and_removes_permission(self):
        self.bot.state.update(auto_enabled=True, approved=['first'])
        self.backend.apply.return_value = 'reset'
        self.backend.snapshot.side_effect = [(ACCOUNT, snapshot()), (ACCOUNT, snapshot([], used=0))]
        self.bot.monitor()
        self.backend.apply.assert_called_once_with('first')
        self.assertEqual(self.bot.state['approved'], [])
        self.assertIn('Сервис подтвердил', self.texts())
        self.assertEqual(self.bot.state['baseline']['credits'], {})

    def test_incomplete_snapshot_never_applies(self):
        self.bot.state.update(auto_enabled=True, approved=['first'])
        value = snapshot()
        value['rateLimitResetCredits']['availableCount'] = 2
        self.backend.snapshot.return_value = (ACCOUNT, value)
        with self.assertRaises(ValueError):
            self.bot.monitor()
        self.backend.apply.assert_not_called()

    def test_unknown_account_identity_blocks_enable_and_auto_apply(self):
        self.bot.state.update(auto_enabled=True, approved=['first'])
        self.backend.snapshot.return_value = (dict(ACCOUNT, email=None), snapshot())
        with self.assertRaises(app.UserError):
            self.bot.offer_auto()
        self.bot.monitor()
        self.assertFalse(self.bot.state['auto_enabled'])
        self.backend.apply.assert_not_called()

    def test_account_switch_clears_permissions(self):
        self.bot.monitor()
        self.bot.state.update(auto_enabled=True, approved=['first'])
        self.backend.snapshot.return_value = (dict(ACCOUNT, email='another@example.test'), snapshot())
        self.bot.monitor()
        self.assertFalse(self.bot.state['auto_enabled'])
        self.assertEqual(self.bot.state['approved'], [])

    def test_confirmed_redemption_with_failed_verification_pauses_auto(self):
        self.bot.monitor()
        self.bot.state.update(auto_enabled=True, approved=['first'])
        self.backend.snapshot.return_value = (ACCOUNT, snapshot([]))
        self.backend.redemption_status.return_value = 'confirmed'
        self.bot.monitor()
        self.assertFalse(self.bot.state['auto_enabled'])
        self.assertIn('постпроверка не завершена', self.texts())
        self.backend.apply.assert_not_called()

    def test_expired_callback_ack_does_not_block_pause(self):
        self.bot.state['auto_enabled'] = True
        self.telegram.call.side_effect = TelegramError('answerCallbackQuery', 400)
        self.bot.handle({'callback_query': {'id': 'expired', 'from': {'id': 42},
            'message': {'chat': {'id': 42, 'type': 'private'}}, 'data': 'auto:off'}})
        self.assertFalse(self.bot.state['auto_enabled'])

    def test_connect_uses_device_code_and_clears_auto_permissions(self):
        self.bot.state.update(auto_enabled=True, approved=['first'])
        self.backend.start_login.return_value = {'verificationUrl': 'https://auth.openai.com/codex/device',
                                                'userCode': 'ABCD-1234'}
        self.bot.command('connect')
        self.assertIn('ABCD-1234', self.texts())
        self.assertFalse(self.bot.state['auto_enabled'])
        self.assertEqual(self.bot.state['approved'], [])


class EventTests(unittest.TestCase):
    def test_ipv4_precedes_unreachable_advertised_ipv6(self):
        addresses = [(socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('::1', 443, 0, 0)),
                     (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
        connection = Mock()
        with patch('telegram_api.socket.getaddrinfo', return_value=addresses), \
             patch('telegram_api.socket.socket', return_value=connection) as factory:
            self.assertIs(ipv4_first_connection(('api.telegram.org', 443)), connection)
        factory.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM, 6)
        connection.connect.assert_called_once_with(('127.0.0.1', 443))

    def test_ipv6_still_works_if_ipv4_is_unavailable(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443)),
                     (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('::1', 443, 0, 0))]
        failed, success = Mock(), Mock()
        failed.connect.side_effect = TimeoutError('unreachable IPv4')
        with patch('telegram_api.socket.getaddrinfo', return_value=addresses), \
             patch('telegram_api.socket.socket', side_effect=[failed, success]):
            self.assertIs(ipv4_first_connection(('api.telegram.org', 443)), success)
        failed.close.assert_called_once()
        success.connect.assert_called_once_with(('::1', 443, 0, 0))

    def test_scheduled_renewal_is_detected_even_when_usage_stays_zero(self):
        previous = app.snapshot_baseline(ACCOUNT, snapshot(used=0, resets=NOW - 1))
        current = app.snapshot_baseline(ACCOUNT, snapshot(used=0, resets=NOW + 3600))
        self.assertIn(('window_renewed', '10080', NOW + 3600), app.events_between(previous, current, NOW))
    def test_scheduled_renewal_and_unknown_usage_drop_are_distinct(self):
        previous = app.snapshot_baseline(ACCOUNT, snapshot(used=90, resets=NOW - 1))
        current = app.snapshot_baseline(ACCOUNT, snapshot(used=0, resets=NOW + 3600))
        self.assertIn(('window_renewed', '10080', NOW + 3600), app.events_between(previous, current, NOW))
        previous['windows']['10080']['resetsAt'] = NOW + 3600
        kinds = [kind for kind, _, _ in app.events_between(previous, current, NOW)]
        self.assertIn('usage_decreased', kinds)
        self.assertNotIn('window_renewed', kinds)

    def test_first_observation_does_not_announce_old_resets_as_new(self):
        current = app.snapshot_baseline(ACCOUNT, snapshot())
        self.assertEqual(app.events_between(None, current, NOW), [])

    def test_removed_credit_is_not_claimed_as_consumed(self):
        before = app.snapshot_baseline(ACCOUNT, snapshot())
        after = app.snapshot_baseline(ACCOUNT, snapshot([]))
        self.assertEqual(app.events_between(before, after, NOW), [('credit_removed', 'first', NOW + 600)])

    def test_network_errors_never_leak_bot_token(self):
        token = 'secret-telegram-token'
        with patch('telegram_api.urlopen', side_effect=URLError('https://api.telegram.org/bot' + token)):
            with self.assertRaises(TelegramError) as error:
                Telegram(token).call('getMe')
        self.assertNotIn(token, str(error.exception))


if __name__ == '__main__':
    unittest.main()
