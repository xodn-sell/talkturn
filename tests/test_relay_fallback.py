import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from talkturn import relay_fallback as relay, runtime


class RelayFallbackTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        root_patch = patch.object(runtime, 'ROOT', self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        runtime.write_json('delivery.json', {'model': 'gpt-6-luna', 'room_name': 'keep-local'})

    def test_quota_fallback_saves_success_and_preserves_other_settings(self):
        invoke = Mock(side_effect=[relay.RelayQuotaExceeded('private provider details'), {'bubbles': []}])
        with patch.object(relay.providers, 'login_state', return_value=True) as login:
            result = relay.run_relay('gpt-6-luna', invoke)
        self.assertEqual(result, {'bubbles': []})
        self.assertEqual([call.args[0] for call in invoke.call_args_list], ['gpt-6-luna', 'claude-haiku-5-5'])
        login.assert_called_once_with('claude')
        self.assertEqual(runtime.read_json('delivery.json'), {'model': 'claude-haiku-5-5', 'room_name': 'keep-local'})
        receipt = runtime.read_json('relay-status.json')
        self.assertEqual(set(receipt), {'from_model', 'used_model', 'model_saved', 'attempts', 'at'})
        self.assertEqual(receipt['attempts'], 2)
        self.assertTrue(receipt['model_saved'])
        self.assertNotIn('private', json.dumps(receipt))

    def test_nonquota_errors_never_retry_or_probe_login(self):
        for error in (TimeoutError('timeout'), RuntimeError('auth failed'), ValueError('invalid result')):
            invoke = Mock(side_effect=error)
            with self.subTest(error=type(error).__name__), patch.object(relay.providers, 'login_state') as login:
                with self.assertRaises(type(error)) as caught:
                    relay.run_relay('gpt-6-luna', invoke)
                self.assertIs(caught.exception, error)
                invoke.assert_called_once_with('gpt-6-luna')
                login.assert_not_called()

    def test_both_exhausted_stop_after_two_attempts_without_saving(self):
        invoke = Mock(side_effect=relay.RelayQuotaExceeded('private'))
        with patch.object(relay.providers, 'login_state', return_value=True):
            with self.assertRaisesRegex(relay.RelayQuotaExceeded, '모두 소진'):
                relay.run_relay('gpt-6-luna', invoke)
        self.assertEqual(invoke.call_count, 2)
        self.assertEqual(runtime.read_json('delivery.json')['model'], 'gpt-6-luna')
        self.assertFalse((self.root / 'relay-status.json').exists())

    def test_missing_auth_and_disabled_setting_do_not_call_alternate(self):
        for enabled, logged_in in ((True, False), (False, True)):
            runtime.write_json('delivery.json', {'model': 'gpt-6-luna', 'relay_auto_switch': enabled})
            invoke = Mock(side_effect=relay.RelayQuotaExceeded())
            with self.subTest(enabled=enabled), patch.object(relay.providers, 'login_state', return_value=logged_in) as login:
                with self.assertRaises(relay.RelayQuotaExceeded):
                    relay.run_relay('gpt-6-luna', invoke)
                invoke.assert_called_once_with('gpt-6-luna')
                self.assertEqual(login.call_count, int(enabled))
            self.assertEqual(runtime.read_json('delivery.json')['model'], 'gpt-6-luna')

    def test_reverse_fallback_preserves_concurrent_explicit_model_choice(self):
        runtime.write_json('delivery.json', {'model': 'claude-haiku-5-5'})
        def invoke(model):
            if model == 'claude-haiku-5-5':
                raise relay.RelayQuotaExceeded()
            runtime.write_json('delivery.json', {'model': 'explicit-model', 'room_name': 'new-room'})
            return 'valid result'
        with patch.object(relay.providers, 'login_state', return_value=True) as login:
            self.assertEqual(relay.run_relay('claude-haiku-5-5', invoke), 'valid result')
        login.assert_called_once_with('codex')
        self.assertEqual(runtime.read_json('delivery.json'), {'model': 'explicit-model', 'room_name': 'new-room'})
        self.assertFalse(runtime.read_json('relay-status.json')['model_saved'])

    def test_detector_requires_explicit_exhaustion_in_error_context(self):
        positives = [
            ({'type': 'turn.failed', 'error': {'message': "You've hit your usage limit. Try again later."}}, ''),
            ({'error': {'code': 'insufficient_quota'}}, ''),
            ({'type': 'result', 'is_error': True, 'errors': ["You've hit your limit · resets tomorrow"]}, ''),
            ({}, 'Error: Usage limit reached'),
        ]
        for event, stderr in positives:
            with self.subTest(event=event, stderr=stderr):
                self.assertTrue(relay.quota_exhausted(json.dumps(event), stderr))
        negatives = [
            {'type': 'assistant', 'message': {'content': "You've hit your usage limit"}},
            {'type': 'result', 'is_error': False, 'result': "You've hit your limit"},
            {'type': 'turn.completed', 'usage': {'input_tokens': 100}},
            {'type': 'error', 'error': {'code': '429', 'message': 'Rate limit reached'}},
            {'type': 'error', 'message': 'Authentication failed'},
            {'type': 'error', 'message': 'Model unavailable'},
            {'type': 'error', 'message': 'Request timed out'},
        ]
        for event in negatives:
            with self.subTest(event=event):
                self.assertFalse(relay.quota_exhausted(json.dumps(event), ''))
        self.assertFalse(relay.quota_exhausted("You've hit your usage limit", 'HTTP 429 Too Many Requests'))


if __name__ == '__main__':
    unittest.main()
