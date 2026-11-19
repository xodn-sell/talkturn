"""Portable state and real OS locks, isolated from provider accounts and desktop apps."""
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from talkturn import runtime, sessions


class PortableCoreTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='talkturn-portable-')
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.state = self.folder / 'state 한글'
        self.state.mkdir()
        self.home = self.folder / 'test-home'
        self.home.mkdir()
        self.children = []
        self.events = {}
        self.addCleanup(self.cleanup_children)
        root_patch = patch.object(runtime, 'ROOT', self.state)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.environment = {**os.environ,
            'TALKTURN_HOME': str(self.state), 'TOKTURN_HOME': str(self.state),
            'HOME': str(self.home), 'USERPROFILE': str(self.home),
            'APPDATA': str(self.home / 'AppData' / 'Roaming'),
            'LOCALAPPDATA': str(self.home / 'AppData' / 'Local'),
            'CODEX_HOME': str(self.home / '.codex'), 'CLAUDE_CONFIG_DIR': str(self.home / '.claude'),
            'PYTHONUTF8': '1', 'PYTHONPATH': str(Path(runtime.__file__).resolve().parent.parent)}

    def spawn(self, code):
        process = subprocess.Popen([sys.executable, '-u', '-c',
            'import sys\nfrom talkturn import runtime\n' + code],
            cwd=self.folder, env=self.environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding='utf-8', creationflags=runtime.NO_WINDOW)
        events = queue.Queue()
        self.events[process] = events

        def read():
            for line in process.stdout:
                events.put(line.rstrip('\r\n'))
            events.put(None)

        thread = threading.Thread(target=read, daemon=True)
        self.children.append((process, thread))
        thread.start()
        return process

    def expect(self, process, event):
        try:
            actual = self.events[process].get(timeout=15)
        except queue.Empty:
            self.fail(f'Child did not report {event!r} within the test deadline')
        self.assertEqual(actual, event, f'Child exited or reported another event (exit={process.poll()})')

    def release(self, process):
        process.stdin.write('release\n')
        process.stdin.flush()

    def finish(self, process):
        process.wait(timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr.read())

    def cleanup_children(self):
        for process, thread in self.children:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            thread.join(timeout=1)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

    def holder(self):
        process = self.spawn("with runtime.locked('portable.lock'):\n"
                             "    print('LOCKED', flush=True)\n"
                             "    sys.stdin.readline()\n")
        self.expect(process, 'LOCKED')
        return process

    def test_other_process_times_out_while_lock_owner_remains_alive(self):
        holder = self.holder()
        contender = self.spawn("try:\n"
            "    with runtime.locked('portable.lock', timeout=.3):\n"
            "        print('ACQUIRED', flush=True)\n"
            "except TimeoutError:\n"
            "    print('TIMEOUT', flush=True)\n")
        self.expect(contender, 'TIMEOUT')
        self.finish(contender)
        self.assertIsNone(holder.poll())
        self.release(holder)
        self.finish(holder)

    def test_waiting_process_acquires_after_normal_release(self):
        holder = self.holder()
        waiter = self.spawn("print('WAITING', flush=True)\n"
            "with runtime.locked('portable.lock', timeout=10):\n"
            "    print('ACQUIRED', flush=True)\n")
        self.expect(waiter, 'WAITING')
        self.release(holder)
        self.finish(holder)
        self.expect(waiter, 'ACQUIRED')
        self.finish(waiter)

    def test_exception_releases_lock_before_owner_process_exits(self):
        owner = self.spawn("try:\n"
            "    with runtime.locked('portable.lock'):\n"
            "        raise LookupError('synthetic failure')\n"
            "except LookupError:\n"
            "    print('RELEASED', flush=True)\n"
            "sys.stdin.readline()\n")
        self.expect(owner, 'RELEASED')
        self.assertIsNone(owner.poll())
        contender = self.spawn("with runtime.locked('portable.lock', timeout=3):\n"
                               "    print('ACQUIRED', flush=True)\n")
        self.expect(contender, 'ACQUIRED')
        self.finish(contender)
        self.assertIsNone(owner.poll())
        self.release(owner)
        self.finish(owner)

    def test_terminated_owner_does_not_leave_a_stale_os_lock(self):
        owner = self.holder()
        owner.terminate()  # Only this test's child; no finally block or application cleanup.
        owner.wait(timeout=15)
        self.assertNotEqual(owner.returncode, 0)
        contender = self.spawn("with runtime.locked('portable.lock', timeout=3):\n"
                               "    print('ACQUIRED', flush=True)\n")
        self.expect(contender, 'ACQUIRED')
        self.finish(contender)

    def test_json_state_round_trip_preserves_korean_text_and_original_whitespace(self):
        settings = {'computer_name': '개발용 노트북', 'model': 'provider default',
                    'message': '첫 줄  두 칸\n둘째 줄 "따옴표"\\경로', 'reply_routing_enabled': False}
        with runtime.locked('settings.lock'):
            runtime.write_json('delivery.json', settings)
        self.assertEqual(runtime.read_json('delivery.json'), settings)
        self.assertIn('개발용 노트북'.encode('utf-8'), (self.state / 'delivery.json').read_bytes())
        settings['reply_routing_enabled'] = True
        with runtime.locked('settings.lock'):
            runtime.write_json('delivery.json', settings)
        self.assertEqual(runtime.read_json('delivery.json'), settings)
        self.assertEqual(list(self.state.glob('*.tmp')), [])

    def test_selected_and_watched_metadata_keep_exact_provider_identity(self):
        identifier = '11111111-1111-4111-8111-111111111111'
        rows = [{'provider': provider, 'session_id': identifier, 'name': '동일한 작업 제목',
                 'code': sessions.code(provider, identifier), 'cwd': str(self.folder / '한글 작업'),
                 'state': '재개 가능', 'live': False, 'reachable': True}
                for provider in ('claude', 'codex', 'agy')]
        catalog = {'rows': rows, 'fetched_at': time.time()}
        runtime.write_json('ui.json', {'selected_session': rows[0], 'section': 'all', 'note': '사용자 설정 보존'})
        # Checking AGY work installs its hook in the real ~/.gemini config: never from a test.
        self.enterContext(patch('talkturn.agy_hooks.install'))
        sessions.watch_many([row['code'] for row in rows], catalog)
        self.assertEqual(sessions.watched(), rows)
        self.assertEqual(sessions.selected(), rows[0])
        self.assertEqual(len({sessions.identity(row) for row in sessions.watched()}), 3)
        sessions.watch_many([rows[2]['code']], catalog)
        self.assertEqual(sessions.selected(), rows[2])
        self.assertEqual(runtime.read_json('ui.json')['note'], '사용자 설정 보존')
        sessions.watch_many([], catalog)
        self.assertEqual(sessions.watched(), [])
        self.assertIsNone(sessions.selected())

    @unittest.skipIf(os.name == 'nt', 'POSIX native executable permissions')
    def test_posix_native_provider_override_is_executable_without_windows_suffix(self):
        binary = self.folder / 'bin' / 'agy'
        binary.parent.mkdir()
        binary.write_text("#!/bin/sh\nprintf 'portable-provider-ok\\n'\n", encoding='utf-8')
        binary.chmod(0o700)
        environment = {**self.environment, 'PATH': str(binary.parent), 'TALKTURN_AGY_BIN': str(binary),
                       'TOKTURN_AGY_BIN': ''}
        with patch.dict(os.environ, environment):
            chosen = runtime.executable('agy')
            self.assertEqual(Path(chosen), binary)
            result = subprocess.run([chosen], cwd=self.folder, env=environment, capture_output=True,
                                    text=True, encoding='utf-8', timeout=10)
            self.assertEqual((result.returncode, result.stdout), (0, 'portable-provider-ok\n'))
            binary.chmod(0o600)
            with self.assertRaises(RuntimeError):
                runtime.executable('agy')


if __name__ == '__main__':
    unittest.main()
