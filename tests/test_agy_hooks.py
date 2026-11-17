"""AGY native hooks against synthetic local files only; no real config, CLI, or Kakao."""
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from talkturn import agy_hooks, runtime


SESSION = '11111111-1111-4111-8111-111111111111'


def user(step=0, text='private user prompt'):
    return {'step_index': step, 'source': 'USER_EXPLICIT', 'type': 'USER_INPUT', 'status': 'DONE', 'content': text}


def answer(step=1, text='final result', **changes):
    return {'step_index': step, 'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'status': 'DONE', 'content': text, **changes}


class AgyHooksTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.root = folder / 'TalkTurn state 한글'
        self.root.mkdir()
        self.config = folder / '.gemini' / 'antigravity-cli' / 'settings.json'
        self.hooks = self.config.parent.parent / 'config' / 'hooks.json'
        self.transcript = self.config.parent / 'brain' / SESSION / '.system_generated' / 'logs' / 'transcript.jsonl'
        self.transcript.parent.mkdir(parents=True)
        self.stack.enter_context(patch.object(runtime, 'ROOT', self.root))
        self.stack.enter_context(patch.object(agy_hooks, 'config_path', return_value=self.config))
        self.stack.enter_context(patch.dict(os.environ, {'TALKTURN_AGY_MANAGED_FORWARD': ''}))
        self.stack.enter_context(patch('subprocess.run', side_effect=AssertionError('External process forbidden')))
        self.stack.enter_context(patch('subprocess.Popen', side_effect=AssertionError('External process forbidden')))
        self.record = self.stack.enter_context(patch('talkturn.tasks.record'))
        self.deliver = self.stack.enter_context(patch('talkturn.plain_delivery.deliver', return_value=True))
        (self.root / 'enabled').touch()
        runtime.write_json('ui.json', {'watched_sessions': [{'provider': 'agy', 'session_id': SESSION}]})
        self.payload = {'conversationId': SESSION, 'workspacePaths': [str(folder / 'work')],
                        'modelName': 'synthetic-model', 'transcriptPath': str(self.transcript),
                        'executionNum': 2, 'terminationReason': 'model_stop', 'fullyIdle': True}

    def transcript_rows(self, rows):
        self.transcript.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')

    def read_hooks(self):
        return json.loads(self.hooks.read_text(encoding='utf-8'))

    def test_install_reinstall_and_uninstall_preserve_foreign_hooks_and_disabled_flag(self):
        foreign = {'enabled': False, 'PreToolUse': [{'matcher': 'run_command', 'hooks': [
            {'command': 'user-policy.exe', 'permissionOverrides': ['read_file(safe)']}]}]}
        self.hooks.parent.mkdir(parents=True)
        self.hooks.write_text(json.dumps({'user-policy': foreign}), encoding='utf-8')
        with patch.object(runtime, 'PYTHON', Path('C:/Python with spaces/python.exe')):
            agy_hooks.install()
            installed = self.read_hooks()
            expected = subprocess.list2cmdline(runtime.module_command(
                'agy_hooks', 'event', '--event', 'SessionStart', '--home', str(self.root)))
            self.assertEqual(installed['talkturn']['SessionStart'][0]['command'], expected)
            self.assertEqual(installed['talkturn']['Stop'][0]['timeout'], 300)
            self.assertEqual(installed['user-policy'], foreign)
            installed['talkturn']['enabled'] = False
            self.hooks.write_text(json.dumps(installed), encoding='utf-8')
            agy_hooks.install()
            self.assertEqual(self.read_hooks(), installed)
            self.assertEqual(runtime.read_json(agy_hooks.RECEIPT)['entry'], installed['talkturn'])
            self.assertTrue(agy_hooks.uninstall())
        self.assertEqual(self.read_hooks(), {'user-policy': foreign})
        self.assertFalse(agy_hooks.uninstall())
        self.assertTrue(list((self.root / 'backups').glob('agy-hooks-*.bak')))

    def test_name_collision_and_modified_installation_are_not_overwritten_or_removed(self):
        self.hooks.parent.mkdir(parents=True)
        foreign = {'talkturn': {'enabled': True, 'Stop': [{'command': 'not-talkturn.exe'}]}}
        self.hooks.write_text(json.dumps(foreign), encoding='utf-8')
        with self.assertRaises(ValueError):
            agy_hooks.install()
        self.assertEqual(self.read_hooks(), foreign)
        self.assertFalse((self.root / agy_hooks.RECEIPT).exists())
        self.hooks.write_text('{}', encoding='utf-8')
        agy_hooks.install()
        changed = self.read_hooks()
        changed['talkturn']['Stop'].append({'command': 'user-added-policy.exe'})
        self.hooks.write_text(json.dumps(changed), encoding='utf-8')
        with self.assertRaises(ValueError):
            agy_hooks.uninstall()
        self.assertEqual(self.read_hooks(), changed)

    def test_uninstall_requires_receipt_for_the_exact_config_path(self):
        agy_hooks.install()
        installed = self.read_hooks()
        receipt = runtime.read_json(agy_hooks.RECEIPT)
        runtime.write_json(agy_hooks.RECEIPT, {**receipt, 'path': str(self.root / 'other-hooks.json')})
        self.assertFalse(agy_hooks.uninstall())
        self.assertEqual(self.read_hooks(), installed)

    def test_events_record_only_normalized_metadata_and_never_read_the_transcript(self):
        payload = {**self.payload, 'lastUserInput': 'private question', 'unknown': 'private material'}
        with patch.object(agy_hooks, '_transcript_path') as read:
            agy_hooks.handle('event', payload, event='SessionStart')
            agy_hooks.handle('event', payload, event='PreInvocation')
        self.assertEqual([call.args[2] for call in self.record.call_args_list], ['시작됨', '진행 중'])
        self.assertEqual(set(self.record.call_args.args[1]), {'session_id', 'cwd', 'model'})
        self.assertNotIn('private', repr(self.record.call_args_list))
        read.assert_not_called()
        self.deliver.assert_not_called()

    def test_stop_sends_only_latest_completed_model_response_after_latest_user(self):
        rows = [user(), answer(text='old final'), user(4, 'latest private prompt'),
                answer(5, 'internal thought', type='THOUGHT'),
                answer(6, 'private tool output', source='TOOL', type='TOOL_RESULT'),
                answer(7, 'earlier visible response'), answer(8, '최종 원문\n  spacing preserved')]
        self.transcript_rows(rows)
        self.assertEqual(agy_hooks.handle('stop', self.payload), {})
        args, kwargs = self.deliver.call_args
        self.assertEqual(args[0], rows[-1]['content'])
        self.assertEqual(len(args[1]), 64)
        self.assertEqual(kwargs['origin'], {'source': 'agy', 'kind': 'result', 'session_id': SESSION,
                                          'cwd': self.payload['workspacePaths'][0], 'model': 'synthetic-model'})
        self.assertNotIn('private', repr(self.deliver.call_args))

    def test_cli_159_stop_reads_only_visible_content_from_explicit_full_transcript(self):
        full = self.transcript.with_name('transcript_full.jsonl')
        rows = [user(text='PRIVATE_USER'), answer(1, 'VISIBLE_FINAL', thinking='PRIVATE_THINKING')]
        full.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        self.transcript_rows([user(), answer(1, 'WRONG_SIBLING_RESPONSE')])
        payload = {**self.payload, 'terminationReason': 'NO_TOOL_CALL', 'executionNum': 0,
                   'transcriptPath': str(full)}
        self.assertEqual(agy_hooks.handle('stop', payload), {})
        self.deliver.assert_called_once()
        self.assertEqual(self.deliver.call_args.args[0], 'VISIBLE_FINAL')
        self.assertNotIn('PRIVATE', repr(self.deliver.call_args))
        full.unlink()
        self.deliver.reset_mock()
        with self.assertRaises(FileNotFoundError):
            agy_hooks.handle('stop', payload)
        self.deliver.assert_not_called()  # Never silently substitute the sibling transcript.

    def test_transcript_variants_share_dedup_key_and_unknown_stop_reasons_stay_ignored(self):
        self.transcript_rows([user(), answer(1, 'visible', thinking='PRIVATE_THINKING')])
        full = self.transcript.with_name('transcript_full.jsonl')
        full.write_bytes(self.transcript.read_bytes())
        for path in (self.transcript, full):
            agy_hooks.handle('stop', {**self.payload, 'terminationReason': 'NO_TOOL_CALL', 'transcriptPath': str(path)})
        self.assertEqual(self.deliver.call_args_list[0].args[1], self.deliver.call_args_list[1].args[1])
        self.deliver.reset_mock()
        for reason in ('NO_TOOL_CALL_ERROR', 'no_tool_call', 'UNKNOWN'):
            with patch.object(agy_hooks, '_transcript_path') as read:
                agy_hooks.handle('stop', {**self.payload, 'terminationReason': reason})
                read.assert_not_called()
        self.deliver.assert_not_called()

    def test_nonterminal_disabled_unwatched_and_managed_events_never_read_transcripts(self):
        for change in ({'fullyIdle': False}, {'fullyIdle': 1}, {'terminationReason': 'error'},
                       {'terminationReason': 'max_steps_exceeded'}, {'error': 'private failure'}):
            with self.subTest(change=change), patch.object(agy_hooks, '_transcript_path') as read:
                self.assertEqual(agy_hooks.handle('stop', {**self.payload, **change}), {})
                read.assert_not_called()
        for ui in ({}, {'selected_session': {'provider': 'agy', 'session_id': SESSION}},
                   {'watched_sessions': [{'provider': 'codex', 'session_id': SESSION}]}):
            runtime.write_json('ui.json', ui)
            with patch.object(agy_hooks, '_transcript_path') as read:
                agy_hooks.handle('stop', self.payload)
                read.assert_not_called()
        runtime.write_json('ui.json', {'watched_sessions': [{'provider': 'agy', 'session_id': SESSION}]})
        with patch.dict(os.environ, {'TALKTURN_AGY_MANAGED_FORWARD': '1'}), \
             patch.object(agy_hooks, '_transcript_path') as read:
            agy_hooks.handle('stop', self.payload)
            read.assert_not_called()
        (self.root / 'enabled').unlink()
        with patch.object(agy_hooks, '_transcript_path') as read:
            agy_hooks.handle('stop', self.payload)
            read.assert_not_called()
        self.deliver.assert_not_called()

    def test_invalid_uuid_execution_and_foreign_paths_fail_before_transcript_read(self):
        for change in ({'conversationId': '../other'}, {'conversationId': None}, {'executionNum': True},
                       {'executionNum': -1}, {'executionNum': '2'}, {'transcriptPath': 'relative.jsonl'},
                       {'transcriptPath': str(self.root / 'foreign.jsonl')},
                       {'transcriptPath': str(self.transcript.with_name('thinking.jsonl'))},
                       {'transcriptPath': r'\\server\share\transcript.jsonl'}):
            with self.subTest(change=change), patch.object(agy_hooks, '_last_response') as read:
                with self.assertRaises((ValueError, OSError)):
                    agy_hooks.handle('stop', {**self.payload, **change})
                read.assert_not_called()
        self.deliver.assert_not_called()

    def test_transcript_reparse_point_is_rejected(self):
        self.transcript_rows([user(), answer()])
        with patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)):
            with self.assertRaises(ValueError):
                agy_hooks.handle('stop', self.payload)
        self.deliver.assert_not_called()

    def test_missing_user_boundary_partial_response_and_malformed_lines_fail_closed(self):
        for rows in ([answer()], [user(), answer(), user(2)],
                     [user(), answer(), answer(2, status='RUNNING')],
                     [user(), answer(type='THOUGHT')], [user(), answer(content={})],
                     [user(), answer(5), answer(4)]):
            with self.subTest(rows=rows):
                self.transcript_rows(rows)
                with self.assertRaises(ValueError):
                    agy_hooks.handle('stop', self.payload)
        self.transcript_rows([user(), answer()])
        self.transcript.write_bytes(self.transcript.read_bytes() + b'{"partial"')
        with self.assertRaises(ValueError):
            agy_hooks.handle('stop', self.payload)
        self.transcript.write_bytes(b'not json\n')
        with self.assertRaises(ValueError):
            agy_hooks.handle('stop', self.payload)
        self.deliver.assert_not_called()

    def test_tail_limit_skips_old_history_but_rejects_an_oversized_current_response(self):
        self.transcript_rows([user(10), answer(11)])
        tail = self.transcript.read_bytes()
        self.transcript.write_bytes(b'x' * (agy_hooks.MAX_TRANSCRIPT_BYTES + 1) + b'\n' + tail)
        agy_hooks.handle('stop', self.payload)
        self.deliver.assert_called_once()
        self.deliver.reset_mock()
        self.transcript_rows([user(), answer(text='x' * agy_hooks.MAX_TRANSCRIPT_BYTES)])
        with self.assertRaises(ValueError):
            agy_hooks.handle('stop', self.payload)
        self.deliver.assert_not_called()

    def test_event_key_is_stable_for_duplicate_hook_and_changes_with_execution_or_step(self):
        self.transcript_rows([user(), answer()])
        for execution in (2, 2, 3):
            agy_hooks.handle('stop', {**self.payload, 'executionNum': execution,
                                      'transcriptPath': str(self.transcript.resolve())})
        self.transcript_rows([user(), answer(2)])
        agy_hooks.handle('stop', self.payload)
        keys = [call.args[1] for call in self.deliver.call_args_list]
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(len(set(keys)), 3)

    def test_main_always_returns_empty_object_and_errors_store_only_metadata(self):
        for text in ('not json with private content', json.dumps({**self.payload, 'conversationId': 'private-bad-id'})):
            with self.subTest(text=text), patch.object(agy_hooks.sys, 'argv', ['agy_hooks', 'stop', '--home', str(self.root)]), \
                 patch.object(agy_hooks.sys, 'stdin', io.StringIO(text)), patch.dict(os.environ, {}), \
                 redirect_stdout(io.StringIO()) as output:
                agy_hooks.main()
                self.assertEqual(output.getvalue(), '{}\n')
                status = runtime.read_json('hook-status.json')
                self.assertEqual(set(status), {'source', 'error', 'at'})
                self.assertNotIn('private', json.dumps(status))
        self.deliver.assert_not_called()

    def test_invalid_cli_arguments_still_emit_only_empty_object(self):
        for args in ([], ['--help'], ['unknown'], ['event', '--event', 'private-invalid-event']):
            with self.subTest(args=args), patch.object(agy_hooks.sys, 'argv', ['agy_hooks', *args]), \
                 patch.object(agy_hooks.sys, 'stdin', io.StringIO('{}')), redirect_stdout(io.StringIO()) as output:
                agy_hooks.main()
                self.assertEqual(output.getvalue(), '{}\n')
        self.deliver.assert_not_called()


if __name__ == '__main__':
    unittest.main()
