"""Provider failures and question deadlines with synthetic state and transports."""
from contextlib import ExitStack
from pathlib import Path
import queue
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from talkturn import codex_rpc, providers, questions, runtime, sessions


SESSION = '11111111-1111-4111-8111-111111111111'


class ProviderEdgeCasesTest(unittest.TestCase):
    def test_answer_accepted_before_expiry_survives_next_poll(self):
        with ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            stack.enter_context(patch.object(runtime, 'ROOT', root))
            stack.enter_context(patch.object(questions, 'ROOT', root))
            stack.enter_context(patch.object(sessions, 'claude_title', return_value='Synthetic task'))
            stack.enter_context(patch('talkturn.tasks.record'))
            (root / 'enabled').touch()
            (root / 'reply-enabled').touch()
            runtime.write_json('ui.json', {'watched_sessions': [{'provider': 'claude', 'session_id': SESSION}]})
            now = [1000.0]
            acknowledgements = []

            def deliver(*args, **kwargs):
                now[0] = 1599.5

            def sleep(_):
                now[0] = 1599.9
                record = runtime.read_json('questions.json')[0]
                acknowledgements.append(questions.answer(record['id'], '거절'))
                now[0] = 1600.5

            stack.enter_context(patch.object(questions, 'time', SimpleNamespace(time=lambda: now[0], sleep=sleep)))
            stack.enter_context(patch('talkturn.plain_delivery.deliver', side_effect=deliver))
            output = questions.handle('claude', {
                'hook_event_name': 'PermissionRequest', 'session_id': SESSION,
                'tool_name': 'Bash', 'tool_input': {'command': 'echo synthetic'},
            })
            self.assertEqual(len(acknowledgements), 1)
            self.assertEqual(output['hookSpecificOutput']['decision'], {'behavior': 'deny'})
            self.assertEqual(runtime.read_json('questions.json')[0]['status'], 'returned_to_hook')

    def test_codex_loaded_pages_stop_on_empty_page_or_repeated_cursor(self):
        for data in ([], [SESSION]):
            with self.subTest(data=data):
                client = Mock(shared=True)
                loaded_calls = []

                def call(method, params):
                    if method == 'thread/list':
                        return {'data': [], 'nextCursor': None}
                    if method == 'thread/loaded/list':
                        loaded_calls.append(params)
                        if len(loaded_calls) > 2:
                            self.fail('Loaded thread pagination did not stop at a repeated cursor')
                        return {'data': data, 'nextCursor': 'same-cursor'}
                    self.assertEqual(method, 'thread/read')
                    return {'thread': {'id': SESSION, 'status': {'type': 'idle'}}}

                client.call.side_effect = call
                with patch.object(sessions, 'CodexReader') as reader:
                    reader.return_value.__enter__.return_value = client
                    rows, _ = sessions.codex_sessions()
                self.assertEqual(len(loaded_calls), 2 if data else 1)
                self.assertEqual([row['session_id'] for row in rows], [SESSION] if data else [])

    def test_codex_rpc_rejects_malformed_envelopes_without_raw_error_text(self):
        for message in (None, [], 'private response', {'id': 1, 'error': 'private error'}, {'id': 1}):
            with self.subTest(message=message):
                client = codex_rpc.CodexReader()
                client.sequence = 0
                client.messages = queue.Queue()
                client.messages.put(message)
                with patch.object(client, 'send') as send, self.assertRaises(RuntimeError) as caught:
                    client.call('account/read', timeout=.02)
                self.assertNotIn('private', str(caught.exception))
                self.assertEqual(send.call_count, 1)

    def test_codex_websocket_invalid_json_is_controlled_failure(self):
        client = codex_rpc.CodexReader()
        client.sequence = 0
        client.ws = Mock()
        client.ws.recv.return_value = '{private invalid json'
        with patch.object(client, 'send'), self.assertRaises(RuntimeError) as caught:
            client.call('account/read', timeout=.02)
        self.assertNotIn('private', str(caught.exception))

    def test_codex_closed_connection_and_normal_result_keep_existing_contract(self):
        for message in ({'_closed': True}, {'id': 1, 'result': {'account': None}}):
            with self.subTest(message=message):
                client = codex_rpc.CodexReader()
                client.sequence = 0
                client.messages = queue.Queue()
                client.messages.put(message)
                with patch.object(client, 'send') as send:
                    if '_closed' in message:
                        with self.assertRaises(RuntimeError):
                            client.call('account/read', timeout=.02)
                    else:
                        self.assertEqual(client.call('account/read', timeout=.02), {'account': None})
                self.assertEqual(send.call_count, 1)

    def test_usage_isolates_malformed_codex_payloads(self):
        for payload in (None, [], {'rateLimitsByLimitId': ['invalid']}):
            with self.subTest(payload=payload), patch.object(providers, 'CodexReader') as reader, \
                 patch.object(providers, 'claude_usage', return_value={'accounts': []}):
                reader.return_value.__enter__.return_value.call.return_value = payload
                result = providers.usage('all')
                self.assertEqual(result['claude'], {'accounts': []})
                self.assertEqual(result['codex']['state'], 'unavailable')

    def test_usage_isolates_malformed_claude_payload_and_redacts_exception(self):
        with patch.object(providers, 'ccswap_accounts', return_value={'claude': None}), \
             patch.object(providers, 'codex_usage', return_value={'limits': []}):
            result = providers.usage('all')
        self.assertEqual(result['codex'], {'limits': []})
        self.assertEqual(result['claude'], {'state': 'unavailable', 'error': 'AttributeError'})
        with patch.object(providers, 'claude_usage', side_effect=ValueError('private account payload')):
            self.assertEqual(providers.usage('claude')['claude'],
                             {'state': 'unavailable', 'error': 'ValueError'})

    def test_claude_peer_names_may_repeat_but_refs_stay_distinct(self):
        peers = [{'name': 'same-name', 'ref': 'first', 'type': 'desktop'},
                 {'name': 'same-name', 'ref': 'second', 'type': 'desktop'},
                 {'name': 'same-name', 'ref': 'first', 'type': 'desktop'}]
        with patch.object(sessions, 'executable', return_value='never-launched.exe'), \
             patch.object(sessions, 'run_json', return_value=[]), \
             patch.object(sessions, 'claude_targets', return_value={'targets': peers}), \
             patch.object(sessions, 'claude_desktop_sessions', return_value=[]):
            rows, _ = sessions.claude_sessions()
        self.assertEqual([row['peer_ref'] for row in rows], ['first', 'second'])
        self.assertEqual(len({row['code'] for row in rows}), 2)
        for row in rows:
            with self.subTest(ref=row['peer_ref']), \
                 patch.object(sessions, 'selected', return_value=row), \
                 patch.object(sessions, 'claude_targets', return_value={'targets': peers[:2]}), \
                 patch.object(sessions, 'write_json'), \
                 patch('talkturn.reply_bridge.forward', return_value={'accepted': True}) as forward:
                sessions.send_selected('synthetic reply', expected=row)
                self.assertEqual(forward.call_args.args[0]['peer_ref'], row['peer_ref'])
        with patch.object(sessions, 'claude_targets', return_value={'targets': peers[:1]}):
            with self.assertRaises(ValueError):
                sessions.validate_target(rows[1])


if __name__ == '__main__':
    unittest.main()
