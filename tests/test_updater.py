import io
import json
import unittest
from unittest.mock import patch, Mock

from talkturn import updater, service, mouse_lock

RELEASES = [
    {'tag_name': 'v0.1.1', 'published_at': '2026-11-19T06:00:00Z', 'draft': False,
     'assets': [{'name': 'talkturn-0.1.1.tar.gz', 'browser_download_url': 'https://x/0.1.1.tar.gz'},
                {'name': 'talkturn-0.1.1-py3-none-any.whl', 'browser_download_url': 'https://x/0.1.1.whl'}]},
    {'tag_name': 'v0.1.0', 'published_at': '2026-11-17T08:08:30Z', 'draft': False,
     'assets': [{'name': 'talkturn-0.1.0-py3-none-any.whl', 'browser_download_url': 'https://x/0.1.0.whl'}]},
    {'tag_name': 'v0.2.0', 'published_at': None, 'draft': True,
     'assets': [{'name': 'talkturn-0.2.0-py3-none-any.whl', 'browser_download_url': 'https://x/0.2.0.whl'}]},
]


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        response = io.BytesIO(json.dumps(RELEASES).encode())
        response.__enter__ = lambda self=response: self
        response.__exit__ = lambda *args: None
        patcher = patch.object(updater.urllib.request, 'urlopen', return_value=response)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_lists_published_wheels_newest_first_and_picks_any_version(self):
        rows = updater.releases()
        self.assertEqual([row['version'] for row in rows], ['0.1.1', '0.1.0'])
        with patch.object(updater, '__version__', '0.1.0'):
            self.assertEqual(updater.label(rows[0], rows), '0.1.1 · 2026-11-19 · 최신')
            self.assertEqual(updater.label(rows[1], rows), '0.1.0 · 2026-11-17 · 지금 사용 중')
        self.assertEqual(updater.pick(rows, '최신')['wheel'], 'https://x/0.1.1.whl')
        self.assertEqual(updater.pick(rows, 'v0.1.0')['wheel'], 'https://x/0.1.0.whl')
        self.assertRaises(ValueError, updater.pick, rows, '0.2.0')

    def test_installs_the_chosen_wheel_and_leaves_a_stopped_receiver_stopped(self):
        with patch.object(service, 'worker_pid', return_value=None), patch.object(service, 'process_running', return_value=False), \
             patch.object(mouse_lock, 'active', return_value=False), patch.object(service, 'stop') as stop, \
             patch.object(updater.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            message = updater.update(None, '0.1.0')
        self.assertEqual(run.call_args.args[0][-1], 'https://x/0.1.0.whl')
        self.assertEqual(run.call_count, 1)
        stop.assert_not_called()
        self.assertIn('0.1.0 설치 완료', message)


if __name__ == '__main__':
    unittest.main()
