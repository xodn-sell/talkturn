"""Windows reader contention must not corrupt state or hide permanent write failures."""
import errno
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from talkturn import runtime


class AtomicWriteTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.path = self.root / 'state.json'
        self.path.write_text('old', encoding='utf-8')

    def assert_no_temporary_files(self):
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    @unittest.skipUnless(sys.platform == 'win32', 'Requires Windows file sharing semantics')
    def test_real_reader_releases_target_before_retry(self):
        reader = self.path.open('r', encoding='utf-8')
        self.addCleanup(reader.close)
        replace = runtime.os.replace
        with patch.object(runtime.os, 'replace', wraps=replace) as move, \
             patch.object(runtime.time, 'sleep', side_effect=lambda _: reader.close()) as sleep:
            runtime.atomic_write(self.path, 'new 한국어\n')
        self.assertEqual(move.call_count, 2)
        sleep.assert_called_once_with(.02)
        self.assertEqual(self.path.read_text(encoding='utf-8'), 'new 한국어\n')
        self.assert_no_temporary_files()

    @unittest.skipUnless(sys.platform == 'win32', 'Requires Windows file sharing semantics')
    def test_real_persistent_reader_is_bounded_and_preserves_original(self):
        replace = runtime.os.replace
        with self.path.open('r', encoding='utf-8'), \
             patch.object(runtime.os, 'replace', wraps=replace) as move, \
             patch.object(runtime.time, 'sleep') as sleep:
            with self.assertRaises(PermissionError) as caught:
                runtime.atomic_write(self.path, 'not committed')
        self.assertIn(caught.exception.winerror, (5, 32, 33))
        self.assertEqual(move.call_count, 6)
        self.assertEqual(sleep.call_count, 5)
        self.assertAlmostEqual(sum(call.args[0] for call in sleep.call_args_list), .62)
        self.assertEqual(self.path.read_text(encoding='utf-8'), 'old')
        self.assert_no_temporary_files()

    def test_windows_transient_errors_retry_same_temporary_content(self):
        replace = runtime.os.replace
        for code in (5, 32, 33):
            with self.subTest(code=code):
                error = PermissionError(errno.EACCES, 'synthetic Windows contention')
                error.winerror = code
                calls = []

                def move(source, target):
                    calls.append((source, target))
                    if len(calls) <= 2:
                        raise error
                    return replace(source, target)

                with patch.object(runtime.os, 'replace', side_effect=move), patch.object(runtime.time, 'sleep') as sleep:
                    runtime.atomic_write(self.path, 'committed')
                self.assertEqual(len(calls), 3)
                self.assertEqual(len({source for source, _ in calls}), 1)
                self.assertEqual(sleep.call_count, 2)
                self.assertEqual(self.path.read_text(encoding='utf-8'), 'committed')
                self.assert_no_temporary_files()

    def test_unrelated_errors_fail_once_without_waiting(self):
        errors = (PermissionError(errno.EACCES, 'permanent permission denied'),
                  OSError(errno.EIO, 'disk I/O error'))
        for error in errors:
            with self.subTest(error=type(error).__name__), \
                 patch.object(runtime.os, 'replace', side_effect=error) as move, \
                 patch.object(runtime.time, 'sleep') as sleep:
                with self.assertRaises(type(error)) as caught:
                    runtime.atomic_write(self.path, 'not committed')
                self.assertIs(caught.exception, error)
                self.assertEqual(move.call_count, 1)
                sleep.assert_not_called()
                self.assertEqual(self.path.read_text(encoding='utf-8'), 'old')
                self.assert_no_temporary_files()


if __name__ == '__main__':
    unittest.main()
