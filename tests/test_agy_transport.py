"""AGY ACP contract checks using a synthetic Python child, never a live agent."""
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from talkturn import agy_transport as agy, runtime


SESSION = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'
CHILD = r'''
import json, sys, time
from pathlib import Path
sys.stdin.reconfigure(encoding='utf-8')
sys.stdout.reconfigure(encoding='utf-8')
config = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
trace = Path(sys.argv[2])
mode = config.get('mode', '')
page_count = 0
def read():
    line = sys.stdin.readline()
    if not line: return None
    item = json.loads(line)
    with trace.open('a', encoding='utf-8') as output: output.write(json.dumps(item) + '\n')
    return item
def send(item):
    print(json.dumps(item), flush=True)
def result(req, value):
    send({'jsonrpc': '2.0', 'id': req['id'], 'result': value})
def update(session, kind, **fields):
    send({'jsonrpc': '2.0', 'method': 'session/update', 'params': {
        'sessionId': session, 'update': {'sessionUpdate': kind, **fields}}})
while True:
    req = read()
    if req is None: break
    method = req.get('method')
    if method == 'initialize':
        if mode == 'bad_json':
            print('not JSON', flush=True); continue
        if mode == 'bad_id':
            send({'jsonrpc': '2.0', 'id': 777, 'result': {}}); continue
        if mode == 'bad_error':
            send({'jsonrpc': '2.0', 'id': req['id'], 'error': 'PRIVATE ERROR'}); continue
        result(req, {'protocolVersion': 1, 'agentCapabilities': {
            'sessionCapabilities': config.get('capabilities', {'list': {}, 'resume': {}})}})
    elif method == 'session/list':
        page_count += 1
        if mode == 'timeout': time.sleep(10)
        if mode == 'readonly_request':
            send({'jsonrpc': '2.0', 'id': 'filesystem', 'method': 'fs/read_text_file',
                  'params': {'sessionId': config['session'], 'path': 'C:/not-read.txt'}})
            read()
        if mode == 'rpc_error':
            send({'jsonrpc': '2.0', 'id': req['id'], 'error': {'code': -32000, 'message': 'PRIVATE ERROR'}})
            continue
        page = {'sessions': config.get('rows', [])}
        if mode == 'repeat_cursor': page['nextCursor'] = 'same'
        if mode == 'unique_cursors': page['nextCursor'] = str(page_count)
        result(req, page)
    elif method == 'session/resume':
        result(req, {})
    elif method == 'session/prompt':
        session = config['session']
        update(config['other'], 'agent_message_chunk', content={'type': 'text', 'text': 'WRONG SESSION'})
        update(session, 'user_message_chunk', content={'type': 'text', 'text': 'USER INPUT'})
        if mode == 'unconfirmed_error':
            send({'jsonrpc': '2.0', 'id': req['id'], 'error': {'code': -32002, 'message': 'PRIVATE ERROR'}})
            continue
        if mode != 'no_updates':
            update(session, 'agent_thought_chunk', content={'type': 'text', 'text': 'PRIVATE THOUGHT'})
            update(session, 'tool_call', toolCallId='tool-1', rawOutput='PRIVATE TOOL OUTPUT')
        if mode == 'permission':
            send({'jsonrpc': '2.0', 'id': 'permission', 'method': 'session/request_permission',
                  'params': {'sessionId': session, 'toolCall': {'toolCallId': 'tool-1'},
                             'options': [{'optionId': 'allow', 'kind': 'allow_once', 'name': 'Allow'}]}})
            read()
            send({'jsonrpc': '2.0', 'id': 'terminal', 'method': 'terminal/create',
                  'params': {'sessionId': session, 'command': 'DO NOT EXECUTE'}})
            read()
        if mode == 'after_ack_error':
            send({'jsonrpc': '2.0', 'id': req['id'], 'error': {'code': -32002, 'message': 'PRIVATE ERROR'}})
            continue
        if mode != 'no_updates':
            update(session, 'agent_message_chunk', content={'type': 'text', 'text': 'Synthetic '})
            update(session, 'agent_message_chunk', content={'type': 'text', 'text': 'answer.'})
        result(req, {'stopReason': config.get('stop_reason', 'end_turn')})
    else:
        raise RuntimeError('forbidden method: ' + str(method))
'''


class AgyTransportTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(runtime, 'ROOT', self.root))
        self.script = self.root / 'synthetic_acp.py'
        self.script.write_text(CHILD, encoding='utf-8')
        self.config_path = self.root / 'config.json'
        self.trace_path = self.root / 'trace.jsonl'
        self.config = {'session': SESSION, 'other': OTHER, 'rows': [
            {'sessionId': SESSION, 'cwd': str(self.root), 'title': 'Synthetic task', 'updatedAt': '2026-11-17T00:00:00Z'}]}
        self.configure()
        self.stack.enter_context(patch.object(agy, '_command', return_value=[sys.executable, str(self.script),
            str(self.config_path), str(self.trace_path)]))
        self.deliver = self.stack.enter_context(patch('talkturn.plain_delivery.deliver', return_value=True))
        runtime.write_json('ui.json', {'watched_sessions': [{'provider': 'agy', 'session_id': SESSION}]})
        (self.root / 'enabled').touch()

    def configure(self, **changes):
        self.config.update(changes)
        self.config_path.write_text(json.dumps(self.config), encoding='utf-8')

    def trace(self):
        return [json.loads(line) for line in self.trace_path.read_text(encoding='utf-8').splitlines()]

    def payload(self, text='literal $(x) ;\n한글  그대로'):
        return {'request_id': uuid.uuid4().hex, 'origin': {'source': 'agy', 'session_id': SESSION,
                'cwd': str(self.root), 'name': 'Synthetic task'}, 'text': text}

    def test_metadata_only_listing_preserves_uuid_and_never_claims_live(self):
        rows, note = agy.sessions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['session_id'], SESSION)
        self.assertTrue(rows[0]['code'].startswith('G-'))
        self.assertTrue(rows[0]['reachable'])
        self.assertFalse(rows[0]['live'])
        self.assertIsNone(rows[0]['model'])
        self.assertIn('실행 상태', note)
        self.assertEqual([item['method'] for item in self.trace()], ['initialize', 'session/list'])

    def test_missing_capability_never_falls_back_to_history_or_new_session(self):
        self.configure(capabilities={'resume': {}})
        with self.assertRaises(RuntimeError):
            agy.sessions()
        self.assertEqual([item['method'] for item in self.trace()], ['initialize'])
        self.configure(capabilities={'list': {}})
        rows, _ = agy.sessions()
        self.assertFalse(rows[0]['reachable'])
        with self.assertRaises(ValueError):
            agy.validate(self.payload()['origin'])

    def test_repeated_and_empty_pages_are_bounded(self):
        self.configure(mode='repeat_cursor')
        self.assertEqual(len(agy.sessions()[0]), 1)
        self.assertEqual(sum(item.get('method') == 'session/list' for item in self.trace()), 2)
        self.trace_path.unlink()
        self.configure(rows=[])
        self.assertEqual(agy.sessions()[0], [])
        self.assertEqual(sum(item.get('method') == 'session/list' for item in self.trace()), 1)
        self.trace_path.unlink()
        self.configure(mode='unique_cursors', rows=[{'sessionId': SESSION, 'cwd': str(self.root)}])
        self.assertEqual(len(agy.sessions()[0]), 1)
        self.assertEqual(sum(item.get('method') == 'session/list' for item in self.trace()), 10)

    def test_invalid_uuid_and_cwd_are_not_exposed_as_targets(self):
        self.configure(rows=[{'sessionId': 'not-a-uuid', 'cwd': str(self.root)},
                             {'sessionId': SESSION, 'cwd': 'relative'}])
        self.assertEqual(agy.sessions()[0], [])

    def test_validation_rechecks_uuid_and_cwd_before_resuming(self):
        for changes in ({'session_id': OTHER}, {'cwd': str(self.root / 'different')}):
            with self.subTest(changes=changes):
                origin = {**self.payload()['origin'], **changes}
                with self.assertRaises(ValueError):
                    agy.validate(origin)
        self.assertTrue(all(item['method'] in ('initialize', 'session/list') for item in self.trace()))

    @unittest.skipUnless(os.name == 'nt', 'AGY Windows drive metadata')
    def test_windows_uri_style_drive_cwd_is_validated_and_resumed_natively(self):
        metadata_cwd = '/' + self.root.as_posix()
        self.configure(rows=[{'sessionId': SESSION, 'cwd': metadata_cwd, 'title': 'Synthetic task'}])
        rows, _ = agy.sessions()
        self.assertEqual(rows[0]['cwd'], agy._cwd(str(self.root)))
        self.assertTrue(Path(rows[0]['cwd']).is_absolute())
        receipt = agy.run_worker(self.payload())
        self.assertTrue(receipt['accepted'])
        resume = next(item for item in self.trace() if item.get('method') == 'session/resume')
        self.assertEqual(resume['params']['cwd'], agy._cwd(str(self.root)))
        for invalid in ('/C:relative', 'file:///C:/work', '/relative', r'\\server\share\work',
                        '//server/share/work', r'\\?\C:\work'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                agy._cwd(invalid)

    def test_readonly_server_requests_are_rejected_without_reading_files(self):
        self.configure(mode='readonly_request')
        agy.sessions()
        reply = next(item for item in self.trace() if item.get('id') == 'filesystem')
        self.assertEqual(reply['error']['code'], -32601)
        with agy.AgyClient() as client:
            for method in ('session/new', 'session/load', 'session/resume', 'session/prompt'):
                with self.subTest(method=method), self.assertRaises(ValueError):
                    client.call(method, {})

    def test_malformed_rpc_and_provider_errors_are_sanitized(self):
        for mode in ('bad_json', 'bad_id', 'bad_error', 'rpc_error'):
            with self.subTest(mode=mode):
                self.configure(mode=mode)
                with self.assertRaises(RuntimeError) as caught:
                    agy.sessions()
                self.assertNotIn('PRIVATE', str(caught.exception))

    def test_timeout_closes_only_the_owned_acp_process(self):
        self.configure(mode='timeout')
        with agy.AgyClient() as client:
            with self.assertRaises(TimeoutError):
                client.call('session/list', {}, timeout=.03)
            owned = client.process
        self.assertIsNotNone(owned.poll())

    def test_worker_keeps_exact_prompt_and_only_delivers_assistant_text(self):
        payload = self.payload()
        with patch.object(agy.subprocess, 'Popen', wraps=agy.subprocess.Popen) as spawn:
            receipt = agy.run_worker(payload)
        self.assertEqual(spawn.call_args.kwargs['env']['TALKTURN_AGY_MANAGED_FORWARD'], '1')
        calls = self.trace()
        self.assertEqual([item.get('method') for item in calls],
                         ['initialize', 'session/list', 'session/resume', 'session/prompt'])
        self.assertEqual(calls[-2]['params'], {'sessionId': SESSION, 'cwd': agy._cwd(str(self.root)), 'mcpServers': []})
        self.assertEqual(calls[-1]['params']['prompt'], [{'type': 'text', 'text': payload['text']}])
        self.assertTrue(receipt['accepted'])
        self.assertEqual(receipt['stage'], 'completed')
        self.deliver.assert_called_once()
        self.assertEqual(self.deliver.call_args.args[0], 'Synthetic answer.')
        saved = (self.root / 'agy' / (payload['request_id'] + '.json')).read_text(encoding='utf-8')
        for private in ('PRIVATE', 'Synthetic answer.', payload['text']):
            self.assertNotIn(private, saved)

    def test_permission_is_cancelled_and_terminal_request_rejected(self):
        self.configure(mode='permission')
        receipt = agy.run_worker(self.payload())
        calls = self.trace()
        self.assertEqual(next(item for item in calls if item.get('id') == 'permission')['result'],
                         {'outcome': {'outcome': 'cancelled'}})
        self.assertEqual(next(item for item in calls if item.get('id') == 'terminal')['error']['code'], -32601)
        self.assertTrue(receipt['resume_warning'])
        self.assertIn(agy.LIMITATION, self.deliver.call_args.args[0])

    def test_successful_prompt_result_can_ack_without_any_streamed_text(self):
        self.configure(mode='no_updates')
        receipt = agy.run_worker(self.payload())
        self.assertTrue(receipt['accepted'])
        self.assertEqual(receipt['stop_reason'], 'end_turn')

    def test_foreign_and_user_notifications_cannot_prove_acceptance(self):
        self.configure(mode='unconfirmed_error')
        receipt = agy.run_worker(self.payload())
        self.assertFalse(receipt['accepted'])
        self.assertEqual(receipt['stage'], 'failed')
        self.deliver.assert_not_called()

    def test_failure_after_ack_never_resubmits_the_prompt(self):
        self.configure(mode='after_ack_error')
        receipt = agy.run_worker(self.payload())
        self.assertTrue(receipt['accepted'])
        self.assertEqual(receipt['stage'], 'failed')
        self.assertEqual(sum(item.get('method') == 'session/prompt' for item in self.trace()), 1)
        self.assertNotIn('PRIVATE', json.dumps(receipt))

    def test_nonfinal_stop_and_delivery_failure_are_not_reported_as_completed_work(self):
        self.configure(stop_reason='cancelled')
        receipt = agy.run_worker(self.payload())
        self.assertEqual(receipt['stage'], 'stopped')
        self.assertTrue(receipt['resume_warning'])
        self.deliver.reset_mock()
        self.deliver.side_effect = RuntimeError('uncertain delivery')
        self.configure(stop_reason='end_turn')
        receipt = agy.run_worker(self.payload())
        self.assertEqual(receipt['stage'], 'completed')
        self.assertFalse(receipt['result_delivered'])
        self.deliver.assert_called_once()

    def test_unwatched_or_disabled_session_does_not_send_results(self):
        runtime.write_json('ui.json', {'watched_sessions': []})
        self.assertFalse(agy.run_worker(self.payload())['result_delivered'])
        runtime.write_json('ui.json', {'watched_sessions': [{'provider': 'agy', 'session_id': SESSION}]})
        (self.root / 'enabled').unlink()
        self.assertFalse(agy.run_worker(self.payload())['result_delivered'])
        self.deliver.assert_not_called()

    def test_unverified_delivery_stays_false_without_a_retry(self):
        self.deliver.return_value = False
        receipt = agy.run_worker(self.payload())
        self.assertTrue(receipt['accepted'])
        self.assertEqual(receipt['stage'], 'completed')
        self.assertFalse(receipt['result_delivered'])
        self.deliver.assert_called_once()

    def test_session_lock_rejects_concurrent_prompt_before_spawn(self):
        with runtime.locked('agy-' + SESSION + '.lock'), patch.object(agy, '_command') as command:
            receipt = agy.run_worker(self.payload())
        self.assertFalse(receipt['accepted'])
        self.assertEqual(receipt['error_type'], 'TimeoutError')
        command.assert_not_called()

    def test_parent_passes_text_only_on_stdin_and_accepts_verified_receipt(self):
        captured = []
        class Pipe(io.StringIO):
            def close(pipe):
                payload = json.loads(pipe.getvalue())
                captured.append(payload)
                runtime.write_json('agy/' + payload['request_id'] + '.json', {
                    'request_id': payload['request_id'], 'session_id': payload['origin']['session_id'], 'accepted': True})
                super().close()
        child = SimpleNamespace(stdin=Pipe(), poll=Mock(return_value=None))
        payload = self.payload()
        with patch.object(agy.subprocess, 'Popen', return_value=child) as spawn:
            result = agy.forward(payload['origin'], payload['text'])
        self.assertTrue(result['accepted'])
        self.assertEqual(captured[0]['text'], payload['text'])
        self.assertNotIn(payload['text'], str(spawn.call_args.args))
        self.assertFalse(spawn.call_args.kwargs.get('shell', False))

    def test_parent_timeout_never_retries_or_claims_acceptance(self):
        child = SimpleNamespace(stdin=io.StringIO(), poll=Mock(return_value=None))
        with patch.object(agy.subprocess, 'Popen', return_value=child) as spawn, patch.object(agy, 'ACK_TIMEOUT', .02):
            with self.assertRaises(TimeoutError):
                agy.forward(self.payload()['origin'], 'Synthetic timeout')
        spawn.assert_called_once()


if __name__ == '__main__':
    unittest.main()
