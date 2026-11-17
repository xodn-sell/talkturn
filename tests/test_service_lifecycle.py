from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from talkturn import runtime, service


class ServiceLifecycleTest(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        for module in (runtime, service):
            stack.enter_context(patch.object(module, 'ROOT', self.root))
        runtime.write_json('delivery.json', {'window_id': 123})
        stack.enter_context(patch.object(service, 'delivery_issues', return_value=[]))
        self.ping = stack.enter_context(patch.object(service, 'ping'))
        self.lock = stack.enter_context(patch.object(service, '_lock_notice', return_value=''))
        self.spawn = stack.enter_context(patch.object(service.subprocess, 'Popen'))

    def test_start_during_shutdown_reports_pending_without_reconnecting(self):
        (self.root / 'reply-bridge.pid').write_text('1234')
        with patch.object(service, 'process_running', return_value=True):
            for connect in (True, False):
                with self.subTest(connect=connect), self.assertRaisesRegex(RuntimeError, '종료 중'):
                    service.start(connect=connect)
        self.ping.assert_not_called()
        self.lock.assert_not_called()
        self.spawn.assert_not_called()
        self.assertFalse((self.root / 'reply-enabled').exists())

    def test_failed_spawn_or_early_exit_does_not_leave_notifications_enabled(self):
        for failure in ('spawn', 'early_exit', 'timeout'):
            with self.subTest(failure=failure), patch.object(service, 'process_running', return_value=False), \
                 patch.object(service.time, 'sleep'), \
                 patch.object(service.time, 'monotonic', side_effect=[0, 1, 6]):
                self.spawn.side_effect = OSError('failed') if failure == 'spawn' else None
                self.spawn.return_value.poll.return_value = 1 if failure == 'early_exit' else None
                with self.assertRaises((OSError, RuntimeError)):
                    service.start(connect=False)
                self.assertFalse((self.root / 'reply-enabled').exists())
                self.assertFalse((self.root / 'enabled').exists())
        self.ping.assert_not_called()
        self.lock.assert_not_called()

    def test_stop_during_ping_is_not_reported_as_running_or_overwritten(self):
        for initially_running in (True, False):
            with self.subTest(initially_running=initially_running):
                (self.root / 'reply-enabled').touch()
                (self.root / 'enabled').touch()
                self.ping.side_effect = service.stop
                with patch.object(service, 'process_running', return_value=initially_running), \
                     self.assertRaisesRegex(RuntimeError, '중지'):
                    service.start()
                self.assertFalse((self.root / 'reply-enabled').exists())
                self.assertFalse((self.root / 'enabled').exists())
        self.spawn.assert_not_called()
        self.lock.assert_not_called()

    def test_worker_exit_during_ping_is_not_reported_as_running(self):
        (self.root / 'reply-enabled').touch()
        with patch.object(service, 'process_running', side_effect=[True, False]), \
             self.assertRaisesRegex(RuntimeError, '중지'):
            service.start()
        self.spawn.assert_not_called()
        self.lock.assert_not_called()

    def test_stop_during_cold_spawn_is_not_reported_as_started(self):
        def spawn(*args, **kwargs):
            service.stop()
            return self.spawn.return_value

        self.spawn.side_effect = spawn
        with patch.object(service, 'process_running', side_effect=[False, True]), \
             self.assertRaisesRegex(RuntimeError, '중지'):
            service.start(connect=False)
        self.spawn.assert_called_once()
        self.lock.assert_not_called()
        self.assertFalse((self.root / 'reply-enabled').exists())
        self.assertFalse((self.root / 'enabled').exists())

    def test_normal_cold_start_does_not_require_preexisting_flags(self):
        for connect in (True, False):
            with self.subTest(connect=connect):
                service.stop()
                with patch.object(service, 'process_running', side_effect=[False, True]):
                    self.assertIn('시작됨', service.start(connect=connect))
                self.assertTrue((self.root / 'reply-enabled').exists())
                self.assertTrue((self.root / 'enabled').exists())
        self.assertEqual(self.spawn.call_count, 2)
        self.ping.assert_called_once()
        self.lock.assert_called_once()


if __name__ == '__main__':
    unittest.main()
